"""Alert ordering, retention and maintenance silences use private SQLite and fakes."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from switchboard.alerts import RETENTION_S
from switchboard.config import validate_probes
from switchboard.monitor import SYSTEM_PROBE
from switchboard.storage import Storage

from .conftest import change_policy
from .test_monitor import cycle, outbox, setup_monitor, state


async def test_recovery_supersedes_backoff_and_snapshot_rows(config, monkeypatch):
    storage, delivery, monitor = setup_monitor(config)
    seen = []

    async def send(channel, payload):
        seen.append(payload["kind"])
        if payload["kind"] == "down":
            raise OSError("fake failure")

    monkeypatch.setattr(delivery, "send", send)
    try:
        cycle(monitor, "down", 100)
        cycle(monitor, "down", 160)
        await delivery.deliver_due(160)
        cycle(monitor, "down", 160 + 6 * 3600)
        cycle(monitor, "up", 160 + 6 * 3600 + 1)
        rows = outbox(storage)
        assert [row["superseded_at"] is not None for row in rows] == [True, True, False]
        await delivery.deliver_due(160 + 6 * 3600 + 1)
        await delivery.deliver_due(160 + 7 * 3600)
        assert seen == ["down", "recovery"]
        assert delivery.status()["outbox_backlog"] == 0
    finally:
        storage.close()


async def test_recovery_invalidates_rows_already_selected_for_delivery(config, monkeypatch):
    storage, delivery, monitor = setup_monitor(config)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def send(channel, payload):
        seen.append(payload["kind"])
        entered.set()
        await release.wait()

    monkeypatch.setattr(delivery, "send", send)
    try:
        cycle(monitor, "down", 100)
        cycle(monitor, "down", 160)
        cycle(monitor, "down", 160 + 6 * 3600)
        task = asyncio.create_task(delivery.deliver_due(160 + 6 * 3600))
        await entered.wait()
        cycle(monitor, "up", 160 + 6 * 3600 + 1)
        release.set()
        await task
        assert seen == ["down"]  # The already selected reminder must never send.
        await delivery.deliver_due(160 + 6 * 3600 + 2)
        assert seen == ["down", "recovery"]
    finally:
        release.set()
        storage.close()


def test_recovery_supersession_rolls_back_with_incident(config, monkeypatch):
    storage, delivery, monitor = setup_monitor(config)
    try:
        cycle(monitor, "down", 100)
        cycle(monitor, "down", 160)
        original = delivery.queue

        def crash(*args):
            original(*args)
            raise RuntimeError("fake crash")

        monkeypatch.setattr(delivery, "queue", crash)
        with pytest.raises(RuntimeError):
            cycle(monitor, "up", 220)
        assert len(outbox(storage)) == 1
        assert outbox(storage)[0]["superseded_at"] is None
        assert state(storage)["alerted"]
    finally:
        storage.close()


def test_host_recovery_and_reminder_are_distinct(config):
    storage, _, monitor = setup_monitor(config)
    try:
        config.probes += validate_probes([{"name": "local", "url": "http://127.0.0.1/"}])
        failed = [{"name": probe["name"], "status": "down"} for probe in config.probes]
        monitor.process(failed, 100)
        monitor.process(failed, 160)
        monitor.process(failed, 160 + 6 * 3600)
        monitor.process([{**probe, "status": "up"} for probe in failed], 160 + 6 * 3600 + 60)
        down, reminder, recovery = [json.loads(row["payload"]) for row in outbox(storage)]
        assert "Can't reach any service" in down["title"]
        assert reminder["title"] == "[Mac mini] [🔴 Still down]"
        assert recovery["title"] == "[Mac mini] [✅ Up]"
        assert recovery["body"] == "Services are reachable again after 6 h 2 min."
    finally:
        storage.close()


async def test_terminal_retention_hourly_and_dedupe_window(config, monkeypatch):
    storage, delivery, _ = setup_monitor(config)
    payload = {"kind": "test", "title": "test", "body": "test"}
    now = RETENTION_S + 1000
    try:
        with storage.db:
            for index, column in enumerate(("delivered_at", "failed_at", "superseded_at")):
                for suffix, timestamp in (("old", 1), ("recent", now - 100)):
                    key = f"{index}-{suffix}"
                    delivery.queue(key, payload, timestamp)
                    storage.db.execute(
                        f"UPDATE alert_outbox SET {column}=? WHERE dedupe_key=?", (timestamp, key)
                    )
            ids = delivery.queue("pending", payload, now)
        monkeypatch.setattr(delivery, "send", lambda *_: asyncio.sleep(0))
        await delivery.deliver_due(now)
        keys = {row["dedupe_key"] for row in outbox(storage)}
        assert keys == {"0-recent", "1-recent", "2-recent", "pending"}
        with storage.db:
            assert delivery.queue("pending", payload, now + 1) == ids
            storage.db.execute("UPDATE alert_outbox SET delivered_at=1 WHERE dedupe_key='pending'")
        await delivery.deliver_due(now + 3599)
        assert len(outbox(storage)) == 4
        # The hourly timestamp persists across delivery-worker restarts.
        from switchboard.alerts import AlertDelivery

        delivery = AlertDelivery(config, storage)
        await delivery.deliver_due(now + 3600)
        assert len(outbox(storage)) == 3
    finally:
        storage.close()


def test_outbox_migration_preserves_existing_dedupe(config):
    config.state_dir.mkdir(mode=0o700)
    db = sqlite3.connect(config.state_dir / "state.sqlite3")
    db.execute(
        "CREATE TABLE alert_outbox (id TEXT PRIMARY KEY, channel TEXT, payload TEXT, "
        "dedupe_key TEXT, attempts INTEGER DEFAULT 0, created_at REAL, next_attempt_at REAL, "
        "delivered_at REAL, failed_at REAL, last_error_code TEXT, UNIQUE(channel,dedupe_key))"
    )
    db.execute(
        "INSERT INTO alert_outbox (id,channel,payload,dedupe_key,created_at,next_attempt_at) "
        "VALUES ('old','ntfy','{}','old:down',100,200)"
    )
    db.execute('UPDATE alert_outbox SET payload=\'{"kind":"down"}\', delivered_at=150')
    db.execute(
        "CREATE TABLE monitor_state (name TEXT PRIMARY KEY, status TEXT, "
        "consecutive_failures INTEGER, down_since REAL, alerted INTEGER, last_alert_at REAL, "
        "last_change_at REAL, incident_id TEXT)"
    )
    db.executemany(
        "INSERT INTO monitor_state VALUES (?, 'down', 2, 100, 1, 110, 100, ?)",
        [("delivered", "old"), ("pending", "not-sent")],
    )
    db.commit()
    db.close()
    storage = Storage(config.state_dir)
    try:
        assert outbox(storage)[0]["id"] == "old"
        assert outbox(storage)[0]["superseded_at"] is None
        assert state(storage, "delivered")["notified"] == 1
        assert state(storage, "pending")["notified"] == 0
    finally:
        storage.close()


@pytest.mark.parametrize("recovery", [False, True])
def test_silence_tracks_state_and_expiry_alerts_fresh(config, recovery):
    storage, _, monitor = setup_monitor(config)
    try:
        monitor.silence("homebridge", 1, "planned restart", "codex", now=100)
        cycle(monitor, "down", 110)
        cycle(monitor, "down", 140)
        assert state(storage)["consecutive_failures"] == 2
        assert not state(storage)["alerted"]
        assert not outbox(storage)
        if recovery:
            cycle(monitor, "up", 150)
            cycle(monitor, "up", 170)
            assert not outbox(storage)
        else:
            cycle(monitor, "down", 160)
            assert json.loads(outbox(storage)[0]["payload"])["kind"] == "down"
        assert not storage.db.execute("SELECT * FROM monitor_silence").fetchall()
    finally:
        storage.close()


@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
async def test_silence_pending_vs_delivered_and_recovery(config, monkeypatch, delivered, recovery):
    storage, delivery, monitor = setup_monitor(config)
    seen = []

    async def send(channel, payload):
        seen.append(payload["kind"])

    monkeypatch.setattr(delivery, "send", send)
    try:
        cycle(monitor, "down", 100)
        cycle(monitor, "down", 160)
        if delivered:
            await delivery.deliver_due(160)
        monitor.silence("homebridge", 1, "restart", "codex", now=170)
        await delivery.deliver_due(180)
        assert seen == (["down"] if delivered else [])
        cycle(monitor, "up" if recovery else "down", 190)
        if recovery:
            await delivery.deliver_due(190)
            assert seen == (["down", "recovery"] if delivered else [])
        else:
            cycle(monitor, "down", 230)
            await delivery.deliver_due(230)
            assert seen == (["down", "down"] if delivered else ["down"])
    finally:
        storage.close()


def test_silence_all_and_host_collapse_cannot_bypass(config):
    storage, _, monitor = setup_monitor(config)
    try:
        config.probes += validate_probes([{"name": "local", "url": "http://127.0.0.1/"}])
        failed = [{"name": probe["name"], "status": "down"} for probe in config.probes]
        monitor.silence("homebridge", 1, "restart", "codex", now=100)
        monitor.process(failed, 110)
        monitor.process(failed, 140)
        assert not state(storage, SYSTEM_PROBE)["alerted"]
        assert state(storage)["consecutive_failures"] == 2
        # The unrelated local service can still alert during per-probe maintenance.
        assert len(outbox(storage)) == 1
        monitor.silence("all", 2, "maintenance", "owner", now=150)
        monitor.process(failed, 170)
        assert not any(row["superseded_at"] is None for row in outbox(storage))
        monitor.unsilence("all")
        monitor.process(failed, 180)
        assert state(storage, SYSTEM_PROBE)["alerted"]
    finally:
        storage.close()


async def test_silence_tools_validation_scope_status_persistence_and_audit(core, monkeypatch):
    core.config.probes = validate_probes([{"name": "plex", "url": "http://127.0.0.1/"}])
    args = {"probe": "plex", "minutes": 10, "reason": "planned restart"}
    assert (await core.call("dot", "ops_alerts_silence", args))["error"]["code"] == "scope_denied"
    change_policy(core.config, lambda p: p["clients"]["dot"]["scopes"].append("ops:silence"))
    assert (await core.call("dot", "ops_alerts_silence", {**args, "probe": "typo"}))["error"][
        "code"
    ] == "invalid_arguments"
    for invalid in (
        {"minutes": 0},
        {"minutes": 241},
        {"minutes": True},
        {"reason": ""},
        {"reason": "x" * 201},
    ):
        assert (await core.call("dot", "ops_alerts_silence", {**args, **invalid}))["error"][
            "code"
        ] == "invalid_arguments"
    monkeypatch.setattr("switchboard.monitor.time.time", lambda: 100)
    result = await core.call("dot", "ops_alerts_silence", args)
    assert result["outcome"] == "ok"
    status = await core.call("dot", "ops_alerts_status", {})
    assert status["silences"] == [
        {"probe": "plex", "until": result["until"], "reason": "planned restart", "client": "dot"}
    ]
    stored = Storage(core.config.state_dir)
    assert stored.db.execute("SELECT client FROM monitor_silence").fetchone()[0] == "dot"
    stored.close()
    record = next(
        row
        for row in reversed(core.audit.tail())
        if row["capability"] == "ops_alerts_silence" and row["decision"] == "allowed"
    )
    assert record["client"] == "dot" and record["args"] == args
    assert (await core.call("dot", "ops_alerts_unsilence", {"probe": "plex"}))["status"] == "ok"
    assert (await core.call("dot", "ops_alerts_status", {}))["silences"] == []
    assert (await core.call("cortex", "ops_alerts_unsilence", {"probe": "all"}))["error"][
        "code"
    ] == "scope_denied"
    policy = yaml.safe_load(await asyncio.to_thread(Path("config/policy.example.yaml").read_text))
    assert {
        name for name, rule in policy["clients"].items() if "ops:silence" in rule["scopes"]
    } == {"owner", "dot", "orb", "openclaw", "claude", "codex", "agy"}


def test_agent_silences_are_self_limiting(config):
    from switchboard.errors import CapabilityError
    from switchboard.monitor import MAX_AGENT_SILENCES

    storage, _, monitor = setup_monitor(config)
    try:
        config.probes += validate_probes(
            [{"name": f"svc{n}", "url": f"http://127.0.0.{n + 2}/"} for n in range(4)]
        )
        with pytest.raises(CapabilityError) as denied:
            monitor.silence("all", 30, "maintenance", "codex", now=100)
        assert denied.value.code == "silence_forbidden"
        for n in range(MAX_AGENT_SILENCES):
            monitor.silence(f"svc{n}", 30, "restart", "codex", now=100)
        with pytest.raises(CapabilityError) as limited:
            monitor.silence("svc3", 30, "restart", "dot", now=100)
        assert limited.value.code == "silence_limit"
        monitor.silence("svc0", 60, "longer restart", "codex", now=100)  # renewing is fine
        monitor.silence("all", 30, "owner maintenance", "owner", now=100)  # owner exempt
    finally:
        storage.close()


def test_silence_ends_when_the_service_recovers(config):
    storage, _, monitor = setup_monitor(config)
    try:
        monitor.silence("homebridge", 240, "restart", "codex", now=100)
        cycle(monitor, "down", 110)
        cycle(monitor, "down", 170)
        cycle(monitor, "up", 230)  # back up: the silence must be gone now
        assert not storage.db.execute("SELECT * FROM monitor_silence").fetchall()
        cycle(monitor, "down", 290)
        cycle(monitor, "down", 350)  # a second outage in the old window alerts normally
        assert any(json.loads(r["payload"])["kind"] == "down" for r in outbox(storage))
    finally:
        storage.close()
