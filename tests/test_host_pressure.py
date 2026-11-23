"""Pressure sampling, incident timing and contracts use only fake host readings."""

import json
import math
from types import SimpleNamespace

import pytest

from switchboard import host_pressure, hostcmd
from switchboard.alert_config import alert_settings
from switchboard.domains.services import SNAPSHOT
from switchboard.errors import CapabilityError
from switchboard.host_pressure import sample as read_pressure
from switchboard.monitor import HOST_PRESSURE_PROBE
from switchboard.schema import validate

from .conftest import change_policy
from .test_monitor import outbox, setup_monitor, state


@pytest.fixture
def readings(monkeypatch):
    values = {"kernel": "1", "load": 0.0, "cpus": 10, "swap": "total = 100M used = 97M"}
    calls = []

    async def run(argv):
        assert hostcmd.valid_argv(argv)
        calls.append(argv)
        assert argv[:2] == [hostcmd.SYSCTL, "-n"]
        key = {host_pressure.KERNEL_PRESSURE: "kernel", "vm.swapusage": "swap"}[argv[-1]]
        value = values[key]
        return (1, "PRIVATE", "PRIVATE") if value is None else (0, value, "")

    monkeypatch.setattr(hostcmd, "run", run)
    monkeypatch.setattr(host_pressure.os, "getloadavg", lambda: (values["load"], 0.0, 0.0))
    monkeypatch.setattr(host_pressure.os, "cpu_count", lambda: values["cpus"])
    monkeypatch.setattr(host_pressure, "sample", read_pressure)
    return values, calls


@pytest.mark.parametrize(
    "kernel,load,level,reason",
    [
        ("1", 0, "ok", None),
        ("1", 40, "ok", None),
        ("1", 40.1, "busy", "load_high"),
        ("1", 100, "busy", "load_high"),
        ("1", 100.1, "critical", "load_critical"),
        ("2", 0, "busy", "memory_warning"),
        ("2", 101, "critical", "load_critical"),
        ("4", 0, "critical", "memory_critical"),
        ("4", 101, "critical", "memory_critical"),
        (None, 0, None, "pressure_unavailable"),
        ("garbage", 0, None, "pressure_unavailable"),
        (None, 101, "critical", "load_critical"),
    ],
)
async def test_sample_levels(readings, kernel, load, level, reason):
    values, calls = readings
    values.update(kernel=kernel, load=load)
    result = await read_pressure()
    assert result["level"] == level
    assert result["load_per_core"] == load / 10
    assert result["swap_used_pct"] == 97  # Full swap alone never raises pressure.
    assert host_pressure.severity(result["kernel_level"], result["load_per_core"]) == (
        level,
        reason,
    )
    assert len(calls) == 2
    validate(result, SNAPSHOT["properties"]["pressure"])
    assert "PRIVATE" not in json.dumps(result)


async def test_missing_load_or_cpu_and_swap(readings, monkeypatch):
    values, _ = readings
    values.update(cpus=None, swap=None)
    result = await read_pressure()
    assert result == {
        "level": None,
        "kernel_level": 1,
        "load_per_core": None,
        "swap_used_pct": None,
    }

    def unavailable():
        raise OSError("PRIVATE")

    values["cpus"] = 10
    monkeypatch.setattr(host_pressure.os, "getloadavg", unavailable)
    assert (await read_pressure())["level"] is None
    values["kernel"] = "4"
    assert (await read_pressure())["level"] == "critical"
    values["swap"] = "total = 0M used = 0M"
    assert (await read_pressure())["swap_used_pct"] == 0


def test_exact_pressure_sysctl_allowlist():
    assert hostcmd.valid_argv([hostcmd.SYSCTL, "-n", host_pressure.KERNEL_PRESSURE])
    for argv in (
        [hostcmd.SYSCTL, "-n", "kern.memorystatus_vm_pressure_level_extra"],
        [hostcmd.SYSCTL, "-n", "kern.memorystatus_vm_pressure_level=4"],
        [hostcmd.SYSCTL, "-n", "kern.memorystatus_vm_pressure_level "],
        [hostcmd.SYSCTL, "-n", "kern.memorystatus_vm_pressure"],
        [hostcmd.SYSCTL, "-n", "kern.memorystatus_vm_pressure_level", "vm.swapusage"],
        [hostcmd.SYSCTL, "-w", "kern.memorystatus_vm_pressure_level=4"],
    ):
        assert not hostcmd.valid_argv(argv)


@pytest.mark.parametrize("interval", [30, 60, 90, 600])
@pytest.mark.parametrize("kernel,status,reason", [("4", "down", "memory_critical")])
async def test_cycle_threshold_reminder_recovery(
    config, readings, monkeypatch, interval, kernel, status, reason
):
    from switchboard import monitor as monitor_module

    values, calls = readings
    values["kernel"] = kernel
    storage, _, monitor = setup_monitor(config)
    config.probes = []
    config.monitor["interval_s"] = interval
    clock = [1000]
    monkeypatch.setattr(monitor_module.time, "time", lambda: clock[0])

    async def services():
        return {"services": []}

    monitor.probes = SimpleNamespace(run=services)
    original_transition = monitor.transition
    transitions = []

    def transition(probe, status, now, reason=None, *args, **kwargs):
        if probe["name"] == HOST_PRESSURE_PROBE:
            assert probe["group"] == "host"
            transitions.append((status, reason))
        return original_transition(probe, status, now, reason, *args, **kwargs)

    monkeypatch.setattr(monitor, "transition", transition)
    try:
        threshold = math.ceil(600 / interval) + 1
        assert monitor.failure_threshold({"name": HOST_PRESSURE_PROBE}, reason) == threshold
        for index in range(threshold - 1):
            clock[0] = 1000 + index * interval
            await monitor.cycle()
            assert not outbox(storage)
        assert clock[0] - 1000 < 600
        clock[0] += interval
        await monitor.cycle()
        assert clock[0] - 1000 >= 600
        assert transitions[-1] == (status, reason)
        assert state(storage, HOST_PRESSURE_PROBE)["consecutive_failures"] == threshold
        assert monitor.status()["open_incidents"][0]["name"] == HOST_PRESSURE_PROBE
        assert len(outbox(storage)) == 1
        clock[0] += interval
        values["kernel"] = "4"  # Escalation stays in the same pressure incident.
        await monitor.cycle()
        assert len(outbox(storage)) == 1
        clock[0] += config.monitor["reminder_hours"] * 3600
        await monitor.cycle()
        assert len(outbox(storage)) == 2
        values["kernel"] = "1"
        clock[0] += interval
        await monitor.cycle()
        await monitor.cycle()
        assert [json.loads(row["payload"])["kind"] for row in outbox(storage)] == [
            "down",
            "reminder",
            "recovery",
        ]
        assert not monitor.status()["open_incidents"]
        assert len(calls) >= 2 * threshold
    finally:
        storage.close()


async def test_busy_pressure_is_shown_but_never_pages(config, readings, monkeypatch):
    """The kernel warning level is common on a healthy Mac: status shows it, no notice."""
    from switchboard import monitor as monitor_module

    values, _ = readings
    values["kernel"] = "2"
    storage, _, monitor = setup_monitor(config)
    config.probes = []
    config.monitor["interval_s"] = 60
    clock = [1000]
    monkeypatch.setattr(monitor_module.time, "time", lambda: clock[0])

    async def services():
        return {"services": []}

    monitor.probes = SimpleNamespace(run=services)
    try:
        for index in range(30):
            clock[0] = 1000 + index * 60
            await monitor.cycle()
        assert state(storage, HOST_PRESSURE_PROBE)["status"] == "degraded"
        assert not outbox(storage)
        values["kernel"] = "1"
        clock[0] += 60
        await monitor.cycle()
        assert state(storage, HOST_PRESSURE_PROBE)["status"] == "up"
        assert not outbox(storage)
    finally:
        storage.close()


async def test_pressure_silence_and_status_tools(core, monkeypatch):
    from switchboard import monitor as monitor_module

    core.config.probes = []
    core.config.alerts = alert_settings(
        {"ntfy": {"url_file": str(core.config.config_dir / "test")}}
    )
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("ops:silence"))
    clock = [1000]
    monkeypatch.setattr(monitor_module.time, "time", lambda: clock[0])
    pressure = host_pressure.reading("4", (0.0,), 10, "total = 100M used = 97M")
    result = await core.call(
        "codex", "ops_alerts_silence", {"probe": HOST_PRESSURE_PROBE, "minutes": 15, "reason": "QA"}
    )
    assert result["status"] == "ok"
    for index in range(11):
        clock[0] = 1000 + index * 60
        core.monitor.process([], clock[0], pressure=pressure)
    result = await core.call("codex", "ops_alerts_status", {})
    assert result["silences"][0]["probe"] == HOST_PRESSURE_PROBE
    assert result["open_incidents"][0]["name"] == HOST_PRESSURE_PROBE
    assert not result["open_incidents"][0]["alerted"]
    assert not outbox(core.storage)
    clock[0] = 1900
    core.monitor.process([], clock[0], pressure=pressure)
    assert len(outbox(core.storage)) == 1
    pressure = host_pressure.reading("1", (0.0,), 10, "total = 100M used = 97M")
    core.monitor.process([], 1960, pressure=pressure)
    assert [json.loads(row["payload"])["kind"] for row in outbox(core.storage)] == [
        "down",
        "recovery",
    ]


def test_pressure_schema_rejects_invalid_levels():
    pressure = host_pressure.reading("1", (0.0,), 10, "")
    schema = SNAPSHOT["properties"]["pressure"]
    for key, value in (
        ("level", "healthy"),
        ("kernel_level", 3),
        ("load_per_core", -1),
        ("swap_used_pct", "97%"),
    ):
        with pytest.raises(CapabilityError):
            validate({**pressure, key: value}, schema)
    with pytest.raises(CapabilityError):
        validate({"level": "ok"}, schema)
