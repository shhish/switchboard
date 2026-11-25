"""Background fresh probes and transactionally queued incident notifications."""

import asyncio
import contextlib
import math
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import host_pressure, privacy_prompts
from .alert_config import heartbeat_url
from .auth import read_private
from .errors import CapabilityError
from .eventkit import EventKit

SYSTEM_PROBE = "@monitor"
HOST_PRESSURE_PROBE = "host-pressure"
PRIVACY_PROMPTS_PROBE = "privacy-prompts"
# Reading the unified log takes seconds: sample it every few minutes, not every cycle.
PRIVACY_SAMPLE_S = 300
PRIVACY_WAIT_S = 0.2
MAX_AGENT_SILENCES = 3
STATE_RANK = {"up": 0, "unknown": 1, "degraded": 2, "down": 3}


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
    def __init__(self, config, storage, probes, delivery, watches=None, board=None):
        self.config = config
        self.db = storage.db
        self.probes = probes
        self.delivery = delivery
        self.watches = watches
        self.board = board
        self.task = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.config.monitor["enabled"]:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        tasks = [t for t in (self.task, getattr(self, "privacy_task", None)) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
            pressure = await host_pressure.sample()
            prompts = await self.privacy_sample(time.time())
            self.process(result["services"], time.time(), pressure=pressure, prompts=prompts)
            await self.heartbeat()
            if self.watches:
                try:
                    await self.watches.evaluate()
                except Exception:
                    pass  # Watch storage/evaluation failures never end a monitor cycle.
            if self.board:
                try:
                    await self.board.evaluate_things()
                except Exception:
                    pass  # Capture failures never end monitoring or its other work.

    async def privacy_sample(self, now):
        """Latest privacy-prompt reading, refreshed at most every PRIVACY_SAMPLE_S.

        Reading the unified log can take seconds on a loaded Mac, so the refresh runs
        in the background: a cycle waits at most PRIVACY_WAIT_S for it, then reports the
        previous reading (None until the first one lands) and moves on.
        """
        last = getattr(self, "privacy_last", None)
        if last and now - last[0] < PRIVACY_SAMPLE_S:
            return last[1]
        task = getattr(self, "privacy_task", None)
        if task is None:

            async def refresh():
                try:
                    reading = await privacy_prompts.sample()
                except Exception:
                    reading = None  # An unreadable log is "unknown", never a guess.
                self.privacy_last = (now, reading)

            task = self.privacy_task = asyncio.create_task(refresh())
            task.add_done_callback(lambda _: setattr(self, "privacy_task", None))
        await asyncio.wait({task}, timeout=PRIVACY_WAIT_S)
        last = getattr(self, "privacy_last", None)
        return last[1] if last else None

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

    def storage_probes(self):
        health = getattr(self, "disk_health", None)
        return health.probe_names() if health else set()

    def process(self, services, now, *, pressure=None, prompts=None):
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
            if pressure is not None:
                level, reason = host_pressure.severity(
                    pressure["kernel_level"], pressure["load_per_core"]
                )
                self.transition(
                    {"name": HOST_PRESSURE_PROBE, "group": "host", "pressure_detail": pressure},
                    {"ok": "up", "busy": "degraded", "critical": "down", None: "unknown"}[level],
                    now,
                    reason,
                    # This Mac often sits at the kernel's warning level while healthy:
                    # show "busy" in status, page the owner only for critical.
                    observe_only=level == "busy",
                )
            if prompts is not None:
                self.transition(
                    {"name": PRIVACY_PROMPTS_PROBE, "group": "host", "privacy_detail": prompts},
                    "degraded" if prompts["waits"] else "up",
                    now,
                    "prompts_unanswered" if prompts["waits"] else None,
                )
            for row in services:
                probe = configs.get(row["name"])
                if probe:
                    self.transition(
                        probe,
                        row["status"],
                        now,
                        row.get("reason"),
                        row.get("latency_ms"),
                        observe_only=not probe.get("alert", True)
                        or (all_failed and not self.silenced(SYSTEM_PROBE, now)),
                    )
            # Retire removed probes without issuing stale recovery notices.
            active = set(configs) | {SYSTEM_PROBE, HOST_PRESSURE_PROBE, PRIVACY_PROMPTS_PROBE}
            active |= self.storage_probes()
            for row in self.db.execute("SELECT name FROM monitor_state").fetchall():
                if row["name"] not in active:
                    self.db.execute("DELETE FROM monitor_state WHERE name=?", (row["name"],))
            self.db.execute(
                "INSERT OR REPLACE INTO monitor_meta VALUES ('last_cycle_at', ?)", (now,)
            )

    def failure_threshold(self, probe, reason):
        if probe["name"] == HOST_PRESSURE_PROBE:
            # N samples span N-1 intervals: never page before ten minutes at this cadence.
            return math.ceil(600 / self.config.monitor["interval_s"]) + 1
        if probe["name"] == PRIVACY_PROMPTS_PROBE:
            # A prompt answered within a few minutes is normal; page after fifteen.
            return math.ceil(900 / self.config.monitor["interval_s"]) + 1
        threshold = probe.get("failures_before_alert", self.config.monitor["failures_before_alert"])
        if reason == "timeout":
            # Refused/5xx means the service is gone; a slow answer needs a longer streak.
            return max(threshold, self.config.monitor["slow_failures_before_alert"])
        return threshold

    def reminder_seconds(self, probe):
        key = "storage_reminder_hours" if "storage_detail" in probe else "reminder_hours"
        return self.config.monitor[key] * 3600

    def transition(self, probe, status, now, reason=None, latency_ms=None, *, observe_only=False):
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
        previous = row["status"] if row else "unknown"
        incident_id = state["incident_id"]
        # A storage warning that turns critical is news even mid-incident.
        escalated = (
            "storage_detail" in probe
            and state["alerted"]
            and state["status"] == "degraded"
            and status == "down"
        )
        if state["status"] != status:
            state.update(status=status, last_change_at=now)
        if status == "unknown" or observe_only:
            # Preserve all incident counters/times. A permission issue is not recovery.
            if observe_only and not probe.get("alert", True):
                # Opting out retires the alert incident, but preserves health history.
                state.update(
                    consecutive_failures=0,
                    down_since=None,
                    alerted=0,
                    notified=0,
                    last_alert_at=None,
                    incident_id=None,
                )
        elif status in ("down", "degraded"):
            if state["down_since"] is None:
                state.update(down_since=now, incident_id=uuid4().hex)
            state["consecutive_failures"] += 1
            kind = None
            if not state["alerted"] and state["consecutive_failures"] >= self.failure_threshold(
                probe, reason
            ):
                kind = "down"
            elif escalated:
                kind = "escalation"
            elif state["alerted"] and now - state["last_alert_at"] >= self.reminder_seconds(probe):
                kind = "reminder"
            if kind == "down" and not self.silenced(name, now):
                self.emit("service.down", name)
            if kind and not self.silenced(name, now) and self.notice(probe, state, kind, now):
                state.update(alerted=1, last_alert_at=now)
        elif status == "up":
            if state["down_since"] is not None and (state["alerted"] or state["notified"]):
                self.emit("service.recovered", name)
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
            "last_change_at, incident_id, latency_ms) "
            "VALUES (:name, :status, :consecutive_failures, :down_since, :alerted, :notified, "
            ":last_alert_at, :last_change_at, :incident_id, :latency_ms)",
            {**state, "latency_ms": latency_ms},
        )
        if previous != status or row is None:
            self.db.execute(
                "INSERT INTO monitor_events(name, at, status, previous, latency_ms, incident_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, now, status, previous, latency_ms, state["incident_id"] or incident_id),
            )
        # Unknown is worse than healthy but must not hide an observed degradation/outage.
        hour = now // 3600 * 3600
        sample = self.db.execute(
            "SELECT worst FROM monitor_hours WHERE hour=? AND name=?", (hour, name)
        ).fetchone()
        worst = max((sample[0], status), key=STATE_RANK.__getitem__) if sample else status
        self.db.execute(
            "INSERT INTO monitor_hours VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(hour, name) DO UPDATE SET worst=excluded.worst, "
            "checks=checks+1, failures=failures+excluded.failures",
            (hour, name, worst, int(status in {"degraded", "down"})),
        )

    def maintain(self, now=None):
        cutoff = (time.time() if now is None else now) - 35 * 86400
        with self.db:
            self.db.execute("DELETE FROM monitor_events WHERE at<?", (cutoff,))
            self.db.execute("DELETE FROM monitor_hours WHERE hour<?", (cutoff // 3600 * 3600,))

    def on_event(self, listener):
        """`listener(event, service)` hears confirmed outages and recoveries
        (not silenced ones); routines use it for `event:` triggers."""
        self.listeners = [*getattr(self, "listeners", []), listener]

    def emit(self, event, service):
        for listener in getattr(self, "listeners", []):
            try:
                listener(event, service)
            except Exception:  # noqa: BLE001 - a listener must not break monitoring
                pass

    def notice(self, probe, state, kind, now):
        # Kuma-style: "[Homebridge (Pi)] [🔴 Down]"; short plain body, local time.
        name = display_name(probe)
        since = local_time(state["down_since"])
        elapsed = duration(now - state["down_since"])
        if "pressure_detail" in probe:
            severity = "Critical" if state["status"] == "down" else "Warning"
            title = f"[{name}] [{'Recovered' if kind == 'recovery' else severity}]"
            body = (
                f"Host pressure recovered after {elapsed}."
                if kind == "recovery"
                else f"Host pressure has been elevated for {elapsed}, since {since}. "
                f"Kernel level: {probe['pressure_detail']['kernel_level']}; "
                f"load per core: {probe['pressure_detail']['load_per_core']}; "
                f"swap used: {probe['pressure_detail']['swap_used_pct']}%."
            )
        elif "privacy_detail" in probe:
            title = f"[{name}] [{'Recovered' if kind == 'recovery' else 'Needs you'}]"
            body = (
                f"macOS permission checks are answering again after {elapsed}."
                if kind == "recovery"
                else f"macOS permission checks have been timing out for {elapsed}, since {since}, "
                f"so apps can't use: {privacy_prompts.describe(probe['privacy_detail'])}. "
                "External drives, Docker and file reads hang until this clears. If a privacy "
                "prompt is on the Mac's screen, answer it; if none is, the permission service "
                "is stuck: run `sudo killall -9 tccd sandboxd` (macOS restarts both at once)."
            )
        elif "storage_detail" in probe:
            severity = "Critical" if state["status"] == "down" else "Warning"
            title = f"[{name}] [{'Recovered' if kind == 'recovery' else severity}]"
            body = (
                f"Storage health recovered after {elapsed}."
                if kind == "recovery"
                else probe["storage_detail"]
            )
        elif probe["name"] == SYSTEM_PROBE and kind == "recovery":
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
        if kind in ("reminder", "escalation"):
            key += f":{now}"
        return self.delivery.queue(key, payload, now)

    def validate_probe(self, name):
        if name != "all" and name not in (
            {probe["name"] for probe in self.config.probes}
            | self.storage_probes()
            | {HOST_PRESSURE_PROBE, PRIVACY_PROMPTS_PROBE}
        ):
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
            SYSTEM_PROBE,
            HOST_PRESSURE_PROBE,
            PRIVACY_PROMPTS_PROBE,
        }
        active |= self.storage_probes()
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
