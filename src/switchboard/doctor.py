"""Read-only diagnostics, without ever returning secrets or backend bodies."""

import asyncio
import contextlib
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from . import hostcmd, remote
from .auth import CLIENTS, Tokens
from .errors import CapabilityError
from .monitor import heartbeat_status


def mode_check(path, mode):
    try:
        info = path.lstat()
        expected_type = stat.S_ISDIR if mode == 0o700 else stat.S_ISREG
        ok = (
            expected_type(info.st_mode)
            and stat.S_IMODE(info.st_mode) == mode
            and info.st_uid == os.getuid()
        )
        return {
            "status": "ok" if ok else "degraded",
            "reason": "present with correct mode" if ok else f"must be owned with mode {mode:04o}",
        }
    except OSError:
        return {"status": "down", "reason": "missing or inaccessible"}


def audit_chain(db):
    from .audit import read_checkpoint, verify_db

    try:
        # Incremental from the last hourly verification; `sb audit verify` does a full pass.
        result = verify_db(db, start=read_checkpoint(db))
    except sqlite3.Error:
        return {"status": "unknown", "reason": "audit tables unavailable"}
    if result["ok"]:
        return {"status": "ok", "rows": result["checked"]}
    # A break means rows were edited or deleted outside Switchboard.
    return {"status": "down", "reason": "audit hash chain broken", "seq": result["broken_at_seq"]}


async def diagnose(config, backends, policy, orb=None, monitor=None, *, core=None):
    checks = {
        "config_dir": mode_check(config.config_dir, 0o700),
        "config": mode_check(config.config_dir / "config.yaml", 0o600),
        "policy_file": mode_check(policy.path, 0o600),
        "token_dir": mode_check(config.token_dir, 0o700),
        # serve refuses to start when the audit log directory is wider than 0700.
        "log_dir": mode_check(config.log_dir, 0o700),
        "probes": {"status": "ok", "configured": len(config.probes)},
    }
    checks["storage"] = {
        "status": "disabled"
        if not config.storage["enabled"]
        else "ok"
        if os.access(hostcmd.SMARTCTL, os.X_OK)
        else "degraded",
        "reason": "not configured"
        if not config.storage["enabled"]
        else "configured; smartctl available"
        if os.access(hostcmd.SMARTCTL, os.X_OK)
        else "smartctl missing or not executable",
    }
    from .alerts import AlertDelivery

    last_cycle = None
    heartbeat = heartbeat_status(config)
    if monitor is not None:
        last_cycle = monitor.status()["last_cycle_at"]
        heartbeat = heartbeat_status(config, monitor.db)
        checks["audit_chain"] = audit_chain(monitor.db)
    else:
        try:
            path = config.state_dir / "state.sqlite3"
            with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute(
                    "SELECT value FROM monitor_meta WHERE key='last_cycle_at'"
                ).fetchone()
                last_cycle = row[0] if row else None
                heartbeat = heartbeat_status(config, db)
                checks["audit_chain"] = audit_chain(db)
        except (OSError, ValueError, sqlite3.Error):
            pass
    age = max(0, time.time() - last_cycle) if last_cycle is not None else None
    enabled = config.monitor["enabled"]
    checks["monitor"] = {
        "status": "degraded"
        if enabled and (age is None or age > 3 * config.monitor["interval_s"])
        else "ok",
        "enabled": enabled,
        "last_cycle_age_s": age,
    }
    heartbeat_age = time.time() - heartbeat["last_at"] if heartbeat["last_at"] is not None else None
    checks["heartbeat"] = {
        "status": "disabled"
        if not heartbeat["configured"]
        else (
            "ok"
            if heartbeat["last_ok"] and heartbeat_age is not None and 0 <= heartbeat_age < 300
            else "degraded"
        ),
        "reason": "not configured"
        if not heartbeat["configured"]
        else "recent success"
        if (heartbeat["last_ok"] and heartbeat_age is not None and 0 <= heartbeat_age < 300)
        else "no recent successful heartbeat",
    }

    async def remote_check(name):
        try:
            data = await remote.call(core or SimpleNamespace(config=config), name, "version")
            if not isinstance(data.get("version"), str) or not data["version"]:
                raise CapabilityError("backend_invalid", "Invalid agent version")
            return {"host": name, "status": "ok", "reason": "agent reachable"}
        except CapabilityError:
            return {"host": name, "status": "down", "reason": "remote agent unavailable"}

    remote_checks = await asyncio.gather(*(remote_check(name) for name in config.remote_hosts))
    for channel, check in AlertDelivery(config, None).config_status().items():
        checks["alerts_" + channel] = check
    if enabled and not any(name in config.alerts for name in ("ntfy", "email")):
        checks["alerts"] = {"status": "degraded", "reason": "no alert channels configured"}
    if config.backends["home"].get("enabled", True):
        control_file = config.backends["home"].get("control_token_file")
        checks["home_control_token"] = (
            mode_check(Path(control_file), 0o600)
            if control_file
            else {"status": "down", "reason": "control token path missing"}
        )
    if config.backends["gitea"].get("enabled", False):
        token_file = config.backends["gitea"].get("token_file")
        checks["gitea_token"] = (
            mode_check(Path(token_file), 0o600)
            if token_file
            else {"status": "down", "reason": "token path missing"}
        )
    try:
        data = policy.load()
        checks["policy"] = {"status": "ok", "reason": "valid"}
        expected = data.get("clients", {})
    except CapabilityError:
        checks["policy"] = {"status": "down", "reason": "missing or invalid"}
        expected = dict.fromkeys(CLIENTS)
    tokens = Tokens(config.token_dir)
    tokens.refresh()
    token_checks = {}
    for client in sorted(expected):
        check = mode_check(config.token_dir / f"{client}.token", 0o600)
        if check["status"] == "ok" and client not in tokens.hashes:
            check = {"status": "degraded", "reason": "invalid or duplicate credential"}
        token_checks[client] = check
    backends_status = await backends.status()
    if orb is None:
        from .upstreams.orb import OrbUpstream

        orb = OrbUpstream(config, policy)
        await orb.start()
        try:
            orb_status = await orb.status()
        finally:
            await orb.close()
    else:
        orb_status = await orb.status()
    all_checks = [
        *remote_checks,
        *checks.values(),
        *token_checks.values(),
        *backends_status.values(),
        orb_status,
    ]
    # A backend switched off on purpose is not a fault.
    status = "ok" if all(c["status"] in ("ok", "disabled") for c in all_checks) else "degraded"
    checks["remote_hosts"] = remote_checks
    return {
        "status": status,
        "checks": checks,
        "tokens": token_checks,
        "backends": backends_status,
        "upstreams": {"orb": orb_status},
        "limitations": [
            "FrameServer cannot prove TCC grants from an empty events/todos response",
            "Orb cards notify only; use owner CLI or trusted client to resolve",
            "HomeRelay scene contents are hidden; sensitive scene targets cannot be detected",
        ],
    }
