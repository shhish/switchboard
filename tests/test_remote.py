"""Pi contracts use fake SSH runners and private temporary files only."""

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from switchboard import hostcmd, remote
from switchboard.config import Config, validate_probes, validate_remote_hosts, validate_services
from switchboard.errors import CapabilityError

from .conftest import change_policy
from .net_helpers import ip

HOST = {
    "address": ip(192, 168, 50, 2),
    "user": "switchboard",
    "key_file": "/missing/ssh_key",
    "known_hosts_file": "/missing/known_hosts",
}


@pytest.fixture
async def pi(core, tmp_path):
    host = dict(HOST)
    for key in ("key_file", "known_hosts_file"):
        path = tmp_path / key
        path.write_text("fake private file")
        path.chmod(0o600)
        host[key] = str(path)
    core.config.remote_hosts = validate_remote_hosts({"remotehost": host})
    core.config.probes = validate_probes(
        [{"name": "homebridge", "url": ("http://" + ip(192, 168, 50, 2) + ":8581/")}]
    )
    core.config.services = validate_services(
        {
            "homebridge": {
                "runtime": {"remotehost": "homebridge"},
                "probe": "homebridge",
                "restart": True,
            },
            "adguard": {"runtime": {"remotehost": "adguard"}, "restart": True},
            "tailscale": {"runtime": {"remotehost": "tailscale"}},
        },
        core.config.probes,
        core.config.remote_hosts,
    )
    change_policy(
        core.config,
        lambda p: p["clients"]["codex"]["scopes"].extend(
            ["ops:diagnose", "ops:restart", "ops:silence"]
        ),
    )
    calls, replies = [], {}

    async def runner(argv, **kwargs):
        assert hostcmd.valid_argv(argv, core.config.remote_hosts), argv
        calls.append((argv, kwargs))
        if argv[0] != hostcmd.SSH:
            return 0, "42 99 99999 /bin/mac\n", ""
        assert kwargs["remote_hosts"] == core.config.remote_hosts
        expected = remote.TIMEOUTS.get(argv[-1].split(" ")[0], 20)
        assert kwargs["timeout_s"] == expected
        value = replies.get((argv[-2], argv[-1]), replies.get(argv[-1], {"ok": True}))
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return value
        return 0, json.dumps(value), "PRIVATE stderr"

    core.services.runner = runner
    return core, calls, replies


def ssh_calls(calls):
    return [argv[-1] for argv, _ in calls if argv[0] == hostcmd.SSH]


def running(name="homebridge", **kwargs):
    return {"name": name, "running": True, "pid": 42, "state": "active/running", **kwargs}


def test_remote_config_load_is_lazy(config):
    data = {
        "remote_hosts": {"remotehost": HOST},
        "services": {"homebridge": {"runtime": {"remotehost": "homebridge"}, "restart": True}},
    }
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    loaded = Config.load()
    assert loaded.remote_hosts == {"remotehost": HOST}
    assert loaded.services["homebridge"]["logs"] == "remote"
    assert (
        Config(config.config_dir, config.state_dir, config.log_dir, **data).services
        == loaded.services
    )


@pytest.mark.parametrize(
    "address",
    [
        ip(10, 0, 0, 1),
        "172.16.0.1",
        "172.31.255.255",
        ip(192, 168, 50, 2),
        ip(100, 64, 0, 1),
        ip(100, 127, 255, 255),
    ],
)
def test_private_remote_addresses(address):
    assert validate_remote_hosts({"remotehost": {**HOST, "address": address}})


@pytest.mark.parametrize(
    "hosts",
    [
        None,
        [],
        {str(i): HOST for i in range(9)},
        {"BAD": HOST},
        {"a" * 41: HOST},
        {"mac": HOST},
        {"docker": HOST},
        {"remotehost": {}},
        {"remotehost": {**HOST, "shell": "sh"}},
        *[
            {"remotehost": {**HOST, "address": address}}
            for address in (
                "8.8.8.8",
                "127.0.0.1",
                "172.32.0.1",
                "100.128.0.1",
                "169.254.1.1",
                "::1",
                "remotehost.lan",
                ip(192, 168, "050", 2),
                (ip(192, 168, 50, 2) + ";id"),
                123,
                None,
            )
        ],
        *[
            {"remotehost": {**HOST, "user": user}}
            for user in ("root@x", "-root", "ROOT", "a b", "a" * 33, 3)
        ],
        *[
            {"remotehost": {**HOST, key: value}}
            for key in ("key_file", "known_hosts_file")
            for value in ("relative", "~/key", "/key\x00", "/key\n", None)
        ],
    ],
)
def test_bad_remote_hosts(hosts):
    with pytest.raises(ValueError):
        validate_remote_hosts(hosts)


@pytest.mark.parametrize(
    "setting",
    [
        {"runtime": {"missing": "homebridge"}},
        {"runtime": {"remotehost": "a b"}},
        {"runtime": {"remotehost": "A"}},
        {"runtime": {"remotehost": "x" * 41}},
        {"runtime": {"remotehost": "homebridge"}, "logs": ["remote"]},
        {"runtime": {"remotehost": "homebridge"}, "logs": []},
        {"runtime": {"remotehost": "homebridge"}, "logs": "/tmp/log"},
        {"runtime": {"remotehost": "homebridge"}, "logs": ["docker"]},
        {"runtime": {"docker": "homebridge"}, "logs": "remote"},
    ],
)
def test_bad_remote_services(setting):
    with pytest.raises(ValueError):
        validate_services({"x": setting}, [], {"remotehost": HOST})


@pytest.mark.parametrize(
    "ssh_request",
    [
        "version",
        "snapshot",
        "checks",
        "status",
        "status homebridge",
        "restart nginx",
        "logs kuma 400",
    ],
)
def test_ssh_exact_vector(ssh_request):
    argv = hostcmd.ssh_argv(HOST, ssh_request)
    assert argv == [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-i",
        HOST["key_file"],
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + HOST["known_hosts_file"],
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "LogLevel=ERROR",
        ("switchboard@" + ip(192, 168, 50, 2)),
        ssh_request,
    ]
    assert hostcmd.valid_argv(argv, {"remotehost": HOST})
    assert not hostcmd.valid_argv(argv)  # No configured trust root, no SSH.
    for index in range(len(argv)):
        bad = argv.copy()
        bad[index] += "!"
        assert not hostcmd.valid_argv(bad, {"remotehost": HOST}), index
    for bad in [argv + ["extra"], argv[:-1], [*argv[:5], *argv[7:9], *argv[5:7], *argv[9:]]]:
        assert not hostcmd.valid_argv(bad, {"remotehost": HOST})


@pytest.mark.parametrize(
    "ssh_request",
    [
        "",
        "id",
        "status Homebridge",
        "status x;id",
        "status $(id)",
        "status x\ny",
        "status x\t",
        "status  x",
        " status",
        "status ",
        "status x y",
        "version x",
        "restart",
        "logs kuma 0",
        "logs kuma 401",
        "logs kuma 1 extra",
        "status " + "x" * 41,
    ],
)
async def test_ssh_bad_ssh_requests_never_spawn(ssh_request):
    argv = hostcmd.ssh_argv(HOST, ssh_request)
    assert not hostcmd.valid_argv(argv, {"remotehost": HOST})
    with pytest.raises(ValueError):
        await hostcmd.run(argv, remote_hosts={"remotehost": HOST})


@pytest.mark.parametrize(
    "reply,expected",
    [
        ((255, "PRIVATE", "PRIVATE"), "backend_down"),
        ((-1, "", "PRIVATE"), "backend_down"),
        (TimeoutError("PRIVATE"), "backend_down"),
        (OSError("PRIVATE"), "backend_down"),
        ((0, "PRIVATE", "PRIVATE"), "backend_invalid"),
        ((0, "[]", "PRIVATE"), "backend_invalid"),
        ({"ok": 1}, "backend_invalid"),
        ({}, "backend_invalid"),
        ((3, '{"ok":false,"code":"not_allowed","error":"PRIVATE"}', "PRIVATE"), "resource_denied"),
        ({"ok": False, "error": "unsupported request"}, "invalid_arguments"),
        ({"ok": False, "error": "unknown service"}, "invalid_arguments"),
        ({"ok": False, "error": "usage: logs <service> <lines>"}, "invalid_arguments"),
        ({"ok": False, "error": "PRIVATE"}, "backend_error"),
        ({"ok": False, "error": {}}, "backend_error"),
        ((2, '{"ok":true}', "PRIVATE"), "backend_error"),
    ],
)
async def test_remote_error_mapping(pi, reply, expected):
    core, _, replies = pi
    replies["version"] = reply
    with pytest.raises(CapabilityError) as exc:
        await remote.call(core, "remotehost", "version")
    assert exc.value.code == expected
    assert "PRIVATE" not in str(exc.value)
    if expected == "backend_down":
        assert str(exc.value) == "remotehost unreachable"
        assert exc.value.result()["error"]["retryable"]


@pytest.mark.parametrize("field", ["key_file", "known_hosts_file"])
@pytest.mark.parametrize("failure", ["missing", "mode", "symlink", "directory", "owner"])
async def test_remote_files_checked_at_use(pi, field, failure, monkeypatch):
    core, calls, _ = pi
    path = Path(core.config.remote_hosts["remotehost"][field])
    if failure == "missing":
        await asyncio.to_thread(path.unlink)
    elif failure == "mode":
        await asyncio.to_thread(path.chmod, 0o644)
    elif failure in ("symlink", "directory"):
        await asyncio.to_thread(path.unlink)
        if failure == "symlink":
            target = path.with_suffix(".target")
            target.write_text("fake")
            target.chmod(0o600)
            await asyncio.to_thread(path.symlink_to, target)
        else:
            await asyncio.to_thread(path.mkdir, mode=0o600)
    else:
        original = os.stat

        def wrong_owner(file, **kwargs):
            info = original(file, **kwargs)
            if str(file) == str(path):
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
            return info

        monkeypatch.setattr(remote.os, "stat", wrong_owner)
    with pytest.raises(CapabilityError) as exc:
        await remote.call(core, "remotehost", "version")
    assert exc.value.code == "resource_denied" and not calls


async def test_status_batch_and_remote_metrics_do_not_use_mac_pid(pi):
    core, calls, replies = pi
    replies["status"] = {
        "ok": True,
        "services": [
            running(restart_count=4, started_at="today", rss_mb=12, docker_health="healthy"),
            running("adguard", running=False, state="inactive/dead"),
            running("tailscale"),
        ],
    }
    result = await core.call("codex", "ops_service_status", {})
    rows = {r["name"]: r for r in result["services"]}
    assert ssh_calls(calls) == ["status"]
    assert rows["homebridge"]["runtime"] == "remote" and rows["homebridge"]["host"] == "remotehost"
    assert rows["homebridge"]["state"] == "active/running"
    assert rows["homebridge"]["restart_count"] == 4
    assert rows["homebridge"]["started_at"] == "today"
    assert rows["homebridge"]["rss_mb"] == 12 and "cpu_percent" not in rows["homebridge"]
    assert rows["homebridge"]["docker_health"] == "healthy"
    assert rows["adguard"]["status"] == "stopped"
    calls.clear()
    await core.call("codex", "ops_service_status", {"service": "homebridge"})
    assert ssh_calls(calls) == ["status"]


async def test_unreachable_host_isolated_from_other_hosts_and_mac(pi):
    core, calls, replies = pi
    core.config.remote_hosts["other"] = {
        **core.config.remote_hosts["remotehost"],
        "address": ip(10, 0, 0, 2),
    }
    core.config.services.update(
        other={"runtime": {"other": "nginx"}}, macapp={"runtime": {"app": "com.example.app"}}
    )
    replies[(("switchboard@" + ip(192, 168, 50, 2)), "status")] = (255, "", "PRIVATE")
    replies[(("switchboard@" + ip(10, 0, 0, 2)), "status")] = {
        "ok": True,
        "services": [running("nginx")],
    }
    result = await core.call("codex", "ops_service_status", {})
    rows = {r["name"]: r for r in result["services"]}
    assert rows["homebridge"]["status"] == rows["adguard"]["status"] == "unknown"
    assert rows["homebridge"]["reason"] == "remotehost unreachable"
    assert rows["other"]["status"] == "running"
    assert rows["macapp"]["status"] == "stopped"
    assert ssh_calls(calls) == ["status", "status"]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        [running(rss_mb=-1)],
        [running(pid="bad")],
        [running(state="unknown/")],
        [running(), running()],
        [{"name": "homebridge"}],
    ],
)
async def test_invalid_remote_status_isolated(pi, rows):
    core, _, replies = pi
    replies["status"] = {"ok": True, "services": rows}
    result = await core.call("codex", "ops_service_status", {})
    assert all(r["status"] == "unknown" for r in result["services"])


async def test_remote_logs_filter_redact_and_truncate(pi):
    core, calls, replies = pi
    replies["logs homebridge 8"] = {
        "ok": True,
        "lines": [
            "noise",
            "ERROR first",
            "error password=PRIVATE",
            "ERROR " + "x" * 600,
        ],
    }
    result = await core.call(
        "codex",
        "ops_service_logs",
        {
            "service": "homebridge",
            "contains": "error",
            "lines": 2,
        },
    )
    assert result["source"] == "remote" and result["matched"] == 3 and result["truncated"]
    assert len(result["lines"]) == 2 and len(result["lines"][-1]) == 500
    assert "PRIVATE" not in json.dumps(result) and "REDACTED" in result["lines"][0]
    replies["logs homebridge 400"] = {"ok": True, "lines": ["line"] * 400}
    result = await core.call("codex", "ops_service_logs", {"service": "homebridge", "lines": 200})
    assert len(result["lines"]) == 200 and result["truncated"]
    assert ssh_calls(calls)[-1] == "logs homebridge 400"
    assert (await core.call("codex", "ops_service_logs", {"service": "homebridge", "source": 1}))[
        "error"
    ]["code"] == "not_found"
    replies["logs homebridge 200"] = {"ok": True, "lines": [None]}
    assert (await core.call("codex", "ops_service_logs", {"service": "homebridge"}))["error"][
        "code"
    ] == "backend_invalid"


@pytest.mark.parametrize("with_probe", [True, False])
async def test_remote_restart_cooldown_silence_and_recovery(pi, with_probe):
    core, calls, replies = pi
    if not with_probe:
        core.config.services["homebridge"].pop("probe")
    replies["restart homebridge"] = {"ok": True, "restarted": True}
    replies["status homebridge"] = {"ok": True, "services": [running()]}
    core.services.poll_timeout = 1
    observed = []

    async def probe(config):
        observed.append(config["name"])
        return {"status": "up"}

    core.probes.probe = probe
    args = {"service": "homebridge", "reason": "repair"}
    result = await core.call("codex", "ops_service_restart", args)
    if with_probe:
        silence = core.storage.db.execute(
            "SELECT * FROM monitor_silence WHERE name='homebridge'"
        ).fetchone()
        assert silence["system"] == 1 and silence["client"] == "codex"
        assert silence["until"] - silence["created_at"] == 600
    cooldown = await core.call("codex", "ops_service_restart", args)
    assert cooldown["outcome"] == "blocked" and "cooldown" in cooldown["error"]["message"]
    await core.jobs.tasks[result["job_id"]]
    assert core.jobs.get(result["job_id"], "codex")["result"]["recovered"]
    assert observed == (["homebridge"] if with_probe else [])
    assert ssh_calls(calls) == (
        ["restart homebridge"] if with_probe else ["restart homebridge", "status homebridge"]
    )
    assert not core.monitor.silenced("homebridge", time.time())


@pytest.mark.parametrize(
    "reply,code",
    [
        ({"ok": False, "code": "not_allowed", "error": "PRIVATE"}, "resource_denied"),
        ({"ok": True, "restarted": False}, "backend_error"),
        ((255, "", "PRIVATE"), "backend_down"),
    ],
)
async def test_remote_restart_failed_keeps_reservation_and_silence(pi, reply, code):
    core, calls, replies = pi
    replies["restart homebridge"] = reply
    args = {"service": "homebridge", "reason": "repair"}
    result = await core.call("codex", "ops_service_restart", args)
    assert result["error"]["code"] == code and "PRIVATE" not in json.dumps(result)
    assert core.monitor.silenced("homebridge", time.time())
    assert (await core.call("codex", "ops_service_restart", args))["outcome"] == "blocked"
    assert ssh_calls(calls) == ["restart homebridge"]
    assert (
        await core.call("codex", "ops_service_restart", {"service": "tailscale", "reason": "test"})
    )["outcome"] == "blocked"


async def test_remote_recovery_retries_unreachable_and_cancels(pi):
    core, _, replies = pi
    core.config.services["homebridge"].pop("probe")
    replies["restart homebridge"] = {"ok": True, "restarted": True}
    replies["status homebridge"] = (255, "", "PRIVATE")
    entered = asyncio.Event()

    async def sleep(_):
        entered.set()
        await asyncio.Event().wait()

    core.services.sleep = sleep
    core.services.poll_timeout = 1
    result = await core.call(
        "codex", "ops_service_restart", {"service": "homebridge", "reason": "test"}
    )
    await asyncio.wait_for(entered.wait(), 1)
    core.jobs.cancel(result["job_id"], "codex")
    await asyncio.gather(core.jobs.tasks[result["job_id"]], return_exceptions=True)
    assert core.jobs.get(result["job_id"], "codex")["status"] == "cancelled"


async def test_remote_snapshot_and_checks(pi):
    core, calls, replies = pi
    proc = {"pid": 1, "cpu_percent": 2.5, "rss_mb": 12, "name": "/usr/bin/pi"}
    replies["snapshot"] = {
        "ok": True,
        "agent_version": "1",
        "hostname": "remotehost",
        "kernel": "6.1",
        "load_averages": [0, 1, 2],
        "cpu_count": 4,
        "uptime_s": 123,
        "memory": {"total_mb": 4000, "available_mb": 3000, "used_mb": 1000},
        "swap": {"total_mb": 100, "used_mb": 0, "free_mb": 100},
        "disks": [{"name": "/", "total_gb": 32, "free_gb": 24, "used_percent": 25}],
        "top_cpu": [proc],
        "top_rss": [proc],
        "temperature_c": 41,
        "throttled": ["under_voltage_since_boot"],
        "failed_units": [],
        "reboot_required": False,
        "private": "PRIVATE",
    }
    result = await core.call("codex", "ops_host_snapshot", {"host": "remotehost"})
    assert result["memory"]["free_mb"] == 3000 and result["memory"]["used_mb"] == 1000
    assert result["cpu_count"] == 4 and result["uptime_s"] == 123
    assert result["temperature_c"] == 41 and result["top_cpu"][0]["name"] == "pi"
    assert "PRIVATE" not in json.dumps(result) and "agent_version" not in result
    replies["checks"] = {
        "ok": True,
        "dns": [{"name": "example.com", "ok": True, "answers": ["1.2.3.4"]}],
        "tailscale": {
            "ok": True,
            "state": "Running",
            "online": True,
            "routes": [(ip(192, 168, 50, 0) + "/24")],
            "exit_node_offered": True,
            "peers_online": 3,
        },
        "kuma": {
            "ok": False,
            "active": 300,
            "not_up": [{"name": "test", "type": "http", "status": "down", "last_check": None}]
            * 300,
        },
        "updates": {"count": 200, "packages": ["pkg"] * 100, "lists_age_s": 60},
    }
    result = await core.call("codex", "ops_host_checks", {"host": "remotehost"})
    assert result["dns"][0]["answers"] == ["1.2.3.4"]
    assert result["tailscale"]["exit_node_offered"]
    assert len(result["kuma"]["not_up"]) == 200 and len(result["updates"]["packages"]) == 40
    assert ssh_calls(calls) == ["snapshot", "checks"]
    replies["checks"] = {
        "ok": True,
        "dns": [],
        "tailscale": {"ok": False, "state": "unavailable"},
        "kuma": {"ok": False, "reason": "kuma database unavailable"},
        "updates": {"count": 0, "packages": [], "lists_age_s": None},
    }
    assert (await core.call("codex", "ops_host_checks", {"host": "remotehost"}))["tailscale"][
        "ok"
    ] is False
    replies["checks"] = {"ok": True, "dns": "bad"}
    assert (await core.call("codex", "ops_host_checks", {"host": "remotehost"}))["error"][
        "code"
    ] == "backend_invalid"


async def test_remote_doctor_and_scope_denials(pi, monkeypatch):
    from switchboard.doctor import diagnose

    core, calls, replies = pi
    replies["version"] = {"ok": True, "version": "1"}
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["checks"]["remote_hosts"] == [
        {"host": "remotehost", "status": "ok", "reason": "agent reachable"}
    ]
    replies["version"] = (255, "PRIVATE", "PRIVATE")
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["checks"]["remote_hosts"][0]["status"] == "down"
    assert "PRIVATE" not in json.dumps(result)
    # CLI doctor uses the same transport without needing a running Core.
    before = len(calls)
    fake = core.services.runner

    async def helper(_core, _host, _request, argv, deadline_s, hosts):
        # The CLI has no Core, so it reaches the default runner, i.e. the native helper.
        code, stdout, _ = await fake(argv, timeout_s=deadline_s, remote_hosts=hosts)
        return code, stdout

    monkeypatch.setattr(hostcmd, "run", fake)
    monkeypatch.setattr(remote, "via_helper", helper)
    result = await diagnose(core.config, core.backends, core.policy, core.orb)
    assert len(calls) == before + 1 and result["checks"]["remote_hosts"][0]["status"] == "down"
    for tool in ("ops_host_snapshot", "ops_host_checks"):
        assert (await core.call("dot", tool, {"host": "remotehost"}))["error"][
            "code"
        ] == "scope_denied"
        assert (await core.call("codex", tool, {"host": "unknown"}))["error"]["code"] == "not_found"
        assert (await core.call("codex", tool, {"host": "remotehost", "exec": "id"}))["error"][
            "code"
        ] == "invalid_arguments"
    assert not any(t["name"] == "ops_host_checks" for t in core.tools("dot"))


async def test_remote_deadlines_and_cancellation(pi, monkeypatch):
    core, _, _ = pi
    entered = asyncio.Event()

    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    core.services.runner = hang
    original = asyncio.timeout
    with monkeypatch.context() as context:

        def timeout(seconds):
            assert seconds == 20
            return original(0.01)

        context.setattr(remote.asyncio, "timeout", timeout)
        with pytest.raises(CapabilityError) as exc:
            await remote.call(core, "remotehost", "version")
        assert exc.value.code == "backend_down"
    entered.clear()
    task = asyncio.create_task(remote.call(core, "remotehost", "version"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("reply", [{"ok": True}, {"ok": True, "memory": []}])
async def test_remote_snapshot_rejects_incomplete_reply(pi, reply):
    core, _, replies = pi
    replies["snapshot"] = reply
    assert (await core.call("codex", "ops_host_snapshot", {"host": "remotehost"}))["error"][
        "code"
    ] == "backend_invalid"


async def test_refresh_updates_and_sampled_snapshot_fields(pi):
    core, calls, replies = pi
    replies["refresh-updates"] = {
        "ok": True,
        "refreshed": True,
        "updates": {"count": 2, "packages": ["apt", "docker-ce"], "lists_age_s": 3},
    }
    result = await core.call("codex", "ops_host_refresh_updates", {"host": "remotehost"})
    assert result["refreshed"] and result["updates"]["count"] == 2
    assert ssh_calls(calls)[-1] == "refresh-updates"
    assert (await core.call("dot", "ops_host_refresh_updates", {"host": "remotehost"}))["error"][
        "code"
    ] == "scope_denied"


CONSOLE = {
    "found": True,
    "power": "awake",
    "name": "PS5-123",
    "address": ip(192, 168, 50, 9),
    "firmware": "13.60",
    "running_title": {"id": "PPSA01234_00", "name": "A Game"},
}


async def test_console_status_needs_no_pairing(pi):
    core, calls, replies = pi
    assert remote.TIMEOUTS["ps5"] == 70
    replies["ps5 status"] = {
        "ok": True,
        "console": {
            **CONSOLE,
            "host_id": "PRIVATE",
            "running_title": {**CONSOLE["running_title"], "art": "PRIVATE"},
        },
        "bridge": {"installed": False, "paired": False},
    }
    result = await core.call("dot", "console_status", {})
    assert result["console"]["power"] == "awake" and result["console"]["firmware"] == "13.60"
    assert result["console"]["running_title"]["name"] == "A Game"
    assert result["bridge"] == {"installed": False, "paired": False}
    assert "PRIVATE" not in json.dumps(result)
    replies["ps5 status"] = {
        "ok": True,
        "console": {"found": False, "power": "off"},
        "bridge": {"installed": True, "paired": True},
    }
    assert (await core.call("dot", "console_status", {}))["console"] == {
        "found": False,
        "power": "off",
    }
    replies["ps5 status"] = {"ok": True, "console": {"found": True, "power": "on"}}
    result = await core.call("dot", "console_status", {})
    assert result["error"]["code"] == "backend_invalid"
    assert ssh_calls(calls) == ["ps5 status"] * 3


async def test_console_control_scope_and_refusals(pi):
    core, calls, replies = pi
    for tool in ("console_wake", "console_standby"):
        assert (await core.call("codex", tool, {}))["error"]["code"] == "scope_denied"
        assert (await core.call("codex", tool, {"action": "x"}))["error"]["code"] in (
            "scope_denied",
            "invalid_arguments",
        )
    assert ssh_calls(calls) == []
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("console:control"))
    replies["ps5 wake"] = {
        "ok": True,
        "action": "wake",
        "changed": True,
        "reached": True,
        "console": CONSOLE,
    }
    result = await core.call("codex", "console_wake", {})
    assert result["reached"] and result["console"]["power"] == "awake"
    replies["ps5 standby"] = (
        3,
        json.dumps({"ok": False, "error": "PRIVATE", "code": "not_paired"}),
        "",
    )
    result = await core.call("codex", "console_standby", {})
    assert result["error"]["code"] == "owner_required"
    assert "owner-setup-ps5" in result["error"]["hint"] and "PRIVATE" not in json.dumps(result)
    for code, expected in (
        ("console_offline", "backend_down"),
        ("control_failed", "backend_error"),
    ):
        replies["ps5 standby"] = (3, json.dumps({"ok": False, "error": "x", "code": code}), "")
        assert (await core.call("codex", "console_standby", {}))["error"]["code"] == expected
    assert ssh_calls(calls) == ["ps5 wake"] + ["ps5 standby"] * 3


async def test_console_needs_exactly_one_remote_host(pi):
    core, calls, _ = pi
    core.config.remote_hosts = {}
    assert (await core.call("dot", "console_status", {}))["error"]["code"] == "backend_disabled"
    assert ssh_calls(calls) == []


@pytest.mark.parametrize(
    "ssh_request", ["ps5", "ps5 reboot", "ps5 wake now", "ps5 Wake", "ps5 pair"]
)
def test_console_requests_are_closed(ssh_request):
    assert not hostcmd.valid_request(ssh_request)
    assert all(hostcmd.valid_request(f"ps5 {a}") for a in ("status", "wake", "standby"))


ENGINE_STATE = {
    "/api/consoles": {"consoles": [{"host": ip(192, 168, 50, 9), "name": "PS5", "id": "x"}]},
    "/api/ps5/hw/temps": {
        "cpu_temp": 39,
        "soc_temp": 37,
        "m2_temp": 41,
        "cpu_freq_mhz": 800,
        "cpu_usage_pct": -1,
        "stale": False,
    },
    "/api/ps5/hw/storage": {"user_total_bytes": 1000, "user_free_bytes": 250},
    "/api/ps5/hw/power": {"operating_time_sec": 4399},
    "/api/ps5/hw/info": {"model": "CFI-1215A 01X", "serial": "PRIVATE-SERIAL"},
    "/api/ps5/status": {"version": "11.2.4", "ps5_kernel": "PRIVATE"},
    "/api/ps5/process/list": {
        "processes": [
            {"pid": 1, "title_id": "NPXS40087", "app_id": 7, "kind": "system"},
            {"pid": 2, "title_id": "", "app_id": 0, "kind": "payload"},
            {"pid": 3, "title_id": "PPSA01935", "app_id": 24, "kind": "app"},
        ]
    },
    "/api/ps5/apps/installed": {
        "titles": [
            {"title_id": "PPSA01935", "title_name": "Plex", "system": False},
            {"title_id": "CUSA01163", "title_name": "Rocket League", "system": False},
            {"title_id": "PLDM00001", "title_name": "Payload Manager", "system": False},
            {"title_id": "NPXS40087", "title_name": "Shell", "system": True},
            {"title_id": "bad id", "title_name": "x", "system": False},
        ]
    },
    "/api/games/installed": [{"title_id": "PPSA01935", "title": "Plex"}],
}


@pytest.fixture
def engine(pi, monkeypatch):
    core, calls, replies = pi
    core.config.backends["ps5upload"] = {"url": "http://127.0.0.1:19113", "enabled": True}
    state, posts = json.loads(json.dumps(ENGINE_STATE)), []
    real_request = core.backends.request

    async def request(name, path="", *, params=None, payload=None, **kwargs):
        if name != "ps5upload":
            return await real_request(name, path, params=params, payload=payload, **kwargs)
        if payload is not None:
            posts.append((path, payload))
            return state.get(("POST", path), {"ok": True})
        if path != "/api/consoles" and path != "/api/games/installed":
            assert params == {"addr": ip(192, 168, 50, 9)}
        value = state[path]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(core.backends, "request", request)
    monkeypatch.setattr(core.backends, "get", lambda name, path="", **kw: request(name, path, **kw))
    replies["ps5 status"] = {
        "ok": True,
        "console": dict(CONSOLE, running_title=None),
        "bridge": {"installed": True, "paired": True},
    }
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("console:control"))
    return core, state, posts


async def test_console_status_adds_engine_details(engine):
    core, state, _ = engine
    result = await core.call("dot", "console_status", {})
    assert result["source"] == "ps5"
    assert result["details"] == {
        "model": "CFI-1215A",
        "cpu_temp_c": 39,
        "soc_temp_c": 37,
        "ssd_temp_c": 41,
        "cpu_mhz": 800,
        "uptime_s": 4399,
        "storage_total_bytes": 1000,
        "storage_free_bytes": 250,
        "helper_version": "11.2.4",
    }
    assert result["console"]["running_title"] == {"id": "PPSA01935", "name": "Plex"}
    assert "PRIVATE" not in json.dumps(result)
    # Engine closed: power still answers, details explain why they're missing.
    from switchboard.errors import CapabilityError as E

    for path in list(state):
        state[path] = E("backend_down", "ps5upload: unreachable")
    result = await core.call("dot", "console_status", {})
    assert result["console"]["power"] == "awake" and "details" not in result
    assert "PS5Upload" in result["details_unavailable"]


async def test_console_status_skips_details_when_not_awake(pi, engine):
    core, _, _ = engine
    _, _, replies = pi
    replies["ps5 status"] = {
        "ok": True,
        "console": {"found": True, "power": "rest", "name": "PS5-123"},
        "bridge": {"installed": True, "paired": True},
    }
    result = await core.call("dot", "console_status", {})
    assert "details" not in result and "details_unavailable" not in result
    assert "running_title" not in result["console"]
    core.config.backends["ps5upload"]["enabled"] = False
    replies["ps5 status"]["console"]["power"] = "awake"
    result = await core.call("dot", "console_status", {})
    assert "details" not in result and "PS5Upload" in result["details_unavailable"]
    assert (await core.call("dot", "console_games", {}))["error"]["code"] == "backend_disabled"


async def test_console_games_launch_and_close(engine):
    core, state, posts = engine
    games = await core.call("dot", "console_games", {})
    assert [g["title_id"] for g in games["games"]] == ["PPSA01935", "CUSA01163", "PLDM00001"]
    assert [g["launchable"] for g in games["games"]] == [True, True, False]
    assert games["games"][2]["platform"] == "homebrew" and games["running"]["name"] == "Plex"
    assert (await core.call("dot", "console_launch", {"title_id": "CUSA01163"}))["error"][
        "code"
    ] == "scope_denied"
    for bad in ("PLDM00001", "NPXS40087", "CUSA0116", "cusa01163"):
        result = await core.call("codex", "console_launch", {"title_id": bad})
        assert result["error"]["code"] == "invalid_arguments", bad
    assert (await core.call("codex", "console_launch", {"title_id": "CUSA99999"}))["error"][
        "code"
    ] == "not_found"
    result = await core.call("codex", "console_launch", {"title_id": "CUSA01163"})
    assert result["launched"] == {"id": "CUSA01163", "name": "Rocket League"}
    assert posts == [
        ("/api/ps5/app/launch", {"addr": ip(192, 168, 50, 9), "title_id": "CUSA01163"})
    ]
    state[("POST", "/api/ps5/app/launch")] = {"ok": False}
    assert (await core.call("codex", "console_launch", {"title_id": "CUSA01163"}))["error"][
        "code"
    ] == "backend_error"
    posts.clear()
    assert (await core.call("codex", "console_close", {"title_id": "CUSA01163"}))["closed"] == []
    result = await core.call("codex", "console_close", {})
    assert result["closed"] == [{"id": "PPSA01935", "name": "Plex"}]
    assert posts == [
        ("/api/ps5/app/lifecycle", {"addr": ip(192, 168, 50, 9), "action": "kill", "app_id": 24})
    ]


@pytest.mark.parametrize(
    "consoles",
    [
        [],
        [{"host": "8.8.8.8"}],
        [{"host": "127.0.0.1"}],
        [{"host": "a"}, {"host": "b"}],
        [{"host": "fe80::1"}],
        [{"host": 3232235778}],
        [{"host": "0.0.0.0"}],
        [{"host": "169.254.1.1"}],
        [{"host": "192.0.2.1"}],
        [{"host": ip(192, 168, 1, 255)}],
        [{"host": " " + ip(192, 168, 1, 2)}],
    ],
)
async def test_engine_console_must_be_one_lan_address(engine, consoles):
    core, state, posts = engine
    state["/api/consoles"] = {"consoles": consoles}
    result = await core.call("codex", "console_close", {})
    assert result["error"]["code"] in ("backend_disabled", "backend_invalid") and posts == []


async def test_odd_engine_answers_never_break_status(pi, engine):
    core, state, _ = engine
    state["/api/ps5/hw/info"] = {"model": "   "}
    state["/api/ps5/status"] = {"version": ""}
    state["/api/games/installed"] = [{"title_id": ["PPSA01935"], "title": "x"}, "junk", None]
    result = await core.call("dot", "console_status", {})
    assert result["outcome"] == "ok" and "model" not in result["details"]
    assert "helper_version" not in result["details"]
    assert result["console"]["running_title"] == {"id": "PPSA01935", "name": "PPSA01935"}


async def test_process_read_failure_is_unknown_not_nothing(pi, engine):
    from switchboard.errors import CapabilityError as E

    core, state, posts = engine
    state["/api/ps5/process/list"] = E("backend_invalid", "x")
    result = await core.call("dot", "console_status", {})
    assert result["details"]["cpu_temp_c"] == 39 and "running_title" not in result["console"]
    games = await core.call("dot", "console_games", {})
    assert len(games["games"]) == 3 and games["running"] is None and games["running_unknown"]
    # Close still works when only the name cache is down.
    state["/api/ps5/process/list"] = ENGINE_STATE["/api/ps5/process/list"]
    state["/api/games/installed"] = E("backend_down", "x")
    result = await core.call("codex", "console_close", {})
    assert result["closed"] == [{"id": "PPSA01935", "name": "PPSA01935"}] and len(posts) == 1
