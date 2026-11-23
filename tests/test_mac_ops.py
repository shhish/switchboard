"""Homebrew and tunnel contracts use only fake commands, HTTP and temporary YAML."""

import asyncio
import json
import os
from pathlib import Path

import pytest
import yaml

from switchboard import hostcmd
from switchboard.brew import settings as brew_settings
from switchboard.config import Config
from switchboard.errors import CapabilityError
from switchboard.tunnel import edit, origin_url, parse
from switchboard.tunnel import settings as tunnel_settings

from .conftest import change_policy

TUNNEL_ID = "12345678-1234-1234-1234-123456789abc"
CONFIG = f"""# keep this heading
tunnel: {TUNNEL_ID}
credentials-file: /private/tunnel.json
originRequest:
  connectTimeout: 10s # retain
ingress:
  # old route comment
  - hostname: old.example.com
    service: http://127.0.0.1:8080
    originRequest:
      noTLSVerify: true # retain
  # catch-all comment
  - service: http_status:404
# keep footer
"""


def package(name="tool", installed=None, current="1.2.0", **extra):
    return {
        "name": name,
        "installed_versions": installed or ["1.1.0"],
        "current_version": current,
        **extra,
    }


@pytest.fixture
def mac(core, tmp_path):
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("ops:restart"))
    path = tmp_path / "cloudflared" / "config.yml"
    path.parent.mkdir(mode=0o700)
    path.write_text(CONFIG)
    path.chmod(0o600)
    core.config.tunnel = tunnel_settings(
        {
            "config_path": str(path),
            "launchd_label": "com.cloudflare.cloudflared.tunnel",
            "zone": "example.com",
        }
    )
    calls = []
    inventory = {
        "formulae": [package(), package("node", current="2.0.0"), package("postgresql@17")],
        "casks": [package("viewer")],
    }
    failure = {}

    async def runner(argv, timeout_s=10):
        assert hostcmd.valid_argv(argv), argv
        calls.append((argv, timeout_s))
        if tuple(argv) in failure:
            return failure[tuple(argv)]
        if argv[:2] == [hostcmd.BREW, "outdated"]:
            return 0, json.dumps(inventory), ""
        if argv[:2] == [hostcmd.BREW, "upgrade"]:
            for kind in inventory:
                inventory[kind][:] = [r for r in inventory[kind] if r["name"] != argv[-1]]
        if argv[:2] == [hostcmd.LAUNCHCTL, "print"]:
            return 0, "state = running\n", ""
        return 0, "", ""

    async def probe(session, url, timeout_s):
        return {"status": 200, "latency_ms": 1.0, "error": None}

    core.brew.runner = core.tunnel.runner = runner
    core.tunnel.probe = probe
    return core, path, calls, inventory, failure


async def finish(core, handle):
    await core.jobs.tasks[handle["job_id"]]
    return core.jobs.get(handle["job_id"], "codex")


async def approve(core, name, args):
    pending = await core.call("codex", name, args)
    assert pending["outcome"] == "needs_approval", pending
    core.resolve("owner", pending["approval_id"], True)
    return {**args, "approval_id": pending["approval_id"]}


async def test_brew_inventory_cache_refresh_and_versions(mac):
    core, _, calls, inventory, _ = mac
    result = await core.call("codex", "ops_brew_outdated", {})
    assert result["formulae"][0] == {
        "name": "tool",
        "installed": ["1.1.0"],
        "current": "1.2.0",
        "major": False,
        "held": False,
        "pinned": False,
    }
    assert result["formulae"][1]["major"] and result["formulae"][1]["held"]
    assert result["formulae"][2]["held"]
    result["formulae"].clear()
    assert len((await core.call("codex", "ops_brew_outdated", {}))["formulae"]) == 3
    assert len(calls) == 1
    core.brew.cached_at -= 601
    await core.call("codex", "ops_brew_outdated", {})
    assert len(calls) == 2
    inventory["formulae"][0]["pinned"] = True
    result = await finish(core, await core.call("codex", "ops_brew_refresh", {}))
    assert result["status"] == "succeeded" and result["result"]["formulae"][0]["pinned"]
    assert ([hostcmd.BREW, "update"], 300) in calls
    for name, argv in [
        ("tool", [hostcmd.BREW, "upgrade", "tool"]),
        ("viewer", [hostcmd.BREW, "upgrade", "--cask", "viewer"]),
    ]:
        result = await finish(core, await core.call("codex", "ops_brew_upgrade", {"name": name}))
        assert result["status"] == "succeeded"
        assert result["result"]["before"] == ["1.1.0"] and result["result"]["after"] == ["1.2.0"]
        assert (argv, 900) in calls


async def test_brew_gate_and_exact_approval(mac):
    core, _, calls, inventory, _ = mac
    inventory["formulae"].append(package("big", current="2.0"))
    for name, code in [
        ("node", "brew_held"),
        ("postgresql@17", "brew_held"),
        ("big", "brew_major"),
        ("absent", "brew_not_outdated"),
    ]:
        assert (await core.call("codex", "ops_brew_upgrade", {"name": name}))["error"][
            "code"
        ] == code
    args = await approve(core, "ops_brew_upgrade_held", {"name": "node"})
    assert not any(a[1] == "upgrade" for a, _ in calls)
    wrong = await core.call("codex", "ops_brew_upgrade_held", {**args, "name": "big"})
    assert wrong["error"]["code"] == "approval_mismatch"
    result = await finish(core, await core.call("codex", "ops_brew_upgrade_held", args))
    assert result["status"] == "succeeded"
    assert (await core.call("codex", "ops_brew_upgrade_held", args))["error"][
        "code"
    ] == "approval_invalid"


async def test_brew_bounds_bad_json_and_failures(mac):
    core, _, _, inventory, failure = mac
    inventory["formulae"] = [package(f"pkg{i}") for i in range(101)]
    result = await core.call("codex", "ops_brew_outdated", {})
    assert len(result["formulae"]) == 100 and result["truncated"]
    failure[(hostcmd.BREW, "outdated", "--json=v2")] = (0, '{"formulae": "PRIVATE"}', "")
    core.brew.cached = None
    result = await core.call("codex", "ops_brew_outdated", {})
    assert result["error"]["code"] == "brew_unavailable" and "PRIVATE" not in str(result)
    failure.clear()
    failure[(hostcmd.BREW, "upgrade", "pkg0")] = (-1, "PRIVATE", "PRIVATE")
    job = await finish(core, await core.call("codex", "ops_brew_upgrade", {"name": "pkg0"}))
    assert job["status"] == "failed" and "PRIVATE" not in str(job)
    assert core.brew.cached is None


async def test_brew_cancel_releases_lock(mac):
    core, _, _, _, _ = mac
    started = asyncio.Event()
    original = core.brew.runner

    async def runner(argv, **kwargs):
        if argv[1] == "update":
            started.set()
            await asyncio.Event().wait()
        return await original(argv, **kwargs)

    core.brew.runner = runner
    handle = await core.call("codex", "ops_brew_refresh", {})
    await started.wait()
    task = core.jobs.tasks[handle["job_id"]]
    core.jobs.cancel(handle["job_id"], "codex")
    await asyncio.gather(task, return_exceptions=True)
    assert not core.brew.lock.locked()
    assert core.jobs.get(handle["job_id"], "codex")["status"] == "cancelled"


@pytest.mark.parametrize(
    "name,args",
    [
        ("ops_brew_outdated", {}),
        ("ops_brew_refresh", {}),
        ("ops_brew_upgrade", {"name": "tool"}),
        ("ops_brew_upgrade_held", {"name": "node"}),
        ("ops_tunnel_status", {}),
        ("ops_tunnel_route_add", {"hostname": "new.example.com", "port": 8081}),
        ("ops_tunnel_route_remove", {"hostname": "old.example.com"}),
    ],
)
async def test_policy_denial_before_io(mac, name, args):
    core, _, calls, _, _ = mac
    change_policy(core.config, lambda p: p["clients"]["codex"].update(scopes=[]))
    result = await core.call("codex", name, args)
    assert result["error"]["code"] == "scope_denied" and not calls
    assert core.storage.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "name,args",
    [
        ("ops_brew_upgrade", {"name": "--all"}),
        ("ops_brew_upgrade", {"name": "homebrew/core/tool"}),
        ("ops_brew_upgrade_held", {"name": "tool;true"}),
        ("ops_brew_outdated", {"exec": "anything"}),
        ("ops_tunnel_route_add", {"hostname": "deep.new.example.com", "port": 8080}),
        ("ops_tunnel_route_add", {"hostname": "new.other.com", "port": 8080}),
        ("ops_tunnel_route_add", {"hostname": "new.example.com", "port": 80}),
        ("ops_tunnel_route_add", {"hostname": "new.example.com", "port": True}),
        ("ops_tunnel_route_remove", {"hostname": "old.example.com", "port": 8080}),
    ],
)
async def test_invalid_inputs_no_grants(mac, name, args):
    core, path, calls, _, _ = mac
    result = await core.call("codex", name, args)
    assert result["error"]["code"] == "invalid_arguments"
    assert not calls and path.read_text() == CONFIG
    assert core.storage.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


async def test_route_add_remove_backups_approval_and_comments(mac, monkeypatch):
    core, path, calls, _, _ = mac
    silences = []
    core.config.services = {
        "tunnel": {
            "runtime": {"launchd": core.config.tunnel["launchd_label"]},
            "probe": "edge-health",
        }
    }
    monkeypatch.setattr(core.monitor, "restart_silence", lambda *args: silences.append(args))
    args = await approve(
        core, "ops_tunnel_route_add", {"hostname": "new.example.com", "port": 8081}
    )
    assert not calls and path.read_text() == CONFIG
    mismatch = await core.call("codex", "ops_tunnel_route_add", {**args, "port": 8082})
    assert mismatch["error"]["code"] == "approval_mismatch"
    result = await core.call("codex", "ops_tunnel_route_add", args)
    assert result["route"]["state"] == "ok", result
    backup = Path(result["backup_path"])
    assert (await asyncio.to_thread(backup.read_text)) == CONFIG and os.stat(
        backup
    ).st_mode & 0o777 == 0o600
    assert os.stat(backup.parent).st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert "# catch-all comment" in path.read_text() and "# keep footer" in path.read_text()
    assert [r.get("hostname") for r in parse(path.read_text())["ingress"]] == [
        "old.example.com",
        "new.example.com",
        None,
    ]
    assert [a[1:4] for a, _ in calls] == [
        ["tunnel", "ingress", "validate"],
        ["tunnel", "route", "dns"],
        ["kickstart", "-k", f"gui/{os.getuid()}/com.cloudflare.cloudflared.tunnel"],
    ]
    assert silences[0][0] == "edge-health"
    calls.clear()
    remove = await approve(core, "ops_tunnel_route_remove", {"hostname": "new.example.com"})
    result = await core.call("codex", "ops_tunnel_route_remove", remove)
    assert result["dns_left_in_place"] and "CNAME" in result["note"]
    assert path.read_text() == CONFIG
    assert len(calls) == 2 and all(a[1:4] != ["tunnel", "route", "dns"] for a, _ in calls)


@pytest.mark.parametrize("phase", ["validate", "dns", "restart"])
async def test_add_failure_rollback_boundary(mac, phase):
    core, path, calls, _, failure = mac
    argv = {
        "validate": [hostcmd.CLOUDFLARED, "tunnel", "ingress", "validate", "--config", str(path)],
        "dns": [hostcmd.CLOUDFLARED, "tunnel", "route", "dns", TUNNEL_ID, "new.example.com"],
        "restart": [
            hostcmd.LAUNCHCTL,
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.cloudflare.cloudflared.tunnel",
        ],
    }[phase]
    failure[tuple(argv)] = (1, "PRIVATE", "PRIVATE")
    args = await approve(
        core, "ops_tunnel_route_add", {"hostname": "new.example.com", "port": 8081}
    )
    result = await core.call("codex", "ops_tunnel_route_add", args)
    assert result["error"]["code"] == "tunnel_command_failed" and "PRIVATE" not in str(result)
    assert (path.read_text() == CONFIG) == (phase == "validate")
    assert len(calls) == {"validate": 1, "dns": 2, "restart": 3}[phase]


async def test_remove_restart_failure_restores(mac):
    core, path, _, _, failure = mac
    failure[
        (
            hostcmd.LAUNCHCTL,
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.cloudflare.cloudflared.tunnel",
        )
    ] = (1, "", "")
    args = await approve(core, "ops_tunnel_route_remove", {"hostname": "old.example.com"})
    result = await core.call("codex", "ops_tunnel_route_remove", args)
    assert result["error"]["code"] == "tunnel_command_failed" and path.read_text() == CONFIG


async def test_preflight_duplicate_missing_and_unsafe_config(mac):
    core, path, calls, _, _ = mac
    for name, args, error in [
        (
            "ops_tunnel_route_add",
            {"hostname": "old.example.com", "port": 8081},
            "tunnel_route_exists",
        ),
        ("ops_tunnel_route_remove", {"hostname": "absent.example.com"}, "tunnel_route_missing"),
    ]:
        assert (await core.call("codex", name, args))["error"]["code"] == error
    path.chmod(0o666)
    assert (await core.call("codex", "ops_tunnel_status", {}))["error"][
        "code"
    ] == "tunnel_config_invalid"
    path.chmod(0o600)
    real = path.with_name("real.yml")
    path.rename(real)
    path.symlink_to(real)
    assert (await core.call("codex", "ops_tunnel_status", {}))["error"][
        "code"
    ] == "tunnel_config_invalid"
    assert not calls


async def test_tunnel_status_classification_and_no_secrets(mac):
    core, path, _, _, _ = mac
    data = parse(CONFIG)
    cases = [
        (200, 200, "ok"),
        (401, 302, "ok"),
        (403, 530, "edge_error"),
        (200, None, "edge_error"),
        (None, 200, "origin_down"),
        (500, 200, "origin_down"),
        (200, 404, "unknown"),
        (404, 404, "ok"),
        (200, 200, "ok"),
    ]
    data["ingress"] = [
        {"hostname": f"route{i}.example.com", "service": f"http://127.0.0.1:{9000 + i}"}
        for i in range(len(cases))
    ] + [{"service": "http_status:404"}]
    # A path-limited rule is probed at the literal prefix of its path regex.
    data["ingress"][-2]["path"] = "^/api/v1/"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    seen = []

    async def probe(session, url, timeout_s):
        seen.append(url)
        if url.startswith("https:"):
            assert timeout_s == 8
            status = cases[int(url.split("route")[1].split(".")[0])][1]
        else:
            assert timeout_s == 5
            status = cases[int(url.rsplit(":", 1)[1].split("/")[0]) - 9000][0]
        return {
            "status": status,
            "latency_ms": 2,
            "error": "unreachable" if status is None else None,
        }

    core.tunnel.probe = probe
    result = await core.call("codex", "ops_tunnel_status", {})
    assert result["tunnel_id"] == TUNNEL_ID and result["connector_state"] == "running"
    assert [r["state"] for r in result["routes"]] == [c[2] for c in cases]
    assert result["counts"] == {"ok": 4, "edge_error": 2, "origin_down": 2, "unknown": 1}
    last = len(cases) - 1
    assert result["routes"][last]["path"] == "/api/v1/"
    assert f"https://route{last}.example.com/api/v1/" in seen
    assert f"http://127.0.0.1:{9000 + last}/api/v1/" in seen
    assert result["credentials_file"] == "/private/tunnel.json"
    assert "service" not in result["routes"][0]


def test_edit_preserves_crlf_and_unrelated_yaml():
    text = CONFIG.replace("\n", "\r\n")
    changed = edit(text, "new.example.com", 9000)
    assert "\r\n" in changed and "\n" not in changed.replace("\r\n", "")
    assert edit(changed, "new.example.com") == text
    removed = edit(CONFIG, "old.example.com")
    assert "# old route comment" in removed and "# catch-all comment" in removed
    assert "# keep footer" in removed and "connectTimeout: 10s # retain" in removed


@pytest.mark.parametrize(
    "text",
    [
        "ingress: []",
        CONFIG.replace("tunnel:", "tunnel: other\ntunnel:"),
        CONFIG.replace("  - hostname: old.example.com", "  - &route hostname: old.example.com"),
        CONFIG.replace(
            "  - service: http_status:404",
            "  - hostname: missing.example.com\n    service: http_status:404",
        ),
    ],
)
def test_invalid_tunnel_yaml(text):
    with pytest.raises(CapabilityError):
        parse(text)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@localhost:8080",
        "http://169.254.169.254/",
        "https://public.example.com",
        "http://localhost/?token=x",
        "http://localhost/#fragment",
    ],
)
def test_origin_restrictions(url):
    assert origin_url(url) is None


def test_config_settings_and_load(config):
    value = {
        "brew": {"held": ["postgresql@*", "custom"]},
        "tunnel": {
            "config_path": str(config.config_dir / "config.yml"),
            "launchd_label": "com.cloudflare.cloudflared.tunnel",
            "zone": "example.com",
        },
    }
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump(value))
    loaded = Config.load()
    assert loaded.brew == value["brew"] and loaded.tunnel == value["tunnel"]
    assert "cloudflared" in brew_settings({})["held"]
    assert tunnel_settings({}) == {}
    for invalid in ({"held": "node"}, {"held": ["*node"]}, {"held": ["node*more"]}, {"exec": "x"}):
        with pytest.raises(ValueError):
            brew_settings(invalid)
    for extra in (
        {"zone": "localhost"},
        {"launchd_label": "com.switchboard"},
        {"config_path": "/tmp/../config.yml"},
        {"extra": True},
    ):
        with pytest.raises(ValueError):
            tunnel_settings({**value["tunnel"], **extra})


@pytest.mark.parametrize(
    "argv",
    [
        [hostcmd.BREW, "outdated", "--json=v2"],
        [hostcmd.BREW, "update"],
        [hostcmd.BREW, "upgrade", "python@3.14"],
        [hostcmd.BREW, "upgrade", "--cask", "docker-desktop"],
        [hostcmd.CLOUDFLARED, "tunnel", "ingress", "validate", "--config", "/tmp/config.yml"],
        [hostcmd.CLOUDFLARED, "tunnel", "route", "dns", TUNNEL_ID, "new.example.com"],
        [
            hostcmd.LAUNCHCTL,
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.cloudflare.cloudflared.tunnel",
        ],
    ],
)
def test_exact_host_argv_allowed(argv):
    assert hostcmd.valid_argv(argv)


@pytest.mark.parametrize(
    "argv",
    [
        [hostcmd.BREW, "upgrade"],
        [hostcmd.BREW, "cleanup"],
        [hostcmd.BREW, "install", "tool"],
        [hostcmd.BREW, "uninstall", "tool"],
        [hostcmd.BREW, "tap", "x"],
        [hostcmd.BREW, "upgrade", "--all"],
        [hostcmd.BREW, "upgrade", "tool", "extra"],
        [hostcmd.BREW, "upgrade", "tool;true"],
        [hostcmd.BREW, "outdated", "--json=v1"],
        [hostcmd.CLOUDFLARED, "tunnel", "run"],
        [hostcmd.CLOUDFLARED, "tunnel", "delete", TUNNEL_ID],
        [hostcmd.CLOUDFLARED, "tunnel", "ingress", "validate", "--config", "relative/config.yml"],
        [hostcmd.CLOUDFLARED, "tunnel", "ingress", "validate", "--config", "/tmp/../config.yml"],
        [
            hostcmd.CLOUDFLARED,
            "tunnel",
            "route",
            "dns",
            "--overwrite-dns",
            TUNNEL_ID,
            "new.example.com",
        ],
        [hostcmd.CLOUDFLARED, "tunnel", "route", "dns", TUNNEL_ID, "bad.example.com;true"],
        [hostcmd.LAUNCHCTL, "kickstart", "-k", "system/com.cloudflare.cloudflared.tunnel"],
        [
            hostcmd.LAUNCHCTL,
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.cloudflare.cloudflared.tunnel",
            "extra",
        ],
    ],
)
def test_host_argv_rejected(argv):
    assert not hostcmd.valid_argv(argv)


async def test_http_probe_timeouts_redirects_body_privacy_and_concurrency(mac, monkeypatch):
    core, _, _, _, _ = mac
    # Exercise the real probe against an in-memory session, never a socket.
    del core.tunnel.probe
    active = peak = 0
    seen = []

    class Response:
        status = 403

        async def __aenter__(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            return self

        async def __aexit__(self, *args):
            nonlocal active
            active -= 1

        async def read(self):
            pytest.fail("Never read origin or public response bodies")

    class Session:
        def get(self, url, *, timeout, allow_redirects):
            assert allow_redirects is False and timeout.total in {5, 8}
            seen.append((url, timeout.total))
            return Response()

    results = await asyncio.gather(
        *(core.tunnel.probe(Session(), "https://example.com/", 8) for _ in range(30))
    )
    assert peak == 8 and active == 0 and len(seen) == 30
    assert all(r["status"] == 403 and r["error"] is None for r in results)

    class FailedResponse(Response):
        async def __aenter__(self):
            raise TimeoutError("PRIVATE")

    monkeypatch.setattr(Session, "get", lambda *args, **kwargs: FailedResponse())
    result = await core.tunnel.probe(Session(), "http://localhost:8080/", 5)
    assert result["status"] is None and result["error"] == "unreachable"
    assert "PRIVATE" not in str(result)


async def test_route_cancel_before_dns_restores_config(mac):
    core, path, _, _, _ = mac
    started = asyncio.Event()

    async def runner(argv, **kwargs):
        started.set()
        await asyncio.Event().wait()

    core.tunnel.runner = runner
    task = asyncio.create_task(core.tunnel.change("codex", "new.example.com", 8081))
    await started.wait()
    assert "new.example.com" in path.read_text()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert path.read_text() == CONFIG and not core.tunnel.lock.locked()


async def test_brew_execution_rechecks_major_gate(mac):
    core, _, calls, inventory, _ = mac
    # First read accepts a minor version, but the job must refuse a changed major.
    original = core.brew.target
    checks = 0

    async def target(name, allow_held):
        nonlocal checks
        checks += 1
        if checks == 2:
            inventory["formulae"][0]["current_version"] = "2.0.0"
        return await original(name, allow_held)

    core.brew.target = target
    job = await finish(core, await core.call("codex", "ops_brew_upgrade", {"name": "tool"}))
    assert job["status"] == "failed" and checks == 2
    assert not any(a[1] == "upgrade" for a, _ in calls)


async def test_brew_and_cloudflared_fixed_environment(monkeypatch):
    seen = []

    class Process:
        returncode = 0

        def __init__(self):
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    async def spawn(*argv, **kwargs):
        seen.append((argv, kwargs))
        return Process()

    monkeypatch.setenv("HOMEBREW_DANGEROUS", "PRIVATE")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert (await hostcmd.run([hostcmd.BREW, "update"], timeout_s=300))[0] == 0
    assert (
        await hostcmd.run(
            [hostcmd.CLOUDFLARED, "tunnel", "route", "dns", TUNNEL_ID, "new.example.com"]
        )
    )[0] == 0
    brew_env = seen[0][1]["env"]
    assert brew_env["HOMEBREW_NO_AUTO_UPDATE"] == brew_env["HOMEBREW_NO_ANALYTICS"] == "1"
    assert "HOMEBREW_DANGEROUS" not in brew_env
    assert brew_env["HOMEBREW_NO_INSTALL_CLEANUP"] == "1"
    assert brew_env["HOME"] == str(Path.home())
    assert seen[1][1]["env"]["HOME"] == str(Path.home())


def test_tap_qualified_brew_names_are_valid_and_held_by_short_name(mac):
    from switchboard import hostcmd

    core = mac[0]
    assert hostcmd.brew_name("xcodesorg/made/xcodes", listed=True)
    assert not hostcmd.brew_name("xcodesorg/made/xcodes")
    assert hostcmd.brew_name("python@3.13")
    assert not hostcmd.brew_name("../evil", listed=True)
    assert not hostcmd.brew_name("a/b", listed=True)
    assert core.brew.held("homebrew/core/node") == core.brew.held("node")


async def test_outdated_lists_tap_formulae(mac, monkeypatch):
    import json as _json

    core = mac[0]
    payload = {
        "formulae": [
            {
                "name": "xcodesorg/made/xcodes",
                "installed_versions": ["2.0.3"],
                "current_version": "2.2.0",
                "pinned": True,
                "pinned_version": "2.0.3",
            }
        ],
        "casks": [],
    }

    async def command(argv, timeout_s):
        return _json.dumps(payload)

    monkeypatch.setattr(core.brew, "command", command)
    result = await core.call("codex", "ops_brew_outdated", {})
    assert result["formulae"][0]["name"] == "xcodesorg/made/xcodes", result
    assert result["formulae"][0]["pinned"] is True
