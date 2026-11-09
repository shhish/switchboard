"""Storage contracts use synthetic diskutil/SMART fixtures and fake mounts only."""

import copy
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from switchboard import hostcmd
from switchboard.config import Config
from switchboard.disk_health import (
    DAY,
    HOUR,
    MAX_POINTS,
    DiskHealth,
    label_for,
    smart_reading,
    trend,
)
from switchboard.errors import CapabilityError
from switchboard.storage import Storage
from switchboard.storage_config import storage_settings

from .conftest import change_policy

FIXTURES = Path(__file__).parent / "fixtures" / "storage"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


@pytest.fixture
async def disks(core, monkeypatch):
    core.config.storage = storage_settings({"enabled": True})
    health = core.disk_health
    physical = plistlib.loads((FIXTURES / "physical.plist").read_bytes())
    infos, mounts = fixture("info"), fixture("mounts")
    whole, members = hostcmd.physical_inventory(physical)
    inventory = {"whole": whole, "members": members}
    replies = {
        "disk0": fixture("nvme"),
        "disk6": fixture("ata"),
        "disk7": fixture("disabled"),
        "disk9": fixture("disabled"),
    }
    calls = []

    async def runner(argv, **kwargs):
        assert hostcmd.valid_argv(argv, physical_disks=kwargs.get("physical_disks")), argv
        calls.append(argv)
        if argv == [hostcmd.DISKUTIL, "list", "-plist", "physical"]:
            return 0, plistlib.dumps(physical).decode(), ""
        assert kwargs["physical_disks"] == inventory
        if argv[0] == hostcmd.DISKUTIL:
            return 0, plistlib.dumps(infos[argv[-1]]).decode(), ""
        data = replies[argv[-1]]
        return data["smartctl"]["exit_status"], json.dumps(data), "PRIVATE stderr"

    def usage(path):
        percent = {"/System/Volumes/Data": 79, "/Volumes/library": 78, "/Volumes/library2": 85}.get(
            path, 95
        )
        return SimpleNamespace(
            f_blocks=1000,
            f_bfree=1000 - percent * 10,
            f_bavail=1000 - percent * 10,
            f_frsize=1024**3,
            f_flag=0,
        )

    health.runner = runner
    health.mounts = lambda: copy.deepcopy(mounts)
    health.statvfs = usage
    health.important_free = lambda path: None
    return core, health, calls, replies, mounts


async def test_discovery_shared_space_filters_and_redaction(disks):
    core, health, calls, replies, _ = disks
    result = await core.call("cortex", "storage_status", {})
    assert result["status"] == "ok", result
    assert len(result["disks"]) == 4
    assert len(result["containers"]) == 4
    assert len(result["volumes"]) == 6
    archive = next(c for c in result["containers"] if c["container_id"] == "disk10")
    assert archive["used_percent"] == 95
    assert len(archive["shares_space_with"]) == 2
    volumes = [v for v in result["volumes"] if v["container_id"] == "disk10"]
    assert len({v["label"] for v in volumes}) == 1
    assert all(v["free_bytes"] == 50 * 1024**3 for v in volumes)
    assert len(health.db.execute("SELECT * FROM storage_samples").fetchall()) == 4
    assert result["disks"][0]["smart"]["percentage_used"] == 12
    assert result["disks"][1]["smart"]["power_on_hours"] == 12345
    assert result["disks"][2]["smart"]["state"] == "disabled"
    encoded = json.dumps(result) + json.dumps(
        [tuple(r) for r in health.db.execute("SELECT * FROM storage_smart")]
    )
    assert "DO-NOT-EXPOSE" not in encoded and "PRIVATE" not in encoded
    assert all(c["mount"] != "/" for c in result["containers"])
    assert any(c["mount"] == "/System/Volumes/Data" for c in result["containers"])
    assert all(v["mount"] != "/Volumes/App Installer" for v in result["volumes"])
    before = len([c for c in calls if c[0] == hostcmd.SMARTCTL])
    await core.call("cortex", "storage_status", {})
    assert len([c for c in calls if c[0] == hostcmd.SMARTCTL]) == before
    result = await core.call("cortex", "storage_status", {"refresh": True})
    assert result["status"] == "ok"
    assert len([c for c in calls if c[0] == hostcmd.SMARTCTL]) == before + 4
    history = await core.call("cortex", "storage_history", {"label": archive["label"], "days": 35})
    assert history["status"] == "ok" and len(history["points"]) == 1
    assert history["points"][0]["used_bytes"] == archive["used_bytes"]


@pytest.mark.parametrize(
    "name,state",
    [
        ("nvme", "ok"),
        ("ata", "ok"),
        ("disabled", "disabled"),
        ("standby", "standby"),
        ("unsupported", "unsupported"),
    ],
)
def test_smart_states(name, state):
    data = fixture(name)
    result = smart_reading(data, data["smartctl"]["exit_status"])
    assert result["state"] == state
    if state != "ok":
        assert result["passed"] is None


@pytest.mark.parametrize(
    "argv",
    [
        [hostcmd.DISKUTIL, "list", "-plist"],
        [hostcmd.DISKUTIL, "info", "-plist", "disk42"],
        [hostcmd.DISKUTIL, "info", "-plist", "disk0;whoami"],
        [hostcmd.DISKUTIL, "info", "-plist", "/dev/disk0"],
        [hostcmd.DISKUTIL, "info", "-plist", "disk0000"],
        [hostcmd.DISKUTIL, "info", "-plist", "disk0s1s2s3"],
        [hostcmd.DISKUTIL, "eraseDisk", "disk0"],
        [hostcmd.SMARTCTL, "-s", "on", "disk6"],
        [hostcmd.SMARTCTL, "-j", "-H", "-A", "-i", "disk6"],
        [hostcmd.SMARTCTL, "-j", "-n", "standby", "-H", "-A", "-i", "disk6s2"],
        [hostcmd.SMARTCTL, "-j", "-n", "standby", "-H", "-A", "-i", "disk42"],
        [hostcmd.SMARTCTL, "-j", "-n", "standby", "-H", "-A", "-i", "disk6", "-s", "on"],
    ],
)
async def test_hostcmd_refuses_other_vectors(argv):
    inventory = {"whole": {"disk0", "disk6"}, "members": {"disk0", "disk0s1", "disk6", "disk6s2"}}
    assert not hostcmd.valid_argv(argv, physical_disks=inventory)
    with pytest.raises(ValueError):
        await hostcmd.run(argv, physical_disks=inventory)


def test_inventory_only_grants_physical_children():
    whole, members = hostcmd.physical_inventory(
        {
            "AllDisksAndPartitions": [
                {
                    "DeviceIdentifier": "disk1",
                    "Partitions": [
                        {"DeviceIdentifier": "disk1s2"},
                        {"DeviceIdentifier": "disk42s1"},
                        {"DeviceIdentifier": "disk1s2;bad"},
                    ],
                },
                {"DeviceIdentifier": "disk0;bad"},
            ]
        }
    )
    assert whole == {"disk1"} and members == {"disk1", "disk1s2"}
    inventory = {"whole": whole, "members": members}
    assert hostcmd.valid_argv(
        [hostcmd.DISKUTIL, "info", "-plist", "disk1s2"], physical_disks=inventory
    )
    assert not hostcmd.valid_argv([hostcmd.DISKUTIL, "info", "-plist", "disk1s2"])


def test_trend_least_squares_and_non_growth():
    now = 1_000_000_000
    points = [(now - n * DAY, 10000 - n * 100) for n in range(7, -1, -1)]
    result = trend(points, 20000, 10000, now)
    assert result["bytes_per_day"] == 100
    assert result["days_to_full"] == 100
    assert trend([(now - 2 * DAY, 300), (now, 100)], 1000, 900, now)["days_to_full"] is None
    assert trend([(now - DAY, 100), (now, 100)], 1000, 900, now)["bytes_per_day"] == 0
    assert trend([(now, 100)], 1000, 900, now)["days_to_full"] is None
    assert trend([(now - 10 * DAY, 999999), *points], 20000, 10000, now) == result
    assert trend([(now - 10, 1), (now, 10)], 1000, 990, now)["bytes_per_day"] is None


async def test_hourly_retention_and_restart_cache(disks):
    core, health, calls, _, _ = disks
    now = 100 * DAY
    result = await health.status(now=now)
    label = result["containers"][0]["label"]
    for n in range(36 * 24):
        health.sample(result["containers"], now + n * HOUR)
    latest = now + (36 * 24 - 1) * HOUR
    history = health.history(label, 35, now=latest)
    assert len(history["points"]) == MAX_POINTS
    assert history["points"][0]["sampled_at"] >= latest - 35 * DAY
    total = health.db.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0]
    assert total <= 4 * (MAX_POINTS + 1)
    new = DiskHealth(core)
    new.runner, new.mounts, new.statvfs = health.runner, health.mounts, health.statvfs
    new.important_free = health.important_free
    count = len([c for c in calls if c[0] == hostcmd.SMARTCTL])
    await new.status(now=now + 5 * HOUR)
    assert len([c for c in calls if c[0] == hostcmd.SMARTCTL]) == count
    await new.status(now=now + 6 * HOUR)
    assert len([c for c in calls if c[0] == hostcmd.SMARTCTL]) == count + 4
    # Persistence is in the shared state database, including across reopen.
    storage = Storage(core.config.state_dir)
    assert storage.db.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0] > 0
    storage.close()


async def test_alerts_hysteresis_silence_and_monitor_preservation(disks):
    core, health, _, _, _ = disks
    core.config.monitor["enabled"] = True
    queued = []
    core.alerts.queue = lambda *args: queued.append(args) or True
    result = await health.status(now=100000)
    archive = next(c for c in result["containers"] if c["container_id"] == "disk10")
    name = "storage:" + archive["label"]
    assert len(result["alerts"]) == 1 and result["alerts"][0]["status"] == "down"
    assert queued and "95.0% used" in queued[0][1]["body"]
    # Model a delivered alert; silencing only queued notices suppresses recovery.
    with health.db:
        health.db.execute("UPDATE monitor_state SET notified=1 WHERE name=?", (name,))
    assert len([i for i in core.monitor.status()["open_incidents"] if i["name"] == name]) == 1
    core.monitor.process([], 100010)
    assert health.alerts("mac")[0]["name"] == name
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("ops:silence"))
    silence = await core.call(
        "codex", "ops_alerts_silence", {"probe": name, "minutes": 15, "reason": "Planned disk work"}
    )
    assert silence["status"] == "ok", silence
    assert (await core.call("cortex", "ops_alerts_status", {}))["status"] == "ok"
    row = copy.deepcopy(archive)
    for percent, expected in ((90, "degraded"), (89, "degraded"), (88, "up"), (87.9, "up")):
        row["used_percent"] = percent
        health.evaluate({"containers": [row], "disks": []}, 100020)
        state = health.db.execute("SELECT * FROM monitor_state WHERE name=?", (name,)).fetchone()
        assert state["status"] == expected
    assert not health.alerts("mac")
    assert not core.monitor.status()["silences"]
    assert queued[-1][1]["kind"] == "recovery"
    with pytest.raises(CapabilityError):
        core.monitor.validate_probe("storage:invented")
    with pytest.raises(CapabilityError):
        core.monitor.validate_probe("smart:disk123")


async def test_smart_increases_persist_through_standby_and_refresh(disks):
    core, health, calls, replies, _ = disks
    core.config.monitor["enabled"] = True
    core.alerts.queue = lambda *_: True
    first = await health.status(now=100000)
    smart6 = "smart:" + first["disks"][1]["health_id"]
    replies["disk6"]["ata_smart_attributes"]["table"][0]["raw"]["value"] = 1
    result = await health.status(refresh=True, now=100010)
    assert "reallocated increased" in result["disks"][1]["smart"]["warnings"]
    assert any(a["name"] == smart6 for a in result["alerts"])
    replies["disk6"] = fixture("standby")
    result = await health.status(refresh=True, now=100020)
    assert result["disks"][1]["smart"]["state"] == "standby"
    assert any(a["name"] == smart6 for a in result["alerts"])
    replies["disk6"] = fixture("ata")
    replies["disk6"]["ata_smart_attributes"]["table"][0]["raw"]["value"] = 2
    result = await health.status(refresh=True, now=100030)
    assert "reallocated increased" in result["disks"][1]["smart"]["warnings"]
    await health.status(now=100040)
    assert any(a["name"] == smart6 for a in health.alerts())
    result = await health.status(refresh=True, now=100050)
    assert not any(a["name"] == smart6 for a in result["alerts"])


async def test_smart_faults_temperature_and_nvme(disks):
    core, health, _, replies, _ = disks
    core.config.monitor["enabled"] = True
    core.alerts.queue = lambda *_: True
    await health.status(now=100000)
    replies["disk0"]["nvme_smart_health_information_log"].update(percentage_used=90, media_errors=1)
    replies["disk6"]["smart_status"]["passed"] = False
    replies["disk6"]["smartctl"]["exit_status"] = 8
    replies["disk6"]["temperature"]["current"] = 55
    replies["disk6"]["ata_smart_attributes"]["table"][1]["raw"]["value"] = 1
    result = await health.status(refresh=True, now=100010)
    nvme, ata = result["disks"][:2]
    assert "media_errors increased" in nvme["smart"]["warnings"]
    assert "NVMe endurance threshold reached" in nvme["smart"]["warnings"]
    assert ata["smart"]["passed"] is False
    assert "Drive temperature threshold reached" in ata["smart"]["warnings"]
    assert "pending increased" in ata["smart"]["warnings"]
    assert len(result["alerts"]) == 3


async def test_forecast_warns_below_fill_threshold(disks):
    core, health, _, _, _ = disks
    core.config.monitor["enabled"] = True
    core.alerts.queue = lambda *_: True
    result = await health.status(now=100 * DAY)
    row = next(c for c in result["containers"] if c["container_id"] == "disk8")
    with health.db:
        health.db.execute(
            "INSERT INTO storage_samples VALUES (?,?,?,?,?)",
            (row["label"], 99 * 24, 99 * DAY, row["used_bytes"] - 30 * 1024**3, row["total_bytes"]),
        )
    result = await health.status(now=100 * DAY + 1)
    row = next(c for c in result["containers"] if c["container_id"] == "disk8")
    assert row["trend"]["days_to_full"] == 5
    assert any(a["name"] == "storage:" + row["label"] for a in result["alerts"])


async def test_denials_disabled_invalid_and_unavailable(disks):
    core, health, calls, _, _ = disks
    for name, args in [
        ("storage_status", {}),
        ("storage_history", {"label": label_for("mac", "x")}),
    ]:
        result = await core.call("agy", name, args)
        assert result["error"]["code"] == "scope_denied"
    assert not calls
    for args in ({"host": "unknown"}, {"refresh": "yes"}, {"arbitrary": "value"}):
        assert (await core.call("cortex", "storage_status", args))["error"][
            "code"
        ] == "invalid_arguments"
    assert not calls
    assert (await core.call("cortex", "storage_history", {"label": label_for("mac", "x")}))[
        "outcome"
    ] == "not_found"
    for days in (0, 36, True):
        assert (
            await core.call(
                "cortex", "storage_history", {"label": label_for("mac", "x"), "days": days}
            )
        )["error"]["code"] == "invalid_arguments"
    core.config.storage["enabled"] = False
    assert (await core.call("cortex", "storage_status", {}))["error"]["code"] == "backend_disabled"
    core.config.storage["enabled"] = True

    async def unavailable(*_, **__):
        return -1, "", "PRIVATE stderr"

    health.runner = unavailable
    result = await core.call("cortex", "storage_status", {})
    assert result["error"]["code"] == "backend_down" and "PRIVATE" not in json.dumps(result)
    assert not health.db.execute("SELECT * FROM storage_samples").fetchall()


async def test_background_sampling_when_monitor_disabled(disks, monkeypatch):
    core, health, calls, _, _ = disks
    core.config.monitor["enabled"] = False
    await health.maintain()
    count = len(calls)
    await health.maintain()
    assert len(calls) == count
    assert not health.alerts()
    assert health.db.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0] == 4


async def test_remote_fill_reuses_snapshot(disks, monkeypatch):
    from switchboard import remote

    core, health, calls, _, _ = disks
    core.config.remote_hosts = {"remotehost": {}}
    requests = []

    async def snapshot(c, host, request):
        requests.append((host, request))
        return {
            "ok": True,
            "disks": [
                {
                    "name": "/",
                    "total_gb": 32,
                    "free_gb": 8,
                    "used_percent": 75,
                    "serial_number": "DO-NOT-EXPOSE",
                }
            ],
        }

    monkeypatch.setattr(remote, "call", snapshot)
    result = await core.call("cortex", "storage_status", {"host": "remotehost", "refresh": True})
    assert result["status"] == "ok", result
    assert requests == [("remotehost", "snapshot")]
    assert not calls and result["disks"] == []
    assert result["containers"][0]["used_bytes"] == 24 * 1024**3
    assert "DO-NOT-EXPOSE" not in json.dumps(result)
    assert (
        await core.call("cortex", "storage_history", {"label": result["containers"][0]["label"]})
    )["status"] == "ok"


@pytest.mark.parametrize(
    "value",
    [
        {"enabled": 1},
        {"warn_percent": float("nan")},
        {"critical_percent": 89},
        {"hysteresis_percent": 90},
        {"temperature_c": True},
        {"command": "anything"},
    ],
)
def test_storage_config_rejects_invalid(config, value):
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"storage": value}))
    with pytest.raises(ValueError):
        Config.load()


def test_storage_config_load(config):
    (config.config_dir / "config.yaml").write_text(
        "storage:\n  enabled: true\n  warn_percent: 88\n"
    )
    assert Config.load().storage["warn_percent"] == 88
    assert Config.load().storage["enabled"] is True


def test_native_mount_adapter_uses_nowait_and_fixed_library(monkeypatch):
    import ctypes

    from switchboard import mounts

    values = (mounts.StatFS * 2)()
    values[0].device = b"/dev/disk3s5"
    values[0].mount = b"/System/Volumes/Data"
    values[0].fs = b"apfs"
    values[1].device = b"/dev/disk12s1"
    values[1].mount = b"/Volumes/Installer"
    values[1].fs = b"hfs"
    values[1].flags = 1
    calls = []

    def getmntinfo(pointer, flags):
        calls.append(flags)
        target = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(mounts.StatFS)))
        target[0] = ctypes.cast(values, ctypes.POINTER(mounts.StatFS))
        return 2

    def library(path, **kwargs):
        assert path == "/usr/lib/libSystem.B.dylib"
        return SimpleNamespace(getmntinfo=getmntinfo)

    monkeypatch.setattr(mounts.sys, "platform", "darwin")
    monkeypatch.setattr(mounts.ctypes, "CDLL", library)
    rows = mounts.mounted_volumes()
    assert calls == [2]
    assert rows[0]["mount"] == "/System/Volumes/Data"
    assert rows[1]["readonly"] is True


async def test_smart_silences_quota_and_reminders_use_existing_pipeline(disks):
    core, health, _, replies, _ = disks
    core.config.monitor["enabled"] = True
    queued = []
    core.alerts.queue = lambda *args: queued.append(args) or True
    first = await health.status(now=100000)
    smart6 = "smart:" + first["disks"][1]["health_id"]
    replies["disk6"]["smart_status"]["passed"] = False
    await health.status(refresh=True, now=100010)
    assert smart6 in health.probe_names()
    core.monitor.silence(smart6, 1, "Planned disk work", "codex", now=100011)
    count = len(queued)
    await health.status(now=100020)
    assert len(queued) == count
    await health.status(now=100072)
    assert len(queued) == count + 1
    await health.status(now=100072 + 6 * HOUR)
    assert not any(item[1]["kind"] == "reminder" for item in queued)  # Storage nags daily.
    await health.status(now=100072 + 24 * HOUR)
    assert any(item[1]["kind"] == "reminder" for item in queued)
    names = sorted(health.probe_names())
    for name in names[:3]:
        core.monitor.silence(name, 15, "Planned disk work", "codex", now=200000)
    with pytest.raises(CapabilityError) as error:
        core.monitor.silence(names[3], 15, "Planned disk work", "codex", now=200001)
    assert error.value.code == "silence_limit"
    with pytest.raises(CapabilityError) as error:
        core.monitor.silence("all", 15, "Host work", "codex", now=200001)
    assert error.value.code == "silence_forbidden"


async def test_concurrent_reads_serialize_smart_refresh(disks):
    core, health, calls, _, _ = disks
    import asyncio

    results = await asyncio.gather(*(health.status(now=100000) for _ in range(3)))
    assert all(r["status"] == "ok" for r in results)
    assert len([c for c in calls if c[0] == hostcmd.SMARTCTL]) == 4
    assert health.db.execute("SELECT COUNT(*) FROM storage_samples").fetchone()[0] == 4


async def test_mount_rename_keeps_history_label_and_sanitizes_names(disks):
    core, health, _, _, mounts = disks
    before = await health.status(now=100000)
    for mount in mounts:
        if mount["mount"] == "/Volumes/archive":
            mount["mount"] = "/Volumes/new name"
    after = await health.status(now=100001)
    assert {c["label"] for c in before["containers"]} == {c["label"] for c in after["containers"]}


async def test_storage_doctor_missing_smartctl(disks, monkeypatch):
    core, _, _, _, _ = disks
    import switchboard.doctor as doctor

    original = doctor.os.access
    monkeypatch.setattr(
        doctor.os,
        "access",
        lambda path, mode: False if path == hostcmd.SMARTCTL else original(path, mode),
    )
    result = await core.call("cortex", "ops_switchboard_status", {})
    assert result["checks"]["storage"]["status"] == "degraded", result
    core.config.storage["enabled"] = False
    result = await core.call("cortex", "ops_switchboard_status", {})
    assert result["checks"]["storage"]["status"] == "disabled"


async def test_drive_model_and_health_id_survive_usb_bridges(disks):
    _, health, _, _, _ = disks
    result = await health.status(refresh=True, now=100000)
    usb = result["disks"][1]
    assert usb["model"] == usb["smart"]["model"]  # the drive, not the enclosure's bridge chip
    assert usb["health_id"].startswith("disk-") and usb["health_id"] != usb["id"]


def test_smart_exit_bits_warn_on_present_failures_and_note_the_past():
    from switchboard.disk_health import smart_reading

    data = {"smart_status": {"passed": True}, "temperature": {"current": 0}}
    past = smart_reading(data, 32)
    assert past["state"] == "ok" and past["warnings"] == [] and past["notes"]
    assert past["temperature_c"] == 0
    now = smart_reading(data, 16 | 64)
    assert len(now["warnings"]) == 2 and now["state"] == "ok"
    assert smart_reading(data, 4)["state"] == "unsupported"


async def test_purgeable_space_counts_as_free(disks):
    _, health, _, _, _ = disks
    baseline = await health.status(refresh=True, now=100000)
    data = next(c for c in baseline["containers"] if c["mount"] == "/System/Volumes/Data")
    assert data["purgeable_bytes"] == 0
    health.important_free = lambda path: (
        data["free_bytes"] + 50 * 1024**3 if path == "/System/Volumes/Data" else None
    )
    result = await health.status(refresh=True, now=100001)
    after = next(c for c in result["containers"] if c["mount"] == "/System/Volumes/Data")
    assert after["purgeable_bytes"] == 50 * 1024**3
    assert after["free_bytes"] == data["free_bytes"] + 50 * 1024**3
    assert after["used_percent"] < data["used_percent"]


def test_important_free_reads_this_mac_or_returns_none():
    from switchboard.mounts import important_free

    value = important_free("/")
    assert value is None or value > 0
    assert important_free("/no/such/volume\0") is None
