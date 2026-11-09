import asyncio
import json
import ssl
from datetime import datetime
from pathlib import Path

import aiohttp
import pytest
import yaml
from aiohttp import web

from switchboard import probes
from switchboard.auth import Tokens
from switchboard.config import Config, validate_probes
from switchboard.policy import Policy

from .conftest import bearer, running_app
from .net_helpers import ip


async def test_health_status_shape_and_redaction(core):
    async def handle(request):
        assert request.method == "GET"
        assert not request.query
        assert not {"Authorization", "X-Api-Key", "Cookie"} & request.headers.keys()
        return web.Response(status=int(request.match_info["code"]), text="PRIVATE BODY")

    app = web.Application()
    app.router.add_get("/{code}", handle)
    async with running_app(web.Application()) as closed_url:
        pass
    async with running_app(app) as url:
        core.config.probes = validate_probes(
            [
                {"name": "z_ok", "group": "apps", "url": url + "/200"},
                {"name": "denied", "group": "media", "url": url + "/401"},
                {"name": "forbidden", "group": "media", "url": url + "/403"},
                {"name": "error", "group": "apps", "url": url + "/503"},
                {"name": "closed", "url": closed_url},
            ]
        )
        result = await core.call("dot", "ops_health", {})
    assert set(result) == {
        "status",
        "checked_at",
        "summary",
        "services",
        "outcome",
        "source",
        "as_of",
        "truncated",
    }
    assert result["status"] == "ok"
    assert datetime.fromisoformat(result["checked_at"]).utcoffset() is not None
    assert result["summary"] == {"up": 3, "degraded": 1, "down": 1}
    assert [row["name"] for row in result["services"]] == [
        "closed",
        "error",
        "z_ok",
        "denied",
        "forbidden",
    ]
    for row, status in zip(result["services"], [None, 503, 200, 401, 403], strict=True):
        expected = {"name", "group", "status", "latency_ms"}
        if status is not None:
            expected.add("http_status")
            assert row["http_status"] == status
        else:
            expected.add("reason")  # Down rows say whether it timed out or was refused.
            assert row["reason"] in ("timeout", "error")
        assert set(row) == expected
        assert row["latency_ms"] >= 0
        assert row["status"] == (
            "down" if status is None else "degraded" if status >= 500 else "up"
        )
    assert url not in json.dumps(result)
    assert "PRIVATE" not in json.dumps(result)
    assert core.registry.capabilities["ops_health"].service == "switchboard"


async def test_timeout_is_down(core):
    async def blocked(_):
        await asyncio.Event().wait()

    app = web.Application()
    app.router.add_get("/", blocked)
    assert probes.PROBE_TIMEOUT_S == 3
    async with running_app(app) as url:
        core.config.probes = validate_probes([{"name": "slow", "url": url}])
        result = await core.call("dot", "ops_health", {})
    assert result["summary"] == {"up": 0, "degraded": 0, "down": 1}
    row = result["services"][0]
    assert row["status"] == "down"
    assert row["latency_ms"] >= 2000
    assert "http_status" not in row


async def test_cache_single_flight_concurrency_and_expiry(core, monkeypatch):
    received = []
    all_started = asyncio.Event()
    release = asyncio.Event()
    clock = [100]
    monkeypatch.setattr(probes, "monotonic", lambda: clock[0])

    async def handle(request):
        received.append(request.path)
        if len(received) == 3:
            all_started.set()
        await release.wait()
        return web.Response(status=200)

    app = web.Application()
    app.router.add_get("/{name}", handle)
    async with running_app(app) as url:
        core.config.probes = validate_probes(
            [{"name": name, "url": url + "/" + name} for name in ("a", "b", "c")]
        )
        calls = [
            asyncio.create_task(core.call(client, "ops_health", {}))
            for client in ("dot", "cortex", "codex", "owner", "claude")
        ]
        try:
            # All three must start before any finishes: probes run concurrently.
            await asyncio.wait_for(all_started.wait(), 1)
            assert len(received) == 3
        finally:
            release.set()
            results = await asyncio.gather(*calls)
        assert all(result == results[0] for result in results)
        assert len(received) == 3
        results[0]["services"].clear()
        clock[0] += 14.99
        cached = await core.call("cortex", "ops_health", {})
        assert len(cached["services"]) == 3
        assert cached == results[1]
        assert len(received) == 3
        clock[0] = 115
        refreshed = await core.call("dot", "ops_health", {})
        assert refreshed["summary"] == {"up": 3, "degraded": 0, "down": 0}
        assert len(received) == 6


async def test_cancelled_caller_does_not_cancel_shared_run(core):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handle(_):
        calls.append(True)
        started.set()
        await release.wait()
        return web.Response(status=200)

    app = web.Application()
    app.router.add_get("/", handle)
    async with running_app(app) as url:
        core.config.probes = validate_probes([{"name": "test", "url": url}])
        first = asyncio.create_task(core.call("dot", "ops_health", {}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not core.probes.task.done()
        finally:
            release.set()
        second = await core.call("cortex", "ops_health", {})
        assert second["summary"]["up"] == 1
        assert calls == [True]


async def test_redirects_cookies_and_environment_are_ignored(core, monkeypatch):
    received = []

    async def redirect(request):
        received.append(request.path)
        assert "Cookie" not in request.headers
        return web.Response(
            status=302, headers={"Location": "/target", "Set-Cookie": "key=PRIVATE"}
        )

    async def target(_):
        pytest.fail("Probe followed a redirect")

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/target", target)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert core.probes.session.trust_env is False
    assert isinstance(core.probes.session.cookie_jar, aiohttp.DummyCookieJar)
    async with running_app(app) as url:
        core.config.probes = validate_probes([{"name": "redirect", "url": url + "/redirect"}])
        result = await core.call("dot", "ops_health", {})
    assert result["services"][0]["http_status"] == 302
    assert result["services"][0]["status"] == "up"
    assert received == ["/redirect"]


async def test_tls_failure_is_down_and_verification_setting_is_used(core, monkeypatch):
    calls = []

    def failed(url, **kwargs):
        calls.append(kwargs)
        raise aiohttp.ClientSSLError(None, ssl.SSLError("PRIVATE TLS details"))

    monkeypatch.setattr(core.probes.session, "get", failed)
    core.config.probes = validate_probes(
        [
            {"name": "verify", "url": "https://127.0.0.1/"},
            {
                "name": "self_signed",
                "url": "https://127.0.0.1/",
                "tls_verify": False,
                "timeout_s": 15,
            },
        ]
    )
    result = await core.call("dot", "ops_health", {})
    assert result["summary"] == {"up": 0, "degraded": 0, "down": 2}
    assert [call.pop("timeout").total for call in calls] == [3, 15]
    assert calls == [
        {"ssl": True, "allow_redirects": False},
        {"ssl": False, "allow_redirects": False},
    ]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://example.com/",
        "http://8.8.8.8/",
        "http://0.0.0.0/",
        "http://169.254.1.2/",
        ("http://" + ip(100, 64, 0, 1) + "/"),
        "http://172.15.255.255/",
        "http://172.32.0.0/",
        "http://192.169.0.1/",
        "http://[fd00::1]/",
        ("http://[::ffff:" + ip(192, 168, 50, 2) + "]/"),
        "http://[::1%lo0]/",
        "http://user:PRIVATE@127.0.0.1/",
        "http://@127.0.0.1/",
        "http://127.0.0.1/?key=PRIVATE",
        "http://127.0.0.1/?",
        "http://127.0.0.1/#",
        "http://127.0.0.1/#PRIVATE",
        "ftp://127.0.0.1/",
        "http://127.0.0.1:0/",
        "http://127.0.0.1:65536/",
        "http://127.0.0.1:bad/",
        "http://127.0.0.1/" + "x" * 200,
        "http://127.0.0.1/\n",
        "http://127.0.0.1\\@8.8.8.8/",
        None,
        [],
    ],
)
def test_invalid_probe_urls_rejected(config, url):
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"probes": [{"name": "test", "url": url}]})
    )
    with pytest.raises(ValueError) as error:
        Config.load()
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "settings",
    [
        {"tls_verify": False, "url": ("https://" + ip(192, 168, 50, 2) + "/")},
        {"tls_verify": False, "url": ("http://" + ip(10, 0, 0, 1) + "/")},
        {"tls_verify": "false"},
        {"tls_verify": 0},
        {"group": None},
        {"group": []},
        {"name": "UPPER"},
        {"name": ""},
        {"name": "a" * 41},
        {"name": 12},
        {"name": "with space"},
        {"name": "with.dot"},
        {"headers": {"Authorization": "PRIVATE"}},
        {"token_file": "PRIVATE"},
        {"method": "POST"},
        {"timeout_s": 0},
        {"timeout_s": 31},
        {"timeout_s": "15"},
        {"timeout_s": True},
        {"failures_before_alert": 0},
        {"failures_before_alert": 11},
        {"failures_before_alert": 2.5},
        {"failures_before_alert": True},
    ],
)
def test_invalid_probe_fields_rejected(config, settings):
    data = {"name": "test", "url": "http://127.0.0.1/", **settings}
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"probes": [data]}))
    with pytest.raises(ValueError) as error:
        Config.load()
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("data", [None, {}, "test", [None], [{}], [{"name": "test"}]])
def test_invalid_probe_lists_rejected(config, data):
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"probes": data}))
    with pytest.raises(ValueError):
        Config.load()


def test_probe_defaults_bounds_unique_and_legacy_config(config):
    assert Config.load().probes == []
    data = [{"name": "a_1-" + str(n), "url": "http://127.0.0.1/"} for n in range(60)]
    path = config.config_dir / "config.yaml"
    path.write_text(yaml.safe_dump({"probes": data, "backends": {"health": {"enabled": False}}}))
    loaded = Config.load()
    assert len(loaded.probes) == 60
    assert loaded.probes[0] == {**data[0], "tls_verify": True, "group": "", "alert": True}
    for invalid in (data + [{"name": "other", "url": "http://127.0.0.1/"}], [data[0], data[0]]):
        path.write_text(yaml.safe_dump({"probes": invalid}))
        with pytest.raises(ValueError):
            Config.load()


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "[::1]", ip(10, 0, 0, 1), "172.16.0.1", "172.31.255.255", ip(192, 168, 50, 2)],
)
def test_allowed_probe_addresses(config, host):
    entries = [
        {"name": scheme, "url": f"{scheme}://{host}:1234/" + "x" * 199}
        for scheme in ("http", "https")
    ]
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"probes": entries}))
    assert len(Config.load().probes) == 2


async def test_doctor_reports_count_without_contacting_aggregator(core, fake_backends):
    # This URL must not be used even when the deprecated backend is enabled.
    core.config.backends["health"].update(url="http://127.0.0.1:8050", enabled=True)
    core.config.probes = validate_probes([{"name": "unused", "url": "http://127.0.0.1:1"}])
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["checks"]["probes"] == {"status": "ok", "configured": 1}
    assert "health" not in result["backends"]
    assert not any(row["path"] in ("/health/status", "/") for row in fake_backends[1])


async def test_cortex_auth_discovery_and_denials(server, config):
    core, url, session = server
    policy = Policy(Path("config/policy.example.yaml"))
    assert policy.client("cortex") == {
        "scopes": ["memory:read", "ops:read", "weather:read", "git:read"]
    }
    core.policy = policy
    assert (
        Tokens(config.token_dir).authenticate(bearer(config, "cortex")["Authorization"]) == "cortex"
    )
    response = await session.get(url + "/v1/tools", headers=bearer(config, "cortex"))
    catalog = await response.json()
    cortex_tools = {"ops_health", "ops_switchboard_status", "ops_alerts_status", "dns_stats"} | {
        name
        for name in core.registry.capabilities
        if name.startswith(("weather_", "storage_"))
        or (
            name.startswith(("git_", "memory_"))
            and core.registry.capabilities[name].scope in ("git:read", "memory:read")
        )
    }
    assert {tool["name"] for tool in catalog["tools"]} == cortex_tools
    assert {tool["name"] for tool in await core.mcp_tools("cortex")} == cortex_tools
    result = await core.call("cortex", "ops_health", {})
    assert result["status"] == "ok"
    for name, cap in core.registry.capabilities.items():
        if cap.scope not in ("ops:read", "weather:read", "git:read", "memory:read"):
            assert (await core.call("cortex", name, {}))["error"]["code"] == "scope_denied"


def test_slow_probe_overrides(config):
    probes = validate_probes(
        [{"name": "nc", "url": "http://127.0.0.1/", "timeout_s": 15, "failures_before_alert": 4}]
    )
    assert probes[0]["timeout_s"] == 15 and probes[0]["failures_before_alert"] == 4
    assert "timeout_s" not in validate_probes([{"name": "a", "url": "http://127.0.0.1/"}])[0]
    with pytest.raises(ValueError):  # LAN probes time out inside the helper.
        validate_probes(
            [{"name": "pi", "url": ("http://" + ip(10, 0, 0, 2) + "/"), "timeout_s": 15}]
        )
