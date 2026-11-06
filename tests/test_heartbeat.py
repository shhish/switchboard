"""Dead-man delivery uses only a fake EventKit helper and temporary private files."""

import asyncio
import json
import time

import pytest
import yaml

from switchboard.alert_config import heartbeat_url, monitor_settings
from switchboard.config import Config
from switchboard.doctor import diagnose
from switchboard.errors import CapabilityError
from switchboard.monitor import heartbeat_status

from .net_helpers import ip


@pytest.mark.parametrize(
    "url",
    [
        ("http://" + ip(192, 168, 50, 2) + "/api/push/private"),
        ("https://" + ip(10, 0, 0, 1) + ":443/ping"),
        "http://172.31.255.255/",
        ("http://" + ip(192, 168, 50, 2) + "/") + "a" * 199,
    ],
)
def test_heartbeat_valid_urls(url):
    assert heartbeat_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/ping",
        "http://[::1]/ping",
        ("http://" + ip(100, 64, 0, 1) + "/ping"),
        "http://8.8.8.8/ping",
        "https://kuma.lan/ping",
        ("ftp://" + ip(192, 168, 50, 2) + "/ping"),
        ("http://" + ip(192, 168, 50, 2) + "/?token=PRIVATE"),
        ("http://" + ip(192, 168, 50, 2) + "/?"),
        ("http://" + ip(192, 168, 50, 2) + "/#"),
        ("http://user@" + ip(192, 168, 50, 2) + "/ping"),
        ("http://user:PRIVATE@" + ip(192, 168, 50, 2) + "/ping"),
        ("http://" + ip(192, 168, 50, 2) + "/p ing"),
        ("http://" + ip(192, 168, 50, 2) + "/p\ning"),
        ("http://" + ip(192, 168, 50, 2) + "/") + "a" * 200,
        ("http://" + ip(192, 168, 50, 2) + "/") + "é" * 100,
        ("http://" + ip(192, 168, 50, 2) + ":0/ping"),
        ("http://" + ip(192, 168, 50, 2) + ":bad/ping"),
        ("http://" + ip(192, 168, "050", 2) + "/ping"),
        ("http://" + ip(192, 168, 50, 2) + "\\@evil.test/"),
        None,
    ],
)
def test_heartbeat_invalid_urls_do_not_echo_target(url):
    with pytest.raises(ValueError) as exc:
        heartbeat_url(url)
    assert "PRIVATE" not in str(exc.value)


def test_heartbeat_config_lazy_and_closed(config):
    path = str(config.config_dir / "not-provisioned.url")
    settings = monitor_settings({"heartbeat_url_file": path})
    assert settings["heartbeat_url_file"] == path
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"monitor": settings}))
    assert Config.load().monitor["heartbeat_url_file"] == path
    for bad in (
        ("http://" + ip(192, 168, 50, 2) + "/PRIVATE"),
        "~/secret",
        "relative",
        "",
        1,
        None,
        "/tmp/a\n",
    ):
        with pytest.raises(ValueError):
            monitor_settings({"heartbeat_url_file": bad})
    with pytest.raises(ValueError):
        monitor_settings({"heartbeat_url": ("http://" + ip(192, 168, 50, 2) + "/PRIVATE")})


@pytest.fixture
async def heartbeat(core, monkeypatch):
    url = "http://" + ip(192, 168, 50, 2) + "/api/push/heartbeat-private-value"
    path = core.config.config_dir / "heartbeat.url"
    path.write_text(url + "\n")
    path.chmod(0o600)
    core.config.monitor["heartbeat_url_file"] = str(path)
    core.config.backends["eventkit"]["enabled"] = True
    calls = []
    reply = [{"http_status": 200, "latency_ms": 1}]

    class Helper:
        def __init__(self, config, timeout):
            assert config is core.config and timeout == 4

        async def call(self, method, args):
            # Prove processing committed before the ping.
            assert core.monitor.status()["last_cycle_at"] is not None
            assert method == "net.probe" and args == {"url": url}
            calls.append(True)
            value = reply[0]
            if isinstance(value, Exception):
                raise value
            return value

    async def probes():
        return {"services": []}

    monkeypatch.setattr(core.probes, "run", probes)
    monkeypatch.setattr("switchboard.monitor.EventKit", Helper)
    return core, path, url, calls, reply


async def test_heartbeat_each_completed_cycle_persists_status_and_never_url(heartbeat, caplog):
    core, _, url, calls, _ = heartbeat
    await core.monitor.cycle()
    await core.monitor.cycle()
    assert len(calls) == 2
    status = await core.call("dot", "ops_alerts_status", {})
    assert status["heartbeat"]["configured"] and status["heartbeat"]["last_ok"]
    assert time.time() - status["heartbeat"]["last_at"] < 2
    # Doctor's normal EventKit backend probe is separately disabled to keep it fake-only.
    core.config.backends["eventkit"]["enabled"] = False
    doctor = await core.call("dot", "ops_switchboard_status", {})
    assert doctor["checks"]["heartbeat"]["status"] == "ok"
    meta = dict(core.storage.db.execute("SELECT key, value FROM monitor_meta"))
    assert meta["last_heartbeat_ok"] == 1
    for value in (status, doctor, meta, core.audit.tail()):
        assert url not in json.dumps(value) and "heartbeat-private-value" not in json.dumps(value)
    assert url not in caplog.text
    for path in core.config.log_dir.glob("*"):
        if path.is_file():
            assert url not in path.read_text()


@pytest.mark.parametrize(
    "result",
    [
        TimeoutError("PRIVATE"),
        CapabilityError("backend_down", "PRIVATE"),
        RuntimeError("PRIVATE"),
        {"error": "timeout"},
        {"local_network_denied": True},
        {"http_status": 500},
        {"http_status": 302},
        {"http_status": "200"},
        {"http_status": True},
        None,
    ],
)
async def test_failed_heartbeat_does_not_break_cycle(heartbeat, result, caplog):
    core, _, url, calls, reply = heartbeat
    reply[0] = result
    await core.monitor.cycle()
    assert calls == [True]
    status = await core.call("dot", "ops_alerts_status", {})
    assert status["last_cycle_at"] is not None and status["heartbeat"]["last_ok"] is False
    assert "PRIVATE" not in json.dumps(status) + caplog.text
    assert url not in json.dumps(status) + caplog.text
    core.config.backends["eventkit"]["enabled"] = False
    assert (await core.call("dot", "ops_switchboard_status", {}))["checks"]["heartbeat"][
        "status"
    ] == "degraded"
    # A later completed cycle retries and can recover.
    reply[0] = {"http_status": 204}
    core.config.backends["eventkit"]["enabled"] = True
    await core.monitor.cycle()
    assert len(calls) == 2 and core.monitor.status()["heartbeat"]["last_ok"]


@pytest.mark.parametrize("failure", ["mode", "missing", "symlink", "url", "large", "multi"])
async def test_heartbeat_private_file_and_target_validation(heartbeat, failure, caplog):
    core, path, url, calls, _ = heartbeat
    if failure == "mode":
        path.chmod(0o644)
    elif failure == "missing":
        path.unlink()
    elif failure == "symlink":
        target = path.with_suffix(".target")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.write_text(
            {"url": "https://evil.test/PRIVATE", "large": url * 100, "multi": url + "\n" + url}[
                failure
            ]
        )
    await core.monitor.cycle()
    assert not calls and core.monitor.status()["heartbeat"]["last_ok"] is False
    assert "PRIVATE" not in caplog.text and url not in caplog.text


async def test_heartbeat_disabled_backend_or_unconfigured_does_not_send(heartbeat):
    core, _, _, calls, _ = heartbeat
    core.config.backends["eventkit"]["enabled"] = False
    await core.monitor.cycle()
    assert not calls and core.monitor.status()["heartbeat"]["last_at"] is None
    core.config.monitor.pop("heartbeat_url_file")
    core.config.backends["eventkit"]["enabled"] = True
    await core.monitor.cycle()
    assert not calls and core.monitor.status()["heartbeat"] == {
        "configured": False,
        "last_at": None,
        "last_ok": None,
    }


async def test_incomplete_cycle_does_not_send_heartbeat(heartbeat, monkeypatch):
    core, _, _, calls, _ = heartbeat

    async def broken():
        raise RuntimeError("fake failure")

    monkeypatch.setattr(core.probes, "run", broken)
    with pytest.raises(RuntimeError):
        await core.monitor.cycle()
    assert not calls


async def test_heartbeat_timeout_is_bounded_and_cancellation_propagates(heartbeat, monkeypatch):
    core, _, _, _, _ = heartbeat
    entered = asyncio.Event()

    class Hanging:
        def __init__(self, *args, **kwargs):
            pass

        async def call(self, *args):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("switchboard.monitor.EventKit", Hanging)
    original = asyncio.timeout
    with monkeypatch.context() as context:

        def timeout(seconds):
            assert seconds == 5
            return original(0.01)

        context.setattr("switchboard.monitor.asyncio.timeout", timeout)
        await core.monitor.cycle()
    assert core.monitor.status()["heartbeat"]["last_ok"] is False
    task = asyncio.create_task(core.monitor.cycle())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_heartbeat_doctor_age_and_cli_persistence(heartbeat, monkeypatch):
    core, _, _, _, _ = heartbeat
    core.config.backends["eventkit"]["enabled"] = False
    assert (await core.call("dot", "ops_switchboard_status", {}))["checks"]["heartbeat"][
        "status"
    ] == "degraded"
    with core.storage.db:
        core.storage.db.executemany(
            "INSERT OR REPLACE INTO monitor_meta VALUES (?, ?)",
            [
                ("last_heartbeat_at", 1000),
                ("last_heartbeat_ok", 1),
            ],
        )
    monkeypatch.setattr("switchboard.doctor.time.time", lambda: 1299)
    doctor = await diagnose(core.config, core.backends, core.policy, core.orb)
    assert doctor["checks"]["heartbeat"]["status"] == "ok"
    assert heartbeat_status(core.config, core.monitor.db)["last_ok"]
    monkeypatch.setattr("switchboard.doctor.time.time", lambda: 1300)
    assert (await core.call("dot", "ops_switchboard_status", {}))["checks"]["heartbeat"][
        "status"
    ] == "degraded"
    core.config.monitor.pop("heartbeat_url_file")
    assert (await core.call("dot", "ops_switchboard_status", {}))["checks"]["heartbeat"][
        "status"
    ] == "disabled"


async def test_heartbeat_storage_failure_does_not_raise_into_cycle(heartbeat, caplog):
    import sqlite3

    core, _, url, calls, _ = heartbeat
    db = core.monitor.db

    class FailingMeta:
        def __enter__(self):
            db.__enter__()
            return self

        def __exit__(self, *args):
            return db.__exit__(*args)

        def execute(self, *args):
            return db.execute(*args)

        def executemany(self, *args):
            raise sqlite3.OperationalError(url)

    core.monitor.db = FailingMeta()
    await core.monitor.cycle()
    assert calls == [True] and core.monitor.status()["last_cycle_at"] is not None
    assert url not in caplog.text
