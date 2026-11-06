import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from switchboard.domains import build_registry
from switchboard.protocol import (
    CAPABILITIES_KEY,
    INSTRUCTIONS,
    MODERN_VERSION,
    VERSION_KEY,
    request_headers,
    server_instructions,
)

from .conftest import bearer


def message(method="ping", modern=True, request_id=1, **params):
    if modern:
        params["_meta"] = {VERSION_KEY: MODERN_VERSION, CAPABILITIES_KEY: {}}
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


async def post(server, config, body, client="dot", overrides=None):
    _, url, session = server
    headers = {
        **bearer(config, client),
        **request_headers(body),
        "Accept": "application/json, text/event-stream",
        **(overrides or {}),
    }
    response = await session.post(url + "/mcp", json=body, headers=headers)
    return response.status, await response.json()


async def test_modern_stateless_discover_list_and_call(server, config):
    core, _, _ = server
    status, body = await post(server, config, message("server/discover"))
    assert status == 200
    result = body["result"]
    assert result["resultType"] == "complete"
    assert MODERN_VERSION in result["supportedVersions"]
    assert result["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {},
        "resources": {},
    }
    assert len(result["instructions"]) <= 3500
    assert result["instructions"].startswith(INSTRUCTIONS)
    assert result["instructions"] == server_instructions(t["name"] for t in core.tools("dot"))
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "switchboard"
    _, catalog = await post(server, config, message("tools/list"))
    tools = catalog["result"]["tools"]
    assert [t["name"] for t in tools] == sorted(t["name"] for t in tools)
    _, limited = await post(server, config, message("tools/list"), client="agy")
    assert [t["name"] for t in limited["result"]["tools"]] == ["jobs_get", "jobs_list"]
    _, called = await post(
        server, config, message("tools/call", name="home_get", arguments={"accessory_id": "a1"})
    )
    assert called["result"]["resultType"] == "complete"
    assert called["result"]["structuredContent"]["accessory"]["id"] == "a1"
    assert called["result"]["isError"] is False


async def test_legacy_initialize_and_mixed_eras(server, config):
    init = message(
        "initialize",
        modern=False,
        protocolVersion="2025-06-18",
        capabilities={},
        clientInfo={"name": "test", "version": "1"},
    )
    status, body = await post(server, config, init)
    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert "resultType" not in body["result"]
    _, body = await post(server, config, message("tools/list", modern=False))
    assert "resultType" not in body["result"]
    _, modern = await post(server, config, message("ping"))
    assert modern["result"]["resultType"] == "complete"
    _, body = await post(server, config, message("ping", modern=False))
    assert body["result"] == {}


@pytest.mark.parametrize(
    "overrides",
    [{"Mcp-Method": "wrong"}, {"Mcp-Name": "wrong"}, {"MCP-Protocol-Version": "2025-03-26"}],
)
async def test_mirrored_headers_required(server, config, overrides):
    status, body = await post(
        server, config, message("tools/call", name="home_list"), overrides=overrides
    )
    assert status == 400
    assert body["error"]["code"] == -32020


async def test_invalid_protocol_and_method_and_metadata(server, config):
    request = message()
    request["params"]["_meta"][VERSION_KEY] = "2099-01-01"
    status, body = await post(server, config, request)
    assert status == 400
    assert body["error"]["code"] == -32022
    _, body = await post(server, config, message("missing/method"))
    assert body["error"]["code"] == -32601
    request = message()
    del request["params"]["_meta"][CAPABILITIES_KEY]
    _, body = await post(server, config, request)
    assert body["error"]["code"] == -32602
    _, body = await post(
        server, config, message("tools/call", name="home_list", arguments={"extra": 1})
    )
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["error"]["code"] == "invalid_arguments"
    _, body = await post(server, config, message("tools/call", name="home_list"), client="agy")
    assert body["result"]["structuredContent"]["error"]["code"] == "scope_denied"


async def test_mcp_approval_result_is_complete(server, config, fake_approval):
    core, _, _ = server
    core.registry.add(fake_approval)
    _, body = await post(
        server, config, message("tools/call", name="test.perform", arguments={"value": "x"})
    )
    assert body["result"]["resultType"] == "complete"
    assert body["result"]["structuredContent"]["status"] == "approval_required"
    assert body["result"]["isError"] is False


async def test_notifications_never_execute(server, config, fake_approval):
    core, url, session = server
    core.registry.add(fake_approval)
    body = message("tools/call", name="test.perform", arguments={"value": "x"})
    del body["id"]
    response = await session.post(
        url + "/mcp",
        json=body,
        headers={
            **bearer(config),
            **request_headers(body),
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status == 202
    assert core.approvals.list() == []


async def test_stdio_subprocess_round_trip_both_eras(server, config):
    (config.config_dir / "config.yaml").write_text(f"port: {config.port}\n")
    requests = [
        message(
            "initialize",
            modern=False,
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={"name": "test", "version": "1"},
        ),
        message("tools/list", modern=False, request_id=2),
        message("server/discover", request_id=3),
        message("tools/call", request_id=4, name="home_get", arguments={"accessory_id": "a1"}),
    ]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "switchboard",
        "mcp-stdio",
        "--client",
        "codex",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(
            ("\n".join(json.dumps(request) for request in requests) + "\n").encode()
        ),
        15,
    )
    assert process.returncode == 0, stderr.decode()
    replies = {reply["id"]: reply for reply in map(json.loads, stdout.splitlines())}
    assert replies[1]["result"]["protocolVersion"] == "2025-11-25"
    assert "resultType" not in replies[2]["result"]
    assert replies[3]["result"]["resultType"] == "complete"
    assert replies[4]["result"]["structuredContent"]["accessory"]["id"] == "a1"


def test_annotations_and_manifest_documentation():
    registry = build_registry()
    for capability in registry.capabilities.values():
        hints = capability.annotations
        assert hints["readOnlyHint"] == (capability.tier == "read")
        assert hints["destructiveHint"] == (capability.tier == "approval")
        assert hints["openWorldHint"] is False
        assert hints["idempotentHint"] == capability.idempotent
        assert capability.input_schema["additionalProperties"] is False
        assert capability.output_schema["type"] == "object"
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs/CAPABILITIES.md").read_text() == registry.markdown()


@pytest.mark.parametrize("modern", [True, False])
async def test_declarative_tool_meta_both_eras(server, config, modern):
    core, _, _ = server
    from .conftest import change_policy

    change_policy(
        config,
        lambda p: p["clients"]["owner"].update(
            scopes=sorted({cap.scope for cap in core.registry.capabilities.values()}),
            trust_client_confirmation=True,
        ),
    )
    _, reply = await post(server, config, message("tools/list", modern=modern), client="owner")
    tools = {t["name"]: t for t in reply["result"]["tools"]}
    assert {
        name for name, tool in tools.items() if tool.get("_meta", {}).get("anthropic/alwaysLoad")
    } == {"ops_health", "context_resolve", "switchboard_report_issue"}
    large = {"ops_service_logs", "notes_get", "recipes_get", "audit_query", "audit_trace"}
    for name, cap in core.registry.capabilities.items():
        tool = tools[name]
        meta = tool.get("_meta", {})
        if cap.tier == "approval":
            assert meta["anthropic/requiresUserInteraction"] is True
        else:
            assert "anthropic/requiresUserInteraction" not in meta
        assert bool(meta.get("anthropic/alwaysLoad")) == cap.always_load
        if name in large or name.endswith("_log"):
            assert 50_000 < meta["anthropic/maxResultSizeChars"] <= 500_000
            assert meta["anthropic/maxResultSizeChars"] == cap.max_result_chars
        assert tool["annotations"] == cap.annotations


def test_registry_rejects_union_inputs_and_invalid_meta():
    from dataclasses import replace

    cap = build_registry().capabilities["home_get"]
    for keyword in ("oneOf", "anyOf", "allOf"):
        with pytest.raises(ValueError, match="single flat object"):
            replace(cap, input_schema={**cap.input_schema, keyword: [{"required": ["name"]}]})
    for size in (0, 500_001, True, "100000"):
        with pytest.raises(ValueError, match="max_result_chars"):
            replace(cap, max_result_chars=size)
    with pytest.raises(ValueError, match="always_load"):
        replace(cap, always_load="true")


def test_instructions_domains_follow_registered_tools():
    names = build_registry().capabilities
    instructions = server_instructions(names)
    assert len(instructions) <= 3500
    assert "console_*: PS5" in instructions and "media_*: Plex" in instructions
    assert "storage_*: disk space" in instructions and "git_*: Gitea" in instructions
    assert "orb_*:" not in instructions
    extended = server_instructions([*names, "orb_show"])
    assert "orb_*:" in extended
    assert len(extended) <= 3500
    limited = server_instructions(["jobs_get"])
    assert "jobs_*:" in limited and "home_*:" not in limited


@pytest.mark.parametrize("modern", [True, False])
@pytest.mark.parametrize(
    "method,params,overrides",
    [
        (
            "tools/call",
            {"name": "home_get", "arguments": {"accessory_id": "a1"}},
            {"Mcp-Name": "wrong"},
        ),
        ("prompts/get", {"name": "switchboard"}, {"Mcp-Name": "wrong"}),
        ("resources/read", {"uri": "switchboard://skills/switchboard"}, {"Mcp-Name": "wrong"}),
        ("ping", {}, {"Mcp-Method": "tools/call"}),
        ("ping", {}, {"Mcp-Name": "unexpected"}),
    ],
)
async def test_present_headers_must_match_in_both_eras(
    server, config, modern, method, params, overrides
):
    core, _, _ = server
    before = core.audit.tail()
    status, body = await post(
        server, config, message(method, modern=modern, **params), overrides=overrides
    )
    assert status == 400 and body["error"]["code"] == -32020
    assert core.audit.tail() == before  # No mismatched request is dispatched.


@pytest.mark.parametrize("modern", [True, False])
async def test_header_mismatch_on_notifications_is_rejected(server, config, modern):
    _, url, session = server
    body = message("ping", modern=modern)
    del body["id"]
    response = await session.post(
        url + "/mcp",
        json=body,
        headers={
            **bearer(config),
            **request_headers(body),
            "Mcp-Method": "tools/call",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32020


async def test_legacy_headers_optional_but_modern_mirrors_required(server, config):
    _, url, session = server
    for modern in (False, True):
        response = await session.post(
            url + "/mcp",
            json=message("ping", modern=modern),
            headers={
                **bearer(config),
                "Accept": "application/json, text/event-stream",
            },
        )
        assert response.status == (400 if modern else 200)


async def test_stdio_emits_tools_list_changed_when_catalog_changes(config):
    from aiohttp import web

    from switchboard.server import CATALOG_HEADER

    from .conftest import running_app

    versions = iter(["v1", "v1", "v2"])

    async def mcp(request):
        body = await request.json()
        response = web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": {}})
        response.headers[CATALOG_HEADER] = next(versions)
        return response

    app = web.Application()
    app.router.add_post("/mcp", mcp)
    async with running_app(app) as url:
        port = url.rsplit(":", 1)[1]
        (config.config_dir / "config.yaml").write_text(f"port: {port}\n")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "switchboard",
            "mcp-stdio",
            "--client",
            "codex",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        lines = []
        for request_id in (1, 2, 3):
            # One at a time so the version sequence is deterministic.
            process.stdin.write(
                (json.dumps(message("ping", request_id=request_id)) + "\n").encode()
            )
            await process.stdin.drain()
            lines.append(json.loads(await asyncio.wait_for(process.stdout.readline(), 10)))
        lines.append(json.loads(await asyncio.wait_for(process.stdout.readline(), 10)))
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
    notices = [line for line in lines if line.get("method") == "notifications/tools/list_changed"]
    assert len(notices) == 1
    assert [line.get("id") for line in lines if "id" in line] == [1, 2, 3]


@pytest.mark.parametrize("version", ["2025-03-26", MODERN_VERSION])
async def test_parse_and_unknown_id_errors_match_protocol_era(server, config, version):
    _, url, session = server
    headers = {
        **bearer(config),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": version,
    }
    async with session.post(url + "/mcp", data=b"{", headers=headers) as response:
        result = await response.json()
        assert result["error"]["code"] == -32700
        assert ("id" in result) == (version != MODERN_VERSION)
        if version != MODERN_VERSION:
            assert result["id"] is None
    for value in ([], message(modern=version == MODERN_VERSION, request_id=None)):
        mirrored = {**headers, **request_headers(value, version)}
        async with session.post(url + "/mcp", json=value, headers=mirrored) as response:
            result = await response.json()
            assert result["error"]["code"] == -32600
            assert ("id" in result) == (version != MODERN_VERSION)


@pytest.mark.parametrize("modern", [False, True])
async def test_stdio_parse_errors_use_connection_version(server, config, modern):
    (config.config_dir / "config.yaml").write_text(f"port: {config.port}\n")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "switchboard",
        "mcp-stdio",
        "--client",
        "codex",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    if modern:
        init = message("server/discover")
    else:
        init = message(
            "initialize",
            modern=False,
            protocolVersion="2025-03-26",
            capabilities={},
            clientInfo={"name": "test", "version": "1"},
        )
    stdout, stderr = await asyncio.wait_for(
        process.communicate((json.dumps(init) + "\n{\n").encode()), 5
    )
    assert process.returncode == 0, stderr.decode()
    result = next(reply for reply in map(json.loads, stdout.splitlines()) if reply.get("error"))
    assert result["error"]["code"] == -32700
    assert ("id" in result) == (not modern)
    if not modern:
        assert result["id"] is None
