"""Unanswered privacy-prompt detection uses only fake log text."""

import json
import math
from types import SimpleNamespace

import pytest

from switchboard import hostcmd, privacy_prompts
from switchboard.monitor import PRIVACY_PROMPTS_PROBE, PRIVACY_SAMPLE_S

from .test_monitor import outbox, setup_monitor, state

LINE = (
    "2026-11-23 03:34:20.944 Df kernel[0:3508087] (Sandbox) watchdog expired for approval "
    "entry {n} (kTCCService{service}, pid {pid})"
)


def log_text(*entries):
    return "\n".join(
        ["Timestamp               Ty Process[PID:TID]"]
        + [LINE.format(n=i, service=s, pid=p) for i, (s, p) in enumerate(entries)]
        + ["2026-11-23 03:34:23.795 Df log[99003:3904372] log run noninteractively"]
    )


def test_reading_counts_waits_services_and_processes():
    text = log_text(
        ("Photos", 1),
        ("Photos", 1),
        ("SystemPolicyRemovableVolumes", 2),
        ("SystemPolicyAllFiles", 3),
    )
    assert privacy_prompts.reading(text) == {
        "waits": 4,
        "processes": 3,
        "services": {"Photos": 2, "SystemPolicyRemovableVolumes": 1, "SystemPolicyAllFiles": 1},
    }
    assert privacy_prompts.reading("") == {"waits": 0, "processes": 0, "services": {}}
    many = log_text(*[(f"Service{chr(65 + i)}", i) for i in range(10)])
    assert len(privacy_prompts.reading(many)["services"]) == privacy_prompts.MAX_SERVICES
    assert privacy_prompts.describe(privacy_prompts.reading(text)) == (
        "Photos (2), external drives (1), Full Disk Access (1)"
    )


def test_only_the_fixed_log_query_is_allowed():
    assert hostcmd.valid_argv(list(hostcmd.PRIVACY_PROMPT_ARGV))
    for argv in (
        [hostcmd.LOG, "show", "--last", "1d"],
        [hostcmd.LOG, "stream"],
        [*hostcmd.PRIVACY_PROMPT_ARGV[:-1], 'eventMessage CONTAINS "password"'],
        [*hostcmd.PRIVACY_PROMPT_ARGV, "--info"],
    ):
        assert not hostcmd.valid_argv(argv)


async def test_sample_returns_none_when_log_unreadable(monkeypatch):
    async def run(argv, timeout_s=10):
        assert argv == hostcmd.PRIVACY_PROMPT_ARGV
        return -1, "", "PRIVATE"

    monkeypatch.setattr(hostcmd, "run", run)
    assert await privacy_prompts.sample() is None


@pytest.mark.parametrize("interval", [60, 90])
async def test_prompts_page_after_fifteen_minutes_then_recover(config, monkeypatch, interval):
    from switchboard import host_pressure
    from switchboard import monitor as monitor_module

    storage, _, monitor = setup_monitor(config)
    config.probes = []
    config.monitor["interval_s"] = interval
    clock = [1000]
    monkeypatch.setattr(monitor_module.time, "time", lambda: clock[0])
    text = [log_text(("SystemPolicyRemovableVolumes", 7), ("Photos", 8))]
    samples = []

    async def run(argv, timeout_s=10):
        samples.append(clock[0])
        return 0, text[0], ""

    async def pressure():
        return host_pressure.reading("1", (0.0,), 10, "")

    async def services():
        return {"services": []}

    monkeypatch.setattr(hostcmd, "run", run)
    monkeypatch.setattr(host_pressure, "sample", pressure)
    monitor.probes = SimpleNamespace(run=services)
    try:
        threshold = math.ceil(900 / interval) + 1
        assert monitor.failure_threshold({"name": PRIVACY_PROMPTS_PROBE}, None) == threshold
        for index in range(threshold - 1):
            clock[0] = 1000 + index * interval
            await monitor.cycle()
            assert not outbox(storage)
        assert state(storage, PRIVACY_PROMPTS_PROBE)["status"] == "degraded"
        clock[0] += interval
        await monitor.cycle()
        rows = outbox(storage)
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        message = json.dumps(payload)
        assert "Needs you" in message and "external drives (1)" in message
        assert "Photos (1)" in message and "sudo killall -9 tccd sandboxd" in message
        # The log is read at most once per sampling period, not every cycle.
        assert all(b - a >= PRIVACY_SAMPLE_S for a, b in zip(samples, samples[1:], strict=False))
        text[0] = log_text()
        clock[0] += PRIVACY_SAMPLE_S
        await monitor.cycle()
        assert state(storage, PRIVACY_PROMPTS_PROBE)["status"] == "up"
        assert json.loads(outbox(storage)[-1]["payload"])["kind"] == "recovery"
        assert not monitor.status()["open_incidents"]
    finally:
        storage.close()


async def test_unreadable_log_records_nothing(config, monkeypatch):
    from switchboard import host_pressure
    from switchboard import monitor as monitor_module

    storage, _, monitor = setup_monitor(config)
    config.probes = []
    monkeypatch.setattr(monitor_module.time, "time", lambda: 1000)

    async def run(argv, timeout_s=10):
        raise OSError("PRIVATE")

    async def pressure():
        return host_pressure.reading("1", (0.0,), 10, "")

    async def services():
        return {"services": []}

    monkeypatch.setattr(hostcmd, "run", run)
    monkeypatch.setattr(host_pressure, "sample", pressure)
    monitor.probes = SimpleNamespace(run=services)
    try:
        await monitor.cycle()
        assert not storage.db.execute(
            "SELECT 1 FROM monitor_state WHERE name=?", (PRIVACY_PROMPTS_PROBE,)
        ).fetchone()
        assert not outbox(storage)
    finally:
        storage.close()


async def test_privacy_probe_can_be_silenced(core):
    from .conftest import change_policy

    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("ops:silence"))
    result = await core.call(
        "codex",
        "ops_alerts_silence",
        {"probe": PRIVACY_PROMPTS_PROBE, "minutes": 30, "reason": "Owner away"},
    )
    assert result["status"] == "ok", result
