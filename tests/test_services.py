"""Host operations use fake command/probe output only; never execute macOS tools."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from switchboard import hostcmd, services
from switchboard.config import Config, validate_probes, validate_services
from switchboard.storage import Storage

from .conftest import change_policy

PROBES = validate_probes([{"name": "web", "url": "http://127.0.0.1:12345/"}])
GOOD = {"web": {"runtime": {"launchd": "com.example.web"}, "probe": "web", "restart": True}}


@pytest.fixture
async def svc(core, monkeypatch):
    monkeypatch.setattr(services, "disks", lambda: [])
    monkeypatch.setattr(services.os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(services.os, "cpu_count", lambda: 8)
    core.config.probes = PROBES.copy()
    core.config.services = validate_services(GOOD, PROBES)
    change_policy(
        core.config,
        lambda p: p["clients"]["codex"]["scopes"].extend(
            ["ops:diagnose", "ops:restart", "ops:silence"]
        ),
    )
    calls = []
    replies = {}

    async def runner(argv):
        assert hostcmd.valid_argv(argv), argv
        calls.append(argv)
        return replies.get(tuple(argv), (0, "", ""))

    core.services.runner = runner
    core.services.poll_timeout = 0
    return core, calls, replies


def test_config_good_and_load(config):
    value = validate_services(
        {
            **GOOD,
            "docker": {"runtime": {"docker": "web-1"}, "logs": ["docker"]},
            "app": {"runtime": {"app": "com.example.App"}, "logs": ["~/Library/Logs/app*.log"]},
        },
        PROBES,
    )
    assert value["app"]["logs"] == [str(Path.home() / "Library/Logs/app*.log")]
    assert value["app"]["restart"] is False
    assert value["web"]["restart"] is True
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"services": GOOD, "probes": PROBES})
    )
    assert Config.load().services == validate_services(GOOD, PROBES)
    assert Config(
        config.config_dir, config.state_dir, config.log_dir, probes=PROBES, services=GOOD
    ).services == validate_services(GOOD, PROBES)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {f"s{i}": GOOD["web"] for i in range(61)},
        {"UPPER": GOOD["web"]},
        {"a" * 41: GOOD["web"]},
        {1: GOOD["web"]},
        {"a": []},
        {"a": {}},
        {"a": {"runtime": {"launchd": "x"}, "extra": True}},
    ],
)
def test_bad_service_mapping(value):
    with pytest.raises(ValueError):
        validate_services(value, PROBES)


@pytest.mark.parametrize(
    "fields",
    [
        {"runtime": None},
        {"runtime": {}},
        {"runtime": {"exec": "x"}},
        {"runtime": {"docker": "x", "launchd": "y"}},
        {"runtime": {"docker": "-x"}},
        {"runtime": {"docker": "a b"}},
        {"runtime": {"launchd": "x;echo"}},
        {"runtime": {"app": "a/b"}},
        {"runtime": {"app": "x" * 121}},
        {"runtime": {"docker": 5}},
        {"probe": "missing"},
        {"probe": None},
        {"restart": "true"},
        {"restart": 1},
        {"runtime": {"app": "x"}, "restart": True},
        {"logs": "x"},
        {"logs": ["/tmp/x"] * 5},
        {"logs": [1]},
        {"logs": [""]},
        {"logs": ["relative.log"]},
        {"logs": ["/tmp/../etc/log"]},
        {"logs": ["/tmp/x..log"]},
        {"logs": ["/etc/log"]},
        {"logs": ["/tmp-other/log"]},
        {"logs": ["/tmp/x\x00log"]},
        {"logs": ["docker"]},
    ],
)
def test_bad_service_fields(fields):
    with pytest.raises(ValueError):
        validate_services({"web": {**GOOD["web"], **fields}}, PROBES)


def launch_print():
    return "state = running\n pid = 42\n last exit code = -9\n"


async def test_status_all_runtimes_probe_and_metrics(svc):
    core, calls, replies = svc
    core.config.services.update(
        docker={"runtime": {"docker": "web"}}, app={"runtime": {"app": "com.example.App"}}
    )
    replies[(hostcmd.LAUNCHCTL, "print", f"gui/{os.getuid()}/com.example.web")] = (
        0,
        launch_print(),
        "",
    )
    replies[(hostcmd.DOCKER, "inspect", "--format", hostcmd.STATE_FORMAT, "web")] = (
        0,
        json.dumps(
            {
                "Running": True,
                "Pid": 43,
                "StartedAt": "2026-01-01T00:00:00Z",
                "Health": {"Status": "healthy"},
            }
        )
        + " 3",
        "",
    )
    replies[(hostcmd.LAUNCHCTL, "list")] = (
        0,
        "PID Status Label\n44 0 application.com.example.App.123\n"
        "45 0 application.com.example.AppOther.123",
        "",
    )
    replies[(hostcmd.PS, "-Axo", "pid,pcpu,rss,comm")] = (
        0,
        "PID %CPU RSS COMM\n42 5.5 10240 /bin/web\n"
        "43 1.0 2048 /bin/docker\n44 2.5 1024 /Applications/App",
        "",
    )
    core.probes.cached = {"services": [{"name": "web", "status": "degraded"}]}
    core.monitor.restart_silence("web", "restart", "codex")
    result = await core.call("codex", "ops_service_status", {})
    assert result["outcome"] == "ok"
    rows = {r["name"]: r for r in result["services"]}
    assert rows["web"]["last_exit"] == -9
    assert rows["web"]["pid"] == 42
    assert rows["web"]["cpu_percent"] == 5.5
    assert rows["web"]["rss_mb"] == 10
    assert rows["web"]["probe_status"] == "degraded" and rows["web"]["silenced"]
    assert rows["docker"]["restart_count"] == 3
    assert rows["docker"]["docker_health"] == "healthy"
    assert rows["docker"]["started_at"] == "2026-01-01T00:00:00Z"
    assert rows["app"]["running"] and rows["app"]["pid"] == 44
    assert (await core.call("codex", "ops_service_status", {"service": "absent"}))[
        "outcome"
    ] == "not_found"
    assert core.audit.tail()[-1]["capability"] == "ops_service_status"


@pytest.mark.parametrize(
    "runtime,text",
    [
        ("launchd", "state = waiting\nlast exit code = 0"),
        ("docker", '{"Running":false,"Pid":0} 0'),
        ("app", "- 0 application.com.example.App.123\n55 0 application.com.example.AppEvil.99"),
    ],
)
async def test_status_stopped(svc, runtime, text):
    core, _, _ = svc
    core.config.services = {"test": {"runtime": {runtime: "com.example.App"}}}

    async def runner(argv):
        return 0, text if argv[0] != hostcmd.PS else "", ""

    core.services.runner = runner
    row = (await core.call("codex", "ops_service_status", {}))["services"][0]
    assert row["status"] == "stopped" and not row["running"]


@pytest.mark.parametrize("kind", ["launchd", "docker", "app"])
@pytest.mark.parametrize("failure", ["code", "oserror", "timeout", "malformed"])
async def test_status_failures_are_per_service(svc, kind, failure):
    core, _, _ = svc
    core.config.services = {"bad": {"runtime": {kind: "com.example.test"}}}

    async def runner(argv):
        if failure == "oserror":
            raise FileNotFoundError("SECRET")
        if failure == "timeout":
            raise TimeoutError("SECRET")
        if failure == "malformed":
            return 0, "malformed", ""
        return 1, "SECRET", "SECRET"

    core.services.runner = runner
    row = (await core.call("codex", "ops_service_status", {}))["services"][0]
    # A successful launchctl list without that app means stopped.
    assert row["status"] == ("stopped" if kind == "app" and failure == "malformed" else "unknown")
    assert "SECRET" not in json.dumps(row)


async def test_logs_tail_filter_source_glob_redaction(svc, tmp_path):
    core, _, _ = svc
    old, new = tmp_path / "old.log", tmp_path / "new.log"
    old.write_text("old\n")
    os.utime(old, (1, 1))
    secrets = [
        "ERROR Authorization: Bearer authsecret",
        "ERROR Bearer bearersecret",
        "ERROR /?apikey=querysecret&token=tokensecret",
        '{"level":"ERROR","api_key":"jsonsecret","nested":{"password":"pwsecret"}}',
        "ERROR secret: yamlsecret",
        "ERROR X-Api-Key=headersecret X-Plex-Token: plexsecret",
        'ERROR password="space secret"',
        "ERROR jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc_def-123",
        "ERROR hex " + "abcdef0123456789" * 3,
        "ERROR b64 " + "aZ09_+/-" * 6,
    ]
    new.write_text("noise\n" + "\n".join(secrets) + "\n")
    core.config.services["web"]["logs"] = [str(old), str(tmp_path / "*.log")]
    result = await core.call(
        "codex",
        "ops_service_logs",
        {"service": "web", "source": 1, "contains": "error", "lines": 20},
    )
    assert result["source"] == str(new.resolve())
    assert result["matched"] == len(secrets)
    text = json.dumps(result["lines"])
    for secret in [
        "authsecret",
        "bearersecret",
        "querysecret",
        "tokensecret",
        "jsonsecret",
        "pwsecret",
        "yamlsecret",
        "headersecret",
        "plexsecret",
        "space secret",
        "eyJ",
        "abcdef0123456789",
        "aZ09_+/-",
    ]:
        assert secret not in text
    assert "<REDACTED>" in text
    last = await core.call("codex", "ops_service_logs", {"service": "web", "source": 1, "lines": 2})
    assert len(last["lines"]) == 2 and last["truncated"]
    assert (await core.call("codex", "ops_service_logs", {"service": "web"}))["lines"] == ["old"]
    assert (await core.call("codex", "ops_service_logs", {"service": "web", "source": 3}))[
        "outcome"
    ] == "not_found"


async def test_logs_bounded_tail_missing_and_symlink(svc, tmp_path):
    core, _, _ = svc
    log = tmp_path / "big.log"
    log.write_bytes(b"do not read this prefix\n" + b"x " * (services.TAIL_LIMIT // 2) + b"\nlast\n")
    core.config.services["web"]["logs"] = [str(log)]
    result = await core.call("codex", "ops_service_logs", {"service": "web", "lines": 1})
    assert result["lines"] == ["last"] and result["truncated"]
    log.write_text("word " * 150 + "\n")
    result = await core.call("codex", "ops_service_logs", {"service": "web"})
    assert len(result["lines"][0]) == 500 and result["lines"][0].endswith("…")
    log.unlink()
    result = await core.call("codex", "ops_service_logs", {"service": "web"})
    assert result["outcome"] == "not_found" and result["error"]["hint"]
    log.symlink_to("/etc/passwd")
    result = await core.call("codex", "ops_service_logs", {"service": "web"})
    assert result["outcome"] == "blocked"


async def test_docker_logs_and_failure(svc):
    core, calls, replies = svc
    core.config.services = {"web": {"runtime": {"docker": "web"}, "logs": ["docker"]}}
    argv = (hostcmd.DOCKER, "logs", "--tail", "800", "web")
    replies[argv] = (0, "out token=abc\n", "err password=xyz\n")
    result = await core.call("codex", "ops_service_logs", {"service": "web", "lines": 200})
    assert result["source"] == "docker"
    assert len(result["lines"]) == 2 and not result["truncated"]
    assert "abc" not in str(result) and "xyz" not in str(result)
    assert any("err" in line for line in result["lines"])
    assert calls[-1] == list(argv)
    replies[argv] = (1, "SECRET", "SECRET")
    result = await core.call("codex", "ops_service_logs", {"service": "web", "lines": 200})
    assert result["outcome"] == "unavailable" and "SECRET" not in str(result)


async def test_snapshot(svc, monkeypatch):
    core, _, replies = svc
    monkeypatch.setattr(services.os, "getloadavg", lambda: (1.0, 2.0, 3.0))
    monkeypatch.setattr(services.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(services.time, "time", lambda: 2000)
    monkeypatch.setattr(
        services,
        "disks",
        lambda: [{"name": "/", "total_gb": 100, "free_gb": 25, "used_percent": 75}],
    )
    replies[(hostcmd.SYSCTL, "-n", "kern.boottime")] = (0, "{ sec = 1000, usec = 0 }", "")
    replies[(hostcmd.SYSCTL, "-n", "hw.memsize")] = (0, str(1024**3), "")
    replies[(hostcmd.SYSCTL, "-n", "vm.swapusage")] = (
        0,
        "total = 2.00G used = 512.00M free = 1.50G",
        "",
    )
    replies[(hostcmd.VM_STAT,)] = (
        0,
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free: 100.\nPages active: 40.\nPages inactive: 50.\n"
        "Pages occupied by compressor: 20.\nPages wired down: 30.\n",
        "",
    )
    replies[(hostcmd.PS, "-Axo", "pid,pcpu,rss,comm")] = (
        0,
        "PID %CPU RSS COMM\n"
        + "\n".join(f"{i} {i}.5 {i * 1024} /Applications/proc{i}" for i in range(1, 12))
        + "\n99 nan 0 /secret\n100 2 inf /secret",
        "",
    )
    result = await core.call("codex", "ops_host_snapshot", {})
    assert result["uptime_s"] == 1000 and result["cpu_count"] == 8
    assert result["load_averages"] == [1, 2, 3]
    assert result["memory"] == {
        "total_mb": 1024,
        "free_mb": 1.56,
        "active_mb": 0.62,
        "cached_mb": 0.78,
        # Activity Monitor style: active + wired + compressed; the file cache is not "used".
        "used_mb": 1.4,
        "compressed_mb": 0.31,
        "wired_mb": 0.47,
    }
    assert result["swap"] == {"total_mb": 2048, "used_mb": 512, "free_mb": 1536}
    assert len(result["top_cpu"]) == len(result["top_rss"]) == 8
    assert result["top_cpu"][0]["pid"] == 11 and result["top_rss"][0]["name"] == "proc11"
    assert "/Applications" not in json.dumps(result)
    assert result["disks"][0]["used_percent"] == 75


async def test_snapshot_unavailable(svc):
    core, _, _ = svc

    async def fail(argv):
        return 1, "SECRET", "SECRET"

    core.services.runner = fail
    result = await core.call("codex", "ops_host_snapshot", {})
    assert result["status"] == "ok" and len(result["unavailable"]) == 5
    assert result["memory"] == {} and result["top_cpu"] == []
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize(
    "runtime,restart",
    [
        ({"launchd": "com.example.web"}, False),
        ({"app": "com.example.App"}, False),
        ({"launchd": "com.example.switchboard"}, True),
        ({"launchd": "com.example.switchboard.worker"}, True),
    ],
)
async def test_restart_blocked(svc, runtime, restart):
    core, calls, _ = svc
    core.config.services = {"web": {"runtime": runtime, "restart": restart}}
    result = await core.call("codex", "ops_service_restart", {"service": "web", "reason": "repair"})
    assert result["outcome"] == "blocked" and result["error"]["hint"]
    assert not result["error"]["retryable"] and not calls


async def test_restart_silence_quota_cooldown_and_recovered_job(svc):
    core, calls, _ = svc
    core.config.probes += validate_probes(
        [{"name": f"p{i}", "url": "http://127.0.0.1:12345/"} for i in range(4)]
    )
    for i in range(3):
        core.monitor.silence(f"p{i}", 10, "other work", "codex")
    observed = []

    async def probe(config):
        observed.append(config["name"])
        return {"name": config["name"], "status": "up"}

    core.probes.probe = probe
    args = {"service": "web", "reason": "repair stalled service"}
    result = await core.call("codex", "ops_service_restart", args)
    assert result["outcome"] == "accepted" and result["job_id"]
    silence = core.storage.db.execute("SELECT * FROM monitor_silence WHERE name='web'").fetchone()
    assert silence["client"] == "codex" and silence["system"] == 1
    assert silence["reason"] == args["reason"]
    assert silence["until"] - silence["created_at"] == 600
    core.monitor.unsilence("p0")
    core.monitor.silence("p3", 10, "quota ignores restart", "codex")
    cooldown = await core.call("codex", "ops_service_restart", args)
    assert cooldown["outcome"] == "blocked" and "seconds" in cooldown["error"]["hint"]
    assert not cooldown["error"]["retryable"]
    await core.jobs.tasks[result["job_id"]]
    job = core.jobs.get(result["job_id"], "codex")
    assert job["status"] == "succeeded" and job["result"] == {"service": "web", "recovered": True}
    assert observed == ["web"]
    assert not core.monitor.silenced("web", services.time.time())
    assert calls == [[hostcmd.LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/com.example.web"]]
    other = Storage(core.config.state_dir)
    assert other.db.execute("SELECT name FROM service_restarts").fetchone()[0] == "web"
    other.close()


async def test_restart_not_recovered_and_failed_command(svc):
    core, _, replies = svc

    async def down(config):
        return {"status": "down"}

    core.probes.probe = down
    args = {"service": "web", "reason": "repair"}
    result = await core.call("codex", "ops_service_restart", args)
    await core.jobs.tasks[result["job_id"]]
    assert core.jobs.get(result["job_id"], "codex")["result"]["recovered"] is False
    assert core.monitor.silenced("web", services.time.time())
    with core.storage.db:
        core.storage.db.execute("UPDATE service_restarts SET restarted_at=restarted_at-601")
    replies[(hostcmd.LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/com.example.web")] = (
        1,
        "SECRET",
        "SECRET",
    )
    result = await core.call("codex", "ops_service_restart", args)
    assert result["outcome"] == "unavailable" and "SECRET" not in str(result)
    assert (await core.call("codex", "ops_service_restart", args))["outcome"] == "blocked"


async def test_restart_runtime_poll_no_sleep_and_cancel(svc):
    core, calls, replies = svc
    core.config.services = {"web": {"runtime": {"docker": "web"}, "restart": True}}
    core.services.poll_timeout = 120
    core.services.poll_interval = 5
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        replies[(hostcmd.DOCKER, "inspect", "--format", hostcmd.STATE_FORMAT, "web")] = (
            0,
            '{"Running":true,"Pid":42} 1',
            "",
        )

    core.services.sleep = fake_sleep
    result = await core.call("codex", "ops_service_restart", {"service": "web", "reason": "repair"})
    await core.jobs.tasks[result["job_id"]]
    assert slept == [5]
    assert core.jobs.get(result["job_id"], "codex")["result"]["recovered"]
    assert calls[0] == [hostcmd.DOCKER, "restart", "web"]
    with core.storage.db:
        core.storage.db.execute("DELETE FROM service_restarts")
    replies.clear()
    event = asyncio.Event()

    async def waiting(seconds):
        event.set()
        await asyncio.Event().wait()

    core.services.sleep = waiting
    result = await core.call("codex", "ops_service_restart", {"service": "web", "reason": "repair"})
    await event.wait()
    task = core.jobs.tasks[result["job_id"]]
    core.jobs.cancel(result["job_id"], "codex")
    await asyncio.gather(task, return_exceptions=True)
    assert core.jobs.get(result["job_id"], "codex")["status"] == "cancelled"


async def test_example_policy_scopes_and_denials(core):
    example = await asyncio.to_thread(Path("config/policy.example.yaml").read_text)
    (core.config.config_dir / "policy.yaml").write_text(example)
    policy = yaml.safe_load(example)
    for scope, allowed in [
        ("notes:read", {"owner", "dot", "orb", "openclaw", "claude", "codex", "agy"}),
        ("ops:diagnose", {"owner", "claude", "codex", "agy", "openclaw"}),
        ("ops:restart", {"owner", "claude", "codex", "agy"}),
    ]:
        assert {c for c, rule in policy["clients"].items() if scope in rule["scopes"]} == allowed
    for client in ("dot", "orb", "cortex", "deepseek"):
        for tool, args in [
            ("ops_service_status", {}),
            ("ops_service_logs", {"service": "web"}),
            ("ops_host_snapshot", {}),
            ("ops_service_restart", {"service": "web", "reason": "repair"}),
        ]:
            result = await core.call(client, tool, args)
            assert result["error"]["code"] == "scope_denied"
        assert not {
            "ops_service_status",
            "ops_service_logs",
            "ops_host_snapshot",
            "ops_service_restart",
        } & {t["name"] for t in core.tools(client)}
    assert (
        await core.call("openclaw", "ops_service_restart", {"service": "web", "reason": "repair"})
    )["error"]["code"] == "scope_denied"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("ops_service_logs", {"service": "web", "lines": 201}),
        ("ops_service_logs", {"service": "web", "source": -1}),
        ("ops_service_logs", {"service": "web", "contains": "x" * 81}),
        ("ops_service_restart", {"service": "web", "reason": ""}),
        ("ops_service_status", {"service": "web; echo"}),
    ],
)
async def test_argument_denials_before_commands(svc, tool, args):
    core, calls, _ = svc
    assert (await core.call("codex", tool, args))["error"]["code"] == "invalid_arguments"
    assert not calls


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "echo hi"],
        [hostcmd.DOCKER, "restart", "-evil"],
        [hostcmd.DOCKER, "restart", "x;echo"],
        [hostcmd.DOCKER, "logs", "--tail", "801", "x"],
        [hostcmd.SYSCTL, "-n", "other"],
        [hostcmd.PS, "aux"],
        [hostcmd.LAUNCHCTL, "print", "gui/0/com.example"],
    ],
)
async def test_runner_rejects_nonfixed_argv(argv):
    with pytest.raises(ValueError):
        await hostcmd.run(argv)


async def test_runner_scrubbed_env_output_cap_timeout_and_cancellation(monkeypatch):
    created = []

    class Process:
        def __init__(self, mode):
            self.returncode = None
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.done = asyncio.Event()
            self.killed = False
            if mode == "ok":
                self.stdout.feed_data(b"out")
                self.stderr.feed_data(b"err")
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.returncode = 0
                self.done.set()
            elif mode == "limit":
                self.stdout.feed_data(b"x" * (hostcmd.OUTPUT_LIMIT + 1))
                self.stderr.feed_data(b"y" * (hostcmd.OUTPUT_LIMIT + 1))

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.done.set()

    mode = "ok"

    async def spawn(*argv, **kwargs):
        assert argv == (hostcmd.VM_STAT,)
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        process = Process(mode)
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await hostcmd.run([hostcmd.VM_STAT]) == (0, "out", "err")
    mode = "limit"
    code, out, err = await hostcmd.run([hostcmd.VM_STAT])
    assert code == -1 and created[-1].killed and len(out) + len(err) <= hostcmd.OUTPUT_LIMIT
    mode = "hang"
    assert (await hostcmd.run([hostcmd.VM_STAT], timeout_s=0.001))[0] == -1
    assert created[-1].killed
    task = asyncio.create_task(hostcmd.run([hostcmd.VM_STAT]))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created[-1].killed


def test_disks_only_directories_skip_unreadable(monkeypatch):
    monkeypatch.setattr(
        Path, "iterdir", lambda p: iter([Path("/Volumes/drive"), Path("/Volumes/unreadable")])
    )
    monkeypatch.setattr(Path, "is_dir", lambda p: True)

    def statvfs(path):
        if str(path).endswith("unreadable"):
            raise PermissionError
        return SimpleNamespace(f_blocks=100, f_frsize=1024**3, f_bavail=30)

    monkeypatch.setattr(os, "statvfs", statvfs)
    devices = {"/": 1, "/Volumes/drive": 2, "/Volumes/unreadable": 3}
    monkeypatch.setattr(os, "stat", lambda p: SimpleNamespace(st_dev=devices[str(p)]))
    rows = services.disks()
    assert [r["name"] for r in rows] == ["/", "/Volumes/drive"]
    assert all(r["used_percent"] == 70 for r in rows)


@pytest.mark.parametrize(
    "state",
    ['{"Running":true} -1', '{"Running":true,"Health":[]} 1', '{"Running":"yes"} 0', "[] 1"],
)
async def test_malformed_docker_does_not_break_other_services(svc, state):
    core, _, replies = svc
    core.config.services["bad"] = {"runtime": {"docker": "bad"}}
    replies[(hostcmd.DOCKER, "inspect", "--format", hostcmd.STATE_FORMAT, "bad")] = (0, state, "")
    replies[(hostcmd.LAUNCHCTL, "print", f"gui/{os.getuid()}/com.example.web")] = (
        0,
        launch_print(),
        "",
    )
    result = await core.call("codex", "ops_service_status", {})
    rows = {r["name"]: r for r in result["services"]}
    assert rows["bad"]["status"] == "unknown"
    assert "restart_count" not in rows["bad"]
    assert rows["web"]["running"]


async def test_literal_filter_and_default_line_limit(svc, tmp_path):
    core, _, _ = svc
    log = tmp_path / "service.log"
    log.write_text("\n".join(f"[{i}] foo.*bar" for i in range(80)) + "\nfoobar\n")
    core.config.services["web"]["logs"] = [str(log)]
    result = await core.call(
        "codex", "ops_service_logs", {"service": "web", "contains": "FOO.*BAR"}
    )
    assert len(result["lines"]) == 50
    assert result["matched"] == 80 and result["truncated"]
    assert result["lines"][-1] == "[79] foo.*bar"


@pytest.mark.parametrize(
    "line,secret",
    [
        ("password: multi word value", "word value"),
        ('prefix password="escaped\\"secret" suffix', "secret"),
        ("api_key='short value'", "short value"),
        ("https://host/?X-Plex-Token=urlvalue&other=ok", "urlvalue"),
        ('prefix {"secret": "quoted value", "message": "ok"}', "quoted value"),
    ],
)
def test_redact_more_secret_forms(line, secret):
    assert secret not in services.redact_line(line)
    assert "<REDACTED>" in services.redact_line(line)


async def test_recovery_probe_timeout_and_wait_uses_injected_sleep(svc):
    core, _, _ = svc
    clock = [0]
    slept = []
    core.services.poll_timeout = 120

    async def down(config):
        return {"status": "down"}

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    core.probes.probe = down
    core.services.sleep = sleep
    # Only the recovery loop uses this fake clock; no real sleeping.
    original = asyncio.get_running_loop().time
    loop = asyncio.get_running_loop()
    try:
        loop.time = lambda: original() + clock[0]
        result = await core.call(
            "codex", "ops_service_restart", {"service": "web", "reason": "repair"}
        )
        await core.jobs.tasks[result["job_id"]]
    finally:
        loop.time = original
    assert core.jobs.get(result["job_id"], "codex")["result"]["recovered"] is False
    assert slept and all(0 < s <= 5 for s in slept)
    assert 119 <= sum(slept) <= 120


async def test_recovery_probe_hang_is_bounded(svc):
    core, _, _ = svc
    core.services.poll_timeout = 0.001

    async def hang(config):
        await asyncio.Event().wait()

    core.probes.probe = hang
    result = await core.call("codex", "ops_service_restart", {"service": "web", "reason": "repair"})
    await core.jobs.tasks[result["job_id"]]
    assert not core.jobs.get(result["job_id"], "codex")["result"]["recovered"]


async def test_runner_merges_docker_stderr_without_inheriting_env(monkeypatch):
    class Process:
        returncode = 0
        stderr = None
        stdout = asyncio.StreamReader()

        async def wait(self):
            return 0

    process = Process()
    process.stdout.feed_data(b"out\nerr\nout2\n")
    process.stdout.feed_eof()

    async def spawn(*argv, **kwargs):
        assert argv == (hostcmd.DOCKER, "logs", "--tail", "200", "web")
        assert kwargs["stderr"] == asyncio.subprocess.STDOUT
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await hostcmd.run([hostcmd.DOCKER, "logs", "--tail", "200", "web"]) == (
        0,
        "out\nerr\nout2\n",
        "",
    )


async def test_empty_inventory_and_monitor_state_fallback(svc):
    core, calls, replies = svc
    core.config.services = {}
    assert (await core.call("codex", "ops_service_status", {}))["services"] == []
    assert not calls
    core.config.services = GOOD
    core.monitor.process([{"name": "web", "status": "up"}], services.time.time())
    replies[(hostcmd.LAUNCHCTL, "print", f"gui/{os.getuid()}/com.example.web")] = (
        0,
        launch_print(),
        "",
    )
    row = (await core.call("codex", "ops_service_status", {"service": "web"}))["services"][0]
    assert row["probe_status"] == "up" and row["silenced"] is False


def test_legacy_silence_migration_preserves_attribution(config):
    import sqlite3

    config.state_dir.mkdir(mode=0o700)
    connection = sqlite3.connect(config.state_dir / "state.sqlite3")
    with connection:
        connection.execute(
            "CREATE TABLE monitor_silence (name TEXT PRIMARY KEY, until REAL, "
            "reason TEXT, client TEXT, created_at REAL)"
        )
        connection.execute("INSERT INTO monitor_silence VALUES ('web', 200, 'work', 'codex', 100)")
    connection.close()
    storage = Storage(config.state_dir)
    try:
        row = storage.db.execute("SELECT * FROM monitor_silence").fetchone()
        assert row["client"] == "codex" and row["system"] == 0
        assert row["reason"] == "work"
    finally:
        storage.close()


def test_redaction_keeps_paths_and_titles_but_drops_keys():
    from switchboard.services import redact_line

    path = "~/workspace/projects/repos/media-ctrl/state/media/compress_request.json"
    title = "The-Lord-of-the-Rings-The-Return-of-the-King-Extended-Edition"
    assert redact_line(f"wrote {path}") == f"wrote {path}"
    assert redact_line(f"importing {title}") == f"importing {title}"
    for secret in ("0123456789abcdef0123456789abcdef", "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3"):
        assert secret not in redact_line(f"value {secret} done")


async def test_process_runtime_and_docker_metrics(core, config):
    from switchboard.config import validate_services

    config.probes = []
    config.services = validate_services(
        {
            "nzbget": {"runtime": {"process": "/Applications/NZBGet.app/"}},
            "db": {"runtime": {"docker": "db"}},
        },
        [],
    )

    async def runner(argv):
        if argv[0] == "/bin/ps":
            return (
                0,
                "  PID %CPU RSS COMM\n"
                "  41 2.0 2048 /Applications/NZBGet.app/Contents/x/nzbget\n"
                "  77 9.0 4096 /usr/bin/other\n",
                "",
            )
        return 0, '{"Running": true, "Pid": 77} 0', ""

    core.services.runner = runner
    rows = {r["name"]: r for r in (await core.services.status())["services"]}
    assert rows["nzbget"]["running"] and rows["nzbget"]["pid"] == 41
    assert rows["nzbget"]["rss_mb"] == 2.0
    # Docker's PID 77 belongs to its VM; a host process with that PID must not be reported.
    assert "cpu_percent" not in rows["db"] and "rss_mb" not in rows["db"]
    with pytest.raises(ValueError):
        validate_services(
            {"x": {"runtime": {"process": "/Applications/X.app/"}, "restart": True}}, []
        )
    with pytest.raises(ValueError):
        validate_services({"x": {"runtime": {"process": "/etc/../bin/"}}}, [])


@pytest.mark.parametrize(
    "label",
    [
        "switchboard",
        "switchboard.eventkit",
        "com.example.switchboard",
        "com.example.switchboard.eventkit",
        "custom.local.service",
    ],
)
async def test_self_restart_guards_use_components_and_custom_label(svc, monkeypatch, label):
    core, calls, _ = svc
    monkeypatch.setenv("SWITCHBOARD_LABEL", "custom.local.service")
    assert not hostcmd.valid_argv(
        [hostcmd.LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
    )
    # Read-only status remains valid even for the service itself.
    assert hostcmd.valid_argv([hostcmd.LAUNCHCTL, "print", f"gui/{os.getuid()}/{label}"])
    core.config.services = {"self": {"runtime": {"launchd": label}, "restart": True}}
    result = await core.call(
        "codex", "ops_service_restart", {"service": "self", "reason": "guard check"}
    )
    assert result["error"]["code"] == "resource_denied"
    assert not calls


@pytest.mark.parametrize(
    "label", ["com.example.switchboardish", "myswitchboard", "switchboard-other"]
)
def test_self_restart_guard_matches_whole_label_components(monkeypatch, label):
    monkeypatch.delenv("SWITCHBOARD_LABEL", raising=False)
    assert hostcmd.valid_argv([hostcmd.LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
