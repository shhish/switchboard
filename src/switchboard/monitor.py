"""Background fresh probes and transactionally queued incident notifications."""

import asyncio
import contextlib
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .alert_config import heartbeat_url
from .auth import read_private
from .errors import CapabilityError
from .eventkit import EventKit

SYSTEM_PROBE = "@monitor"
MAX_AGENT_SILENCES = 3


def duration(seconds):
    minutes = max(0, int(seconds // 60))
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def display_name(probe):
    """Human label in alerts, e.g. "Homebridge (Pi)"; config may set `label`."""
    if probe["name"] == SYSTEM_PROBE:
        return "Mac mini"
    if probe.get("label"):
        return probe["label"]
    name = probe["name"].replace("_", " ").replace("-", " ").title()
    return f"{name} (Pi)" if probe.get("group") == "pi" else name


def local_time(timestamp):
    zone = ZoneInfo("America/New_York")
    return datetime.fromtimestamp(timestamp, UTC).astimezone(zone).strftime("%-I:%M %p %Z")


class Monitor:
    def __init__(self, config, storage, probes, delivery):
        self.config = config
        self.db = storage.db
        self.probes = probes
        self.delivery = delivery
        self.task = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.config.monitor["enabled"]:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self):
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.cycle()
            except Exception:
                # A failed cycle must not silently end monitoring. Doctor reports age.
                pass
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0, self.config.monitor["interval_s"] - elapsed))

    async def cycle(self):
        async with self.lock:
            # Intentionally bypass the health TTL, but refresh its cache.
            result = await self.probes.run()
            self.process(result["services"], time.time())
            await self.heartbeat()

    async def heartbeat(self):
        path = self.config.monitor.get("heartbeat_url_file")
        if not path or not self.config.backends["eventkit"].get("enabled", False):
            return
        ok = False
        try:
            async with asyncio.timeout(5):
                url = heartbeat_url(await asyncio.to_thread(read_private, Path(path), 2048))
                data = await EventKit(self.config, timeout=4).call("net.probe", {"url": url})
                code = data.get("http_status")
                ok = (
                    not data.get("error")
                    and not data.get("local_network_denied")
                    and type(code) is int
                    and 200 <= code < 300
                )
        except Exception:
            # Neither exception text nor helper payloads may expose the private URL.
            pass
        with contextlib.suppress(sqlite3.Error):
            with self.db:
                self.db.executemany(
                    "INSERT OR REPLACE INTO monitor_meta VALUES (?, ?)",
                    (("last_heartbeat_at", time.time()), ("last_heartbeat_ok", int(ok))),
                )

    def process(self, services, now):
        configs = {row["name"]: row for row in self.config.probes}
        failing = {"down", "degraded"}
        # Include opted-out probes in host detection. Require a loopback witness.
        from .probes import is_lan

        all_failed = (
            bool(services)
            and any(p.get("alert", True) for p in configs.values())
            and len(services) == len(configs)
            and any(not is_lan(row["url"]) for row in configs.values())
            and all(row["status"] in failing for row in services)
        )
        with self.db:
            self.db.execute("DELETE FROM monitor_silence WHERE until<=?", (now,))
            system = {"name": SYSTEM_PROBE, "group": "host"}
            system_status = (
                "down"
                if all_failed
                else (
                    "unknown"
                    if services
                    and not any(row["status"] == "up" for row in services)
                    and any(row["status"] == "unknown" for row in services)
                    else "up"
                )
            )
            self.transition(system, system_status, now)
            if not all_failed or self.silenced(SYSTEM_PROBE, now):
                for row in services:
                    probe = configs.get(row["name"])
                    if probe and probe.get("alert", True):
                        self.transition(probe, row["status"], now)
            # Retire removed/opted-out states without issuing stale recovery notices.
            active = {name for name, row in configs.items() if row.get("alert", True)} | {
                SYSTEM_PROBE
            }
            for row in self.db.execute("SELECT name FROM monitor_state").fetchall():
                if row["name"] not in active:
                    self.db.execute("DELETE FROM monitor_state WHERE name=?", (row["name"],))
            self.db.execute(
                "INSERT OR REPLACE INTO monitor_meta VALUES ('last_cycle_at', ?)", (now,)
            )

    def transition(self, probe, status, now):
        name = probe["name"]
        row = self.db.execute("SELECT * FROM monitor_state WHERE name=?", (name,)).fetchone()
        state = (
            dict(row)
            if row
            else {
                "name": name,
                "status": status,
                "consecutive_failures": 0,
                "down_since": None,
                "alerted": 0,
                "notified": 0,
                "last_alert_at": None,
                "last_change_at": now,
                "incident_id": None,
            }
        )
        if state["status"] != status:
            state.update(status=status, last_change_at=now)
        if status == "unknown":
            # Preserve all incident counters/times. A permission issue is not recovery.
            pass
        elif status in ("down", "degraded"):
            if state["down_since"] is None:
                state.update(down_since=now, incident_id=uuid4().hex)
            state["consecutive_failures"] += 1
            kind = None
            if not state["alerted"] and state["consecutive_failures"] >= probe.get(
                "failures_before_alert", self.config.monitor["failures_before_alert"]
            ):
                kind = "down"
            elif (
                state["alerted"]
                and now - state["last_alert_at"] >= self.config.monitor["reminder_hours"] * 3600
            ):
                kind = "reminder"
            if kind and not self.silenced(name, now) and self.notice(probe, state, kind, now):
                state.update(alerted=1, last_alert_at=now)
        elif status == "up":
            if (state["alerted"] or state["notified"]) and self.config.monitor["recovery_alerts"]:
                self.notice(probe, state, "recovery", now)
            if state["down_since"] is not None:
                # Self-managing: a silence ends once the service it covered is back,
                # so it can never hide a second outage later in its window.
                self.db.execute("DELETE FROM monitor_silence WHERE name=?", (name,))
            state.update(
                consecutive_failures=0,
                down_since=None,
                alerted=0,
                notified=0,
                last_alert_at=None,
                incident_id=None,
            )
        self.db.execute(
            "INSERT OR REPLACE INTO monitor_state "
            "(name, status, consecutive_failures, down_since, alerted, notified, last_alert_at, "
            "last_change_at, incident_id) "
            "VALUES (:name, :status, :consecutive_failures, :down_since, :alerted, :notified, "
            ":last_alert_at, :last_change_at, :incident_id)",
            state,
        )

    def notice(self, probe, state, kind, now):
        # Kuma-style: "[Homebridge (Pi)] [🔴 Down]"; short plain body, local time.
        name = display_name(probe)
        since = local_time(state["down_since"])
        elapsed = duration(now - state["down_since"])
        if probe["name"] == SYSTEM_PROBE and kind == "recovery":
            title = f"[{name}] [✅ Up]"
            body = f"Services are reachable again after {elapsed}."
        elif probe["name"] == SYSTEM_PROBE and kind == "reminder":
            title = f"[{name}] [🔴 Still down]"
            body = f"No service has answered for {elapsed}, since {since}."
        elif probe["name"] == SYSTEM_PROBE:
            title = f"[{name}] [🔴 Down] Can't reach any service"
            body = f"No service answered since {since}. The Mac's network may be down."
        elif kind == "recovery":
            title = f"[{name}] [✅ Up]"
            body = f"{name} is back up after {elapsed} (down since {since})."
        elif kind == "reminder":
            title = f"[{name}] [🔴 Still down]"
            body = f"{name} has been down for {elapsed}, since {since}."
        else:
            title = f"[{name}] [🔴 Down]"
            body = f"{name} is not responding (since {since})."
        payload = {"title": title, "kind": kind, "body": body}
        key = f"{state['incident_id']}:{kind}"
        if kind == "reminder":
            key += f":{now}"
        return self.delivery.queue(key, payload, now)

    def validate_probe(self, name):
        if name != "all" and name not in {probe["name"] for probe in self.config.probes}:
            raise CapabilityError("invalid_arguments", "Unknown configured probe")

    def silenced(self, name, now):
        if name == SYSTEM_PROBE:
            # A synthetic incident must not bypass a planned service silence.
            names = [row["name"] for row in self.config.probes] + ["all"]
            return any(self.silenced(probe, now) for probe in names)
        return (
            self.db.execute(
                "SELECT 1 FROM monitor_silence WHERE name IN (?, 'all') AND until>?", (name, now)
            ).fetchone()
            is not None
        )

    def silence(self, name, minutes, reason, client, now=None):
        return self._silence(name, minutes, reason, client, now=now)

    def restart_silence(self, name, reason, client):
        """Internal restart path; attribution stays intact, agent quota is unaffected."""
        if name == "all":
            raise CapabilityError("silence_forbidden", "Restart silences must cover one probe")
        return self._silence(name, 10, reason, client, system=True)

    def _silence(self, name, minutes, reason, client, now=None, *, system=False):
        self.validate_probe(name)
        now = time.time() if now is None else now
        if client != "owner" and not system:
            # Agents silence one service at a time, a few at most; muting everything is
            # the owner's call. Silences still expire (<= 4 h) and end on recovery.
            if name == "all":
                raise CapabilityError("silence_forbidden", "Only the owner can silence all alerts")
            (active,) = self.db.execute(
                "SELECT COUNT(*) FROM monitor_silence WHERE until>? AND client!='owner' "
                "AND name!=? AND system=0",
                (now, name),
            ).fetchone()
            if active >= MAX_AGENT_SILENCES:
                raise CapabilityError(
                    "silence_limit", "Too many active silences; unsilence one first"
                )
        until = now + minutes * 60
        with self.db:
            self.db.execute("DELETE FROM monitor_silence WHERE until<=?", (now,))
            self.db.execute(
                "INSERT OR REPLACE INTO monitor_silence "
                "(name, until, reason, client, created_at, system) VALUES (?, ?, ?, ?, ?, ?)",
                (name, until, reason, client, now, int(system)),
            )
            names = {name, SYSTEM_PROBE} if name != "all" else None
            for row in self.db.execute("SELECT * FROM monitor_state").fetchall():
                if names is None or row["name"] in names:
                    if row["incident_id"]:
                        self.delivery.supersede(row["incident_id"], now)
                    self.db.execute(
                        "UPDATE monitor_state SET alerted=0, last_alert_at=NULL, incident_id=? "
                        "WHERE name=?",
                        (uuid4().hex if row["incident_id"] else None, row["name"]),
                    )
        return {
            "status": "ok",
            "probe": name,
            "until": datetime.fromtimestamp(until, UTC).isoformat(),
        }

    def unsilence(self, name):
        self.validate_probe(name)
        with self.db:
            self.db.execute("DELETE FROM monitor_silence WHERE name=?", (name,))
        return {"status": "ok", "probe": name}

    def status(self):
        row = self.db.execute("SELECT value FROM monitor_meta WHERE key='last_cycle_at'").fetchone()
        active = {row["name"] for row in self.config.probes if row.get("alert", True)} | {
            SYSTEM_PROBE
        }
        incidents = [
            {
                "name": row["name"],
                "status": row["status"],
                "down_since": row["down_since"],
                "alerted": bool(row["alerted"]),
                "consecutive_failures": row["consecutive_failures"],
            }
            for row in self.db.execute(
                "SELECT * FROM monitor_state WHERE down_since IS NOT NULL ORDER BY name"
            )
            if row["name"] in active
        ]
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM monitor_silence WHERE until<=?", (now,))
        silences = [
            {
                "probe": item["name"],
                "until": datetime.fromtimestamp(item["until"], UTC).isoformat(),
                "reason": item["reason"],
                "client": item["client"],
            }
            for item in self.db.execute("SELECT * FROM monitor_silence ORDER BY name")
        ]
        return {
            "status": "ok",
            "silences": silences,
            "monitor_enabled": self.config.monitor["enabled"],
            "heartbeat": heartbeat_status(self.config, self.db),
            "last_cycle_at": row[0] if row else None,
            "interval_s": self.config.monitor["interval_s"],
            "open_incidents": incidents,
            **self.delivery.status(),
        }


def heartbeat_status(config, db=None):
    values = (
        dict(
            db.execute(
                "SELECT key, value FROM monitor_meta WHERE key IN "
                "('last_heartbeat_at', 'last_heartbeat_ok')"
            )
        )
        if db is not None
        else {}
    )
    return {
        "configured": bool(config.monitor.get("heartbeat_url_file")),
        "last_ok": bool(values["last_heartbeat_ok"]) if "last_heartbeat_ok" in values else None,
        "last_at": values.get("last_heartbeat_at"),
    }
