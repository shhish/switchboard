"""Orb federation uses only an ephemeral MCP authority with synthetic credentials."""

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from aiohttp import web

from switchboard.auth import create_token, read_token
from switchboard.config import Config
from switchboard.protocol import (
    MODERN_VERSION,
    VERSION_KEY,
    http_metadata_error,
)
from switchboard.schema import obj
from switchboard.upstreams.orb import EVENT_NAMES, TOOLS, WRITE_TOOLS

from .conftest import change_policy, running_app
from .test_protocol import message, post


@pytest.fixture
async def upstream(config):
    tokens = {}
    for client in ("dot", "claude", "codex"):
        path = create_token(config.config_dir / "upstream-tokens", client)
        config.upstreams["orb"]["client_tokens"][client] = str(path)
        tokens[read_token(path)] = client

    def enable(policy):
        for client in ("dot", "claude", "codex", "owner"):
            policy["clients"][client]["scopes"].append("orb:use")
        policy["allowlists"]["orb"] = {"tools": sorted(WRITE_TOOLS), "events": sorted(EVENT_NAMES)}

    change_policy(config, enable)
    state = {"calls": [], "ttl": 5000, "http": 200, "override": None, "raw": None}
    tools = [
        {
            "name": name,
            "description": "Fake Orb contract",
            "inputSchema": obj(),
            "annotations": {"readOnlyHint": name not in WRITE_TOOLS},
        }
        for name in sorted(TOOLS)
    ]
    # Future hub tools must not widen the capability boundary automatically.
    tools.append({"name": "orb_reboot", "inputSchema": obj()})
    state["tools"] = tools

    async def handle(request):
        if state["raw"] is not None:
            return web.Response(body=state["raw"], content_type="application/json")
        if state["http"] != 200:
            return web.Response(status=state["http"], text="PRIVATE_BACKEND_BODY")
        body = await request.json()
        assert http_metadata_error(body, request.headers) is None
        assert {"application/json", "text/event-stream"} <= set(
            request.headers["Accept"].split(", ")
        )
        client = tokens.get(request.headers["Authorization"].removeprefix("Bearer "))
        if client is None:
            return web.Response(status=401)
        state["calls"].append((client, copy.deepcopy(body)))
        method = body["method"]
        params = body["params"]
        if state["override"] is not None:
            return web.json_response({"jsonrpc": "2.0", "id": body["id"], **state["override"]})
        if method == "server/discover":
            result = {"capabilities": {"tools": {"listChanged": True}, "events": {}}}
        elif method == "tools/list":
            result = {"tools": copy.deepcopy(tools), "ttlMs": state["ttl"], "cacheScope": "private"}
            for tool in result["tools"]:
                tool["_meta"] = {"principal": client}
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "original hub content"}],
                "structuredContent": {"principal": client, "args": params.get("arguments", {})},
                "isError": False,
                "_meta": {"hub": "preserved"},
            }
        elif method == "events/list":
            result = {
                "events": [{"name": name} for name in sorted(EVENT_NAMES)],
                "ttlMs": 1234,
                "cacheScope": "private",
            }
        elif method == "events/subscribe":
            result = {
                "id": f"sub_{client}",
                "refreshBefore": "2026-10-29T10:00:00Z",
                "cursor": None,
                "truncated": False,
            }
        elif method == "events/unsubscribe":
            result = {}
        else:
            raise AssertionError("Unapproved upstream method")
        if params.get("_meta", {}).get(VERSION_KEY) == MODERN_VERSION:
            result["resultType"] = "complete"
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})

    app = web.Application()
    app.router.add_post("/mcp", handle)
    async with running_app(app) as url:
        config.upstreams["orb"]["url"] = url + "/mcp"
        yield state


async def test_merged_catalog_identity_and_cache(server, config, upstream):
    core, _, _ = server
    for client in ("dot", "claude", "codex"):
        local = {t["name"] for t in core.tools(client)}
        _, body = await post(server, config, message("tools/list"), client=client)
        listed = body["result"]["tools"]
        assert {t["name"] for t in listed} == local | TOOLS
        assert [t["name"] for t in listed] == sorted(t["name"] for t in listed)
        assert {t["name"] for t in listed if t["name"].startswith("orb_")} == TOOLS
        for tool in listed:
            if tool["name"] in TOOLS:
                original = next(t for t in upstream["tools"] if t["name"] == tool["name"])
                assert tool == {**original, "_meta": {"principal": client}}
    await post(server, config, message("tools/list"))
    assert [c for c, b in upstream["calls"] if b["method"] == "tools/list"] == [
        "dot",
        "claude",
        "codex",
    ]
    for client in ("owner", "orb", "openclaw", "agy", "cortex"):
        _, body = await post(server, config, message("tools/list"), client=client)
        assert not any(t["name"].startswith("orb_") for t in body["result"]["tools"])
    # Policy allowlists are live even when schemas come from the cache.
    change_policy(config, lambda p: p["allowlists"]["orb"].update(tools=[]))
    _, body = await post(server, config, message("tools/list"))
    assert not WRITE_TOOLS & {t["name"] for t in body["result"]["tools"]}


async def test_cache_zero_ttl_and_expiry(server, config, upstream):
    core, _, _ = server
    upstream["ttl"] = 0
    await post(server, config, message("tools/list"))
    await post(server, config, message("tools/list"))
    assert len(upstream["calls"]) == 2
    upstream["ttl"] = 3600000
    await post(server, config, message("tools/list"))
    identity, expires, catalog = core.orb.cache["dot"]
    core.orb.cache["dot"] = (identity, expires - 6, catalog)
    await post(server, config, message("tools/list"))
    assert len(upstream["calls"]) == 4


@pytest.mark.parametrize("client", ["dot", "claude", "codex"])
async def test_forward_preserves_result_identity_meta_and_legacy(server, config, upstream, client):
    for modern in (True, False):
        request = message("tools/call", modern=modern, name="orb_status", request_id=42)
        request.setdefault("params", {}).setdefault("_meta", {})["trace"] = "kept"
        _, body = await post(server, config, request, client=client)
        result = body["result"]
        assert body["id"] == 42
        assert result == {
            "content": [{"type": "text", "text": "original hub content"}],
            "structuredContent": {"principal": client, "args": {}},
            "isError": False,
            "_meta": {"hub": "preserved"},
            **({"resultType": "complete"} if modern else {}),
        }
        sent = upstream["calls"][-1][1]
        assert sent["params"]["_meta"] == request["params"]["_meta"]


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "result": {
                "content": [{"type": "text", "text": "hub domain failure"}],
                "isError": True,
                "resultType": "complete",
                "_meta": {"keep": True},
            }
        },
        {
            "error": {
                "code": -32015,
                "message": "Callback endpoint verification failed",
                "data": {"reason": "challenge_failed"},
            }
        },
        {"error": {"code": -32602, "message": "Unknown tool", "data": {"test": 1}}},
    ],
)
async def test_upstream_errors_unchanged(server, config, upstream, envelope):
    upstream["override"] = envelope
    _, body = await post(server, config, message("tools/call", name="orb_status", request_id=9))
    assert body == {"jsonrpc": "2.0", "id": 9, **envelope}
    assert server[0].audit.tail(1)[0]["outcome"] == "error"


async def test_modern_events_work_and_legacy_initialize_omits_unserved_events(
    server, config, upstream
):
    _, discover = await post(server, config, message("server/discover"))
    assert discover["result"]["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {},
        "resources": {},
        "events": {},
    }
    _, initialize = await post(
        server,
        config,
        message(
            "initialize",
            modern=False,
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={"name": "test", "version": "1"},
        ),
    )
    assert "events" not in initialize["result"]["capabilities"]
    _, catalog = await post(server, config, message("events/list"))
    assert catalog["result"]["events"] == [{"name": n} for n in sorted(EVENT_NAMES)]
    assert catalog["result"]["ttlMs"] == 1234
    params = {
        "name": "voice.transcribed",
        "arguments": {"device": "orb-fake"},
        "delivery": {
            "mode": "webhook",
            "url": "https://callback.example/private-path",
            "secret": "whsec_PRIVATE_SIGNING",
        },
        "ttlMs": None,
        "cursor": None,
    }
    for client in ("dot", "codex"):
        _, subscribed = await post(server, config, message("events/subscribe", **params), client)
        assert subscribed["result"] == {
            "id": f"sub_{client}",
            "refreshBefore": "2026-10-29T10:00:00Z",
            "cursor": None,
            "truncated": False,
            "resultType": "complete",
        }
        assert upstream["calls"][-1] == (
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events/subscribe",
                "params": message("events/subscribe", **params)["params"],
            },
        )
    unsubscribe = copy.deepcopy(params)
    del unsubscribe["delivery"]["secret"]
    del unsubscribe["ttlMs"], unsubscribe["cursor"]
    _, result = await post(server, config, message("events/unsubscribe", **unsubscribe))
    assert result["result"] == {"resultType": "complete"}
    _, legacy = await post(server, config, message("events/list", modern=False))
    assert legacy["error"]["code"] == -32601
    audit = json.dumps(server[0].audit.tail(100)) + server[0].audit.path.read_text()
    assert "PRIVATE_SIGNING" not in audit and "private-path" not in audit
    assert all(r["args"] == "[REDACTED]" for r in server[0].audit.tail(100))


@pytest.mark.parametrize(
    "method,params",
    [
        ("tools/call", {"name": "orb_show", "arguments": {"card_id": "x", "text": "PRIVATE_TEXT"}}),
        ("tools/call", {"name": "orb_status"}),
        ("events/list", {}),
        ("events/subscribe", {"name": "card.answered"}),
        ("events/unsubscribe", {"name": "voice.transcribed"}),
    ],
)
async def test_scope_enforced_on_hidden_routes(server, config, upstream, method, params):
    await post(server, config, message("tools/list"))
    upstream["calls"].clear()
    change_policy(config, lambda p: p["clients"]["dot"]["scopes"].remove("orb:use"))
    _, denied = await post(server, config, message(method, **params))
    assert denied["error"]["data"]["error"]["code"] == "scope_denied"
    assert upstream["calls"] == []
    _, discover = await post(server, config, message("server/discover"))
    assert discover["result"]["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {},
        "resources": {},
    }
    audit = server[0].audit.tail(1)[0]
    assert audit["decision"] == "denied" and audit["args"] == "[REDACTED]"


async def test_event_error_passthrough_and_unmapped_identity(server, config, upstream):
    error = {
        "code": -32015,
        "message": "Callback endpoint verification failed",
        "data": {"reason": "challenge_failed"},
    }
    upstream["override"] = {"error": error}
    _, result = await post(server, config, message("events/subscribe", name="card.answered"))
    assert result["error"] == error
    upstream["calls"].clear()
    for method in ("events/list", "events/subscribe", "events/unsubscribe"):
        _, denied = await post(server, config, message(method, name="card.answered"), "owner")
        assert denied["error"]["data"]["error"]["code"] == "upstream_unmapped"
    assert upstream["calls"] == []


async def test_orb_notifications_never_forward_and_identity_cannot_be_claimed(
    server, config, upstream
):
    from switchboard.protocol import request_headers

    body = message("tools/call", name="orb_show", arguments={"client": "codex"})
    del body["id"]
    from .conftest import bearer

    _, url, session = server
    response = await session.post(
        url + "/mcp",
        json=body,
        headers={
            **bearer(config),
            **request_headers(body),
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status == 202 and upstream["calls"] == []
    _, called = await post(
        server,
        config,
        message(
            "tools/call",
            name="orb_status",
            arguments={"client": "codex"},
        ),
    )
    assert called["result"]["structuredContent"]["principal"] == "dot"
    assert upstream["calls"][-1][0] == "dot"
    for path in config.upstreams["orb"]["client_tokens"].values():
        token = read_token(path)
        assert token not in json.dumps(called)
        assert token not in server[0].audit.path.read_text()


@pytest.mark.parametrize("case", ["json", "shape", "id", "error", "both", "huge"])
async def test_malformed_upstream_is_contained(server, config, upstream, case):
    raw = {
        "json": b'{"PRIVATE_BODY":NaN}',
        "shape": b"[]",
        "id": b'{"jsonrpc":"2.0","id":2,"result":{}}',
        "error": b'{"jsonrpc":"2.0","id":1,"error":{"code":true}}',
        "both": b'{"jsonrpc":"2.0","id":1,"result":{},"error":{}}',
        "huge": b" " * (2 * 1024 * 1024 + 1),
    }
    upstream["raw"] = raw[case]
    _, catalog = await post(server, config, message("tools/list"))
    assert not TOOLS & {t["name"] for t in catalog["result"]["tools"]}
    _, failed = await post(server, config, message("tools/call", name="orb_status"))
    assert failed["error"]["data"]["error"]["code"] == "upstream_down"
    assert "PRIVATE_BODY" not in json.dumps(failed) + server[0].audit.path.read_text()


async def test_writes_require_allowlists_and_unknown_tools_never_forward(server, config, upstream):
    change_policy(config, lambda p: p["allowlists"].update(orb={}))
    for name in WRITE_TOOLS:
        _, denied = await post(server, config, message("tools/call", name=name))
        assert denied["error"]["data"]["error"]["code"] == "resource_denied"
    for method in ("events/subscribe", "events/unsubscribe"):
        _, denied = await post(server, config, message(method, name="card.answered"))
        assert denied["error"]["data"]["error"]["code"] == "resource_denied"
    for name in ("orb_reboot", "orb_pair", "orb_ota", "orb_finance"):
        _, denied = await post(server, config, message("tools/call", name=name))
        assert denied["error"]["data"]["error"]["code"] == "unknown_capability"
    assert upstream["calls"] == []
    _, result = await post(server, config, message("tools/call", name="orb_status"))
    assert result["result"]["isError"] is False


async def test_write_arguments_and_audit_are_private(server, config, upstream):
    args = {"card_id": "test-card", "text": "PRIVATE_CARD_TEXT"}
    _, body = await post(server, config, message("tools/call", name="orb_show", arguments=args))
    assert body["result"]["structuredContent"]["args"] == args
    assert upstream["calls"][-1][1]["params"]["arguments"] == args
    audit = server[0].audit.tail(1)[0]
    assert audit["capability"] == "orb_show" and audit["tier"] == "write"
    assert "PRIVATE_CARD_TEXT" not in server[0].audit.path.read_text()


async def test_mapping_revocation_rotation_and_no_fallback(server, config, upstream):
    core, _, _ = server
    await post(server, config, message("tools/list"))
    paths = config.upstreams["orb"]["client_tokens"]
    dot_path = paths.pop("dot")
    config.backends["orb"]["token_file"] = paths["codex"]
    _, denied = await post(server, config, message("tools/call", name="orb_status"))
    assert denied["error"]["data"]["error"]["code"] == "upstream_unmapped"
    paths["dot"] = dot_path
    await asyncio.to_thread(Path(dot_path).unlink)
    _, body = await post(server, config, message("tools/list"))
    assert not any(t["name"] in TOOLS for t in body["result"]["tools"])
    # A cache cannot survive credential replacement, nor borrow another client's catalog.
    create_token(Path(dot_path).parent, "dot")
    _, body = await post(server, config, message("tools/list"))
    assert not any(t["name"] in TOOLS for t in body["result"]["tools"])
    assert "dot" not in core.orb.cache
    _, codex = await post(server, config, message("tools/call", name="orb_status"), client="codex")
    assert codex["result"]["structuredContent"]["principal"] == "codex"
    paths["dot"] = paths["codex"]
    _, denied = await post(server, config, message("tools/call", name="orb_status"))
    assert denied["error"]["data"]["error"]["code"] == "upstream_credentials"


@pytest.mark.parametrize("kind", ["mode", "symlink", "invalid"])
async def test_private_token_checks_on_cache_hits(server, config, upstream, kind):
    await post(server, config, message("tools/list"))
    path = Path(config.upstreams["orb"]["client_tokens"]["dot"])
    if kind == "mode":
        await asyncio.to_thread(path.chmod, 0o644)
    elif kind == "symlink":
        target = path.with_suffix(".saved")
        await asyncio.to_thread(path.rename, target)
        await asyncio.to_thread(path.symlink_to, target)
    else:
        await asyncio.to_thread(path.write_text, "invalid")
    _, body = await post(server, config, message("tools/list"))
    assert not any(t["name"] in TOOLS for t in body["result"]["tools"])
    assert len(upstream["calls"]) == 1


@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_upstream_failure_preserves_local_tools_and_doctor(server, config, upstream, status):
    upstream["http"] = status
    _, catalog = await post(server, config, message("tools/list"))
    assert "ops_health" in {t["name"] for t in catalog["result"]["tools"]}
    assert not TOOLS & {t["name"] for t in catalog["result"]["tools"]}
    _, discover = await post(server, config, message("server/discover"))
    assert discover["result"]["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {},
        "resources": {},
    }
    _, local = await post(server, config, message("tools/call", name="home_list"))
    assert local["result"]["isError"] is False
    _, failed = await post(server, config, message("tools/call", name="orb_status"))
    assert failed["error"]["data"]["error"]["code"] == "upstream_down"
    doctor = await server[0].call("dot", "ops_switchboard_status", {})
    assert doctor["upstreams"]["orb"]["status"] == "degraded"
    assert "PRIVATE_BACKEND_BODY" not in json.dumps(doctor)


async def test_connection_refused_and_invalid_policy(server, config, upstream):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config.upstreams["orb"]["url"] = f"http://127.0.0.1:{port}/mcp"
    _, body = await post(server, config, message("tools/list"))
    assert not TOOLS & {t["name"] for t in body["result"]["tools"]}
    (config.config_dir / "policy.yaml").write_text("invalid: yes")
    _, denied = await post(server, config, message("tools/call", name="orb_status"))
    assert denied["error"]["data"]["error"]["code"] == "policy_invalid"
    assert upstream["calls"] == []


async def test_stdio_federated_tools_events_end_to_end(server, config, upstream):
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"port": config.port}))
    requests = [
        message("server/discover", request_id=1),
        message("tools/list", request_id=2),
        message("tools/call", request_id=3, name="orb_status"),
        message("events/list", request_id=4),
        message(
            "events/subscribe",
            request_id=5,
            name="voice.transcribed",
            delivery={
                "mode": "webhook",
                "url": "https://callback.example/hook",
                "secret": "whsec_PRIVATE_STDIO",
            },
        ),
        message("tools/call", request_id=6, name="home_list"),
    ]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "switchboard",
        "mcp-stdio",
        "--client",
        "dot",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(("\n".join(json.dumps(r) for r in requests) + "\n").encode()), 15
    )
    assert process.returncode == 0 and not stderr
    replies = {r["id"]: r for r in map(json.loads, stdout.splitlines())}
    assert "events" in replies[1]["result"]["capabilities"]
    assert TOOLS <= {t["name"] for t in replies[2]["result"]["tools"]}
    assert replies[3]["result"]["structuredContent"]["principal"] == "dot"
    assert len(replies[4]["result"]["events"]) == 2
    assert replies[5]["result"]["id"] == "sub_dot"
    assert replies[6]["result"]["isError"] is False
    assert "PRIVATE_STDIO" not in server[0].audit.path.read_text()


@pytest.mark.parametrize(
    "upstreams",
    [
        [],
        {"unknown": {}},
        {"orb": []},
        {"orb": {"token": "PRIVATE_CONFIG"}},
        {"orb": {"url": []}},
        {"orb": {"url": "http://0.0.0.0:8445/mcp"}},
        {"orb": {"url": "http://127.0.0.1:8445/other"}},
        {"orb": {"client_tokens": {"unknown": "/fake"}}},
        {"orb": {"client_tokens": {"dot": 1}}},
        {"orb": {"client_tokens": None}},
    ],
)
def test_invalid_upstream_configuration(config, upstreams):
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump({"upstreams": upstreams}))
    with pytest.raises(ValueError):
        Config.load()
