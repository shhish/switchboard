"""Incident transitions and durable outbox use fakes, never owner services."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from switchboard.alert_config import alert_settings, monitor_settings, ntfy_url
from switchboard.alerts import MAX_AGE_S, AlertDelivery
from switchboard.config import Config, validate_probes
from switchboard.monitor import SYSTEM_PROBE, Monitor
from switchboard.storage import Storage

from .conftest import change_policy, domain_payload


def setup_monitor(config):
    config.monitor = monitor_settings({"enabled": True})
    # No loopback witness in ordinary transition tests, so a single failure is an incident.
    config.probes = validate_probes(
        [{"name": "homebridge", "group": "pi", "url": "http://172.20.0.2/"}]
    )
    config.alerts = alert_settings({"ntfy": {"url_file": str(config.config_dir / "ntfy.url")}})
    storage = Storage(config.state_dir)
    delivery = AlertDelivery(config, storage)
    return storage, delivery, Monitor(config, storage, None, delivery)


def cycle(monitor, status, now):
    monitor.process([{"name": "homebridge", "group": "pi", "status": status}], now)


def outbox(storage):
    return [
        dict(row) for row in storage.db.execute("SELECT * FROM alert_outbox ORDER BY created_at")
    ]


def state(storage, name="homebridge"):
    return dict(storage.db.execute("SELECT * FROM monitor_state WHERE name=?", (name,)).fetchone())


def test_failures_reminders_recovery_unknown_and_restart(config):
    storage, delivery, monitor = setup_monitor(config)
    try:
        cycle(monitor, "down", 100)
        cycle(monitor, "unknown", 130)
        assert state(storage)["consecutive_failures"] == 1
        assert not outbox(storage)
        cycle(monitor, "degraded", 160)
        assert len(outbox(storage)) == 1
        assert state(storage)["down_since"] == 100
        incident = state(storage)["incident_id"]
        assert state(storage)["alerted"] == 1
        storage.close()
        storage = Storage(config.state_dir)
        delivery = AlertDelivery(config, storage)
        monitor = Monitor(config, storage, None, delivery)
        cycle(monitor, "down", 220)
        assert len(outbox(storage)) == 1  # Restart doesn't send another DOWN.
        assert state(storage)["incident_id"] == incident
        cycle(monitor, "unknown", 160 + 6 * 3600)
        assert len(outbox(storage)) == 1  # Even an overdue reminder waits for known status.
        cycle(monitor, "down", 160 + 6 * 3600 + 1)
        assert len(outbox(storage)) == 2
        cycle(monitor, "down", 160 + 6 * 3600 + 60)
        assert len(outbox(storage)) == 2
        cycle(monitor, "up", 160 + 6 * 3600 + 120)
        rows = outbox(storage)
        assert [json.loads(row["payload"])["kind"] for row in rows] == [
            "down",
            "reminder",
            "recovery",
        ]
        assert "back up after 6 h 3 min" in rows[-1]["payload"]
        assert "is back up" in json.loads(rows[-1]["payload"])["body"]
        assert "172.20.0.2" not in json.dumps(rows)
        assert state(storage)["down_since"] is None
        assert state(storage)["consecutive_failures"] == 0
        assert monitor.status()["open_incidents"] == []
        cycle(monitor, "down", 30000)
        cycle(monitor, "down", 30060)
        assert state(storage)["incident_id"] != incident
        assert len(outbox(storage)) == 4
    finally:
        storage.close()


def test_unknown_only_and_brief_outage_never_alert(config):
    storage, _, monitor = setup_monitor(config)
    try:
        for now in (10, 70, 30000):
            cycle(monitor, "unknown", now)
        assert state(storage)["consecutive_failures"] == 0
        cycle(monitor, "down", 31000)
        cycle(monitor, "up", 31060)
        assert not outbox(storage)
        assert not monitor.status()["open_incidents"]
    finally:
        storage.close()


def test_alert_opt_out_recovery_opt_out_and_removed_probe(config):
    storage, _, monitor = setup_monitor(config)
    try:
        config.probes[0]["alert"] = False
        cycle(monitor, "down", 10)
        cycle(monitor, "down", 70)
        assert not outbox(storage)
        config.probes[0]["alert"] = True
        cycle(monitor, "down", 100)
        cycle(monitor, "down", 160)
        config.monitor["recovery_alerts"] = False
        cycle(monitor, "up", 220)
        assert len(outbox(storage)) == 1
        cycle(monitor, "down", 280)
        config.probes = []
        monitor.process([], 340)
        assert not monitor.status()["open_incidents"]
    finally:
        storage.close()


def test_all_fail_collapses_and_unknown_is_not_a_host_failure(config):
    storage, _, monitor = setup_monitor(config)
    try:
        config.probes += validate_probes([{"name": "local", "url": "http://127.0.0.1/"}])
        failed = [{"name": p["name"], "status": "down"} for p in config.probes]
        monitor.process(failed, 100)
        monitor.process(failed, 160)
        assert len(outbox(storage)) == 1
        assert "Can't reach any service" in outbox(storage)[0]["payload"]
        assert state(storage, SYSTEM_PROBE)["alerted"] == 1
        assert len(monitor.status()["open_incidents"]) == 1
        monitor.process(
            [{"name": "local", "status": "down"}, {"name": "homebridge", "status": "unknown"}], 220
        )
        assert state(storage, SYSTEM_PROBE)["consecutive_failures"] == 2
        assert len(outbox(storage)) == 1
        monitor.process([{"name": p["name"], "status": "up"} for p in config.probes], 280)
        assert len(outbox(storage)) == 2
        assert not monitor.status()["open_incidents"]
    finally:
        storage.close()


def test_incident_and_outbox_rollback_together(config, monkeypatch):
    storage, delivery, monitor = setup_monitor(config)
    try:
        cycle(monitor, "down", 100)
        original = delivery.queue

        def broken(*args):
            original(*args)
            raise ValueError("simulated crash")

        monkeypatch.setattr(delivery, "queue", broken)
        with pytest.raises(ValueError):
            cycle(monitor, "down", 160)
        assert not outbox(storage)
        assert state(storage)["consecutive_failures"] == 1
        assert state(storage)["alerted"] == 0
    finally:
        storage.close()


async def test_outbox_backoff_expiry_dedupe_and_restart(config, monkeypatch):
    storage, delivery, _ = setup_monitor(config)
    payload = {"title": "[Switchboard] DOWN: test", "body": "test", "kind": "down"}
    try:
        with storage.db:
            ids = delivery.queue("incident:down", payload, 100)
            assert delivery.queue("incident:down", payload, 100) == ids
        calls = []

        async def fail(channel, payload):
            calls.append(channel)
            raise OSError("PRIVATE credentials and URL")

        monkeypatch.setattr(delivery, "send", fail)
        now = 100
        for attempt, minutes in enumerate((1, 2, 5, 15, 30, 60, 60), 1):
            await delivery.deliver_due(now)
            row = outbox(storage)[0]
            assert row["attempts"] == attempt
            assert row["next_attempt_at"] == now + minutes * 60
            assert row["last_error_code"] == "delivery_failed"
            await delivery.deliver_due(now + 1)
            assert len(calls) == attempt
            now = row["next_attempt_at"]
        await delivery.deliver_due(100 + MAX_AGE_S)
        assert outbox(storage)[0]["failed_at"] == 100 + MAX_AGE_S
        assert delivery.status()["outbox_failed"] == 1
        assert delivery.status()["outbox_backlog"] == 0
        assert "PRIVATE" not in json.dumps(outbox(storage))
        with storage.db:
            delivery.queue("other:down", payload, 90000)
        storage.close()
        storage = Storage(config.state_dir)
        delivery = AlertDelivery(config, storage)
        sent = []

        async def succeed(channel, payload):
            sent.append(payload)

        monkeypatch.setattr(delivery, "send", succeed)
        await delivery.deliver_due(90000)
        await delivery.deliver_due(90060)
        assert len(sent) == 1
        assert outbox(storage)[1]["delivered_at"] == 90000
        assert delivery.status()["channels"]["ntfy"]["last_success_at"] == 90000
        assert delivery.status()["channels"]["ntfy"]["last_failure_at"] is not None
    finally:
        storage.close()


async def test_monitor_fresh_cycles_update_health_cache(core):
    core.config.probes = validate_probes([{"name": "probe", "url": "http://172.20.0.2/"}])
    calls = []

    async def probe(_):
        calls.append(True)
        return {"name": "probe", "group": "", "status": "up", "latency_ms": 1}

    core.probes.probe = probe
    await core.probes.health()
    await core.monitor.cycle()
    await core.monitor.cycle()
    assert len(calls) == 3
    assert (await core.call("dot", "ops_health", {}))["services"][0]["status"] == "up"
    assert len(calls) == 3
    result = await core.call("dot", "ops_alerts_status", {})
    assert result["last_cycle_at"] is not None
    assert result["outbox_backlog"] == 0


async def test_lifecycle_and_slow_delivery_do_not_block_reads(core):
    entered, release = asyncio.Event(), asyncio.Event()
    core.config.monitor["enabled"] = True
    core.config.monitor["interval_s"] = 1
    core.config.alerts = alert_settings(
        {"ntfy": {"url_file": str(core.config.config_dir / "test")}}
    )

    async def slow(*_):
        entered.set()
        await release.wait()

    core.alerts.send = slow
    change_policy(core.config, lambda p: p["clients"]["owner"]["scopes"].append("ops:admin"))
    queued = await core.call("owner", "ops_alerts_test", {})
    assert len(queued["outbox_ids"]) == 1
    await asyncio.wait_for(entered.wait(), 2)
    await core.monitor.start()
    for _ in range(100):
        if core.monitor.status()["last_cycle_at"] is not None:
            break
        await asyncio.sleep(0.01)
    result = await asyncio.wait_for(core.call("dot", "ops_alerts_status", {}), 1)
    assert result["last_cycle_at"] is not None
    assert result["outbox_backlog"] == 1
    await core.monitor.close()
    assert core.monitor.task.done()
    release.set()


async def test_alert_tools_scopes_and_example_policy(core):
    assert (await core.call("dot", "ops_alerts_test", {}))["error"]["code"] == "scope_denied"
    change_policy(core.config, lambda p: p["clients"]["owner"]["scopes"].append("ops:admin"))
    assert domain_payload(await core.call("owner", "ops_alerts_test", {})) == {
        "status": "queued",
        "outbox_ids": [],
        "truncated": False,
    }
    assert (await core.call("dot", "ops_alerts_status", {}))["status"] == "ok"
    policy = yaml.safe_load(await asyncio.to_thread(Path("config/policy.example.yaml").read_text))
    assert [name for name, row in policy["clients"].items() if "ops:admin" in row["scopes"]] == [
        "owner"
    ]
    for client, row in policy["clients"].items():
        assert ("ops:diagnose" in row["scopes"]) is (
            client in {"owner", "claude", "codex", "agy", "openclaw"}
        )
        assert ("ops:restart" in row["scopes"]) is (client in {"owner", "claude", "codex", "agy"})


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": 1},
        {"interval_s": 0},
        {"interval_s": True},
        {"failures_before_alert": 0},
        {"reminder_hours": 0},
        {"reminder_hours": float("nan")},
        {"reminder_hours": True},
        {"recovery_alerts": "true"},
        {"shell": "no"},
    ],
)
def test_invalid_monitor_config(config, settings):
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"monitor": settings}))
    with pytest.raises(ValueError):
        Config.load()


@pytest.mark.parametrize(
    "url",
    [
        "http://ntfy.sh/topic",
        "https://evil.test/topic",
        "https://user@ntfy.sh/topic",
        "https://ntfy.sh/topic?token=PRIVATE",
        "https://ntfy.sh/topic#",
        "https://ntfy.sh/",
        "https://ntfy.sh:bad/topic",
        "https://ntfy.sh/topic\n",
        "https://ntfy.sh\\@evil.test/topic",
    ],
)
def test_invalid_ntfy_target(url):
    with pytest.raises(ValueError) as exc:
        ntfy_url(url, alert_settings({}))
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize(
    "settings",
    [
        {"host": "evil.test"},
        {"starttls": False},
        {"port": True},
        {"password": "PRIVATE"},
    ],
)
def test_invalid_smtp_config(config, settings):
    email = {
        "host": "smtp.gmail.com",
        "port": 587,
        "starttls": True,
        **{key + "_file": "/test/" + key for key in ("username", "password", "from", "to")},
        **settings,
    }
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"alerts": {"email": email}}))
    with pytest.raises(ValueError) as exc:
        Config.load()
    assert "PRIVATE" not in str(exc.value)


async def test_http_lifecycle_starts_and_stops_monitor(config, fake_backends):
    from switchboard.core import Core
    from switchboard.server import create_app

    from .conftest import running_app

    config.monitor["enabled"] = True
    core = Core(config)
    async with running_app(create_app(core)):
        assert core.monitor.task is not None
        for _ in range(100):
            if core.monitor.status()["last_cycle_at"] is not None:
                break
            await asyncio.sleep(0.01)
        assert core.monitor.status()["last_cycle_at"] is not None
    assert core.monitor.task.done()
    assert core.alerts.task.done()


@pytest.mark.parametrize("alert", [None, 0, "false", []])
def test_probe_alert_is_a_boolean(config, alert):
    with pytest.raises(ValueError):
        validate_probes([{"name": "probe", "url": "http://127.0.0.1/", "alert": alert}])


def test_valid_monitor_and_alert_config_load(config):
    data = {
        "monitor": {"enabled": True, "interval_s": 45, "reminder_hours": 0.5},
        "alerts": {"ntfy": {"url_file": "~/.config/switchboard/secrets/ntfy.url"}},
        "probes": [{"name": "local", "url": "http://127.0.0.1/", "alert": False}],
    }
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    loaded = Config.load()
    assert loaded.monitor == {
        "enabled": True,
        "interval_s": 45,
        "failures_before_alert": 2,
        "slow_failures_before_alert": 10,
        "reminder_hours": 0.5,
        "storage_reminder_hours": 24,
        "recovery_alerts": True,
    }
    assert loaded.alerts["ntfy"]["url_file"].startswith("/")
    assert loaded.probes[0]["alert"] is False


def test_no_channels_does_not_claim_an_alert_was_queued(config):
    storage, delivery, monitor = setup_monitor(config)
    try:
        settings = config.alerts
        config.alerts = alert_settings({})
        cycle(monitor, "down", 100)
        cycle(monitor, "down", 160)
        assert not state(storage)["alerted"]
        assert not outbox(storage)
        config.alerts = settings
        cycle(monitor, "down", 220)
        assert state(storage)["alerted"]
        assert len(outbox(storage)) == 1
        assert delivery.status()["outbox_backlog"] == 1
    finally:
        storage.close()


def test_alert_messages_read_like_kuma():
    from switchboard.monitor import display_name, duration

    pi = {"name": "homebridge", "group": "pi"}
    assert display_name(pi) == "Homebridge (Pi)"
    assert display_name({"name": "plex", "group": "media", "label": "Plex Media Server"}) == (
        "Plex Media Server"
    )
    assert display_name({"name": SYSTEM_PROBE}) == "Mac mini"
    assert duration(30) == "under a minute" and duration(720) == "12 min"
    assert duration(7200) == "2 h" and duration(7500) == "2 h 5 min"

    captured = []

    class Delivery:
        def queue(self, key, payload, now):
            captured.append(payload)
            return True

    monitor = Monitor.__new__(Monitor)
    monitor.delivery = Delivery()
    state = {"down_since": 1_000_000.0, "incident_id": "i1", "status": "down"}
    monitor.notice(pi, state, "alert", 1_000_120.0)
    monitor.notice(pi, state, "recovery", 1_000_720.0)
    down, up = captured
    assert down["title"] == "[Homebridge (Pi)] [🔴 Down]"
    assert up["title"] == "[Homebridge (Pi)] [✅ Up]"
    assert "back up after 12 min" in up["body"]
    for message in captured:
        assert "Switchboard" not in message["title"] + message["body"]


def test_per_probe_failure_threshold(config):
    storage, _, monitor = setup_monitor(config)
    try:
        config.probes = validate_probes(
            [
                {
                    "name": "homebridge",
                    "group": "pi",
                    "url": "http://172.20.0.2/",
                    "failures_before_alert": 4,
                }
            ]
        )
        for now in (100, 160, 220):
            cycle(monitor, "down", now)
        assert not outbox(storage)  # Three slow-disk stalls are not an outage.
        cycle(monitor, "up", 280)
        assert not outbox(storage)
        for now in (400, 460, 520, 580):
            cycle(monitor, "down", now)
        assert [json.loads(row["payload"])["kind"] for row in outbox(storage)] == ["down"]
    finally:
        storage.close()


def test_timeouts_need_a_longer_streak_than_hard_failures(config):
    storage, _, monitor = setup_monitor(config)
    try:

        def slow(now, reason="timeout"):
            row = {"name": "homebridge", "group": "pi", "status": "down", "reason": reason}
            monitor.process([row], now)

        for minute in range(9):
            slow(100 + 60 * minute)
        assert not outbox(storage)  # Nine minutes of a stalled Mac is not an outage.
        cycle(monitor, "up", 700)
        assert not outbox(storage)  # Nor does the blip send a recovery.
        for minute in range(10):
            slow(1000 + 60 * minute)
        assert [json.loads(row["payload"])["kind"] for row in outbox(storage)] == ["down"]
        cycle(monitor, "up", 2000)
        slow(3000, "refused")
        slow(3060, "refused")  # A refused connection is a dead service: page promptly.
        kinds = [json.loads(row["payload"])["kind"] for row in outbox(storage)]
        assert kinds == ["down", "recovery", "down"]
    finally:
        storage.close()


def test_storage_reminders_repeat_daily(config):
    storage, _, monitor = setup_monitor(config)
    try:
        disk = {"name": "storage:disk-x", "label": "server3", "storage_detail": "99% used"}
        for now in (0, 60, 6 * 3600 + 60):
            with storage.db:
                monitor.transition(disk, "down", now)
        assert len(outbox(storage)) == 1
        with storage.db:
            monitor.transition(disk, "down", 24 * 3600 + 60)
        assert [json.loads(row["payload"])["kind"] for row in outbox(storage)] == [
            "down",
            "reminder",
        ]
    finally:
        storage.close()


def test_slow_threshold_and_storage_reminder_settings_are_checked():
    settings = monitor_settings({"slow_failures_before_alert": 5, "storage_reminder_hours": 12})
    assert settings["slow_failures_before_alert"] == 5
    for bad in ({"slow_failures_before_alert": 0}, {"storage_reminder_hours": -1}):
        with pytest.raises(ValueError):
            monitor_settings(bad)
