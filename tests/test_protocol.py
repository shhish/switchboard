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
    status, body = await post(server, config, message("server/discover"))
    assert status == 200
    result = body["result"]
    assert result["resultType"] == "complete"
    assert MODERN_VERSION in result["supportedVersions"]
    assert result["capabilities"] == {"tools": {"listChanged": True}}
    assert len(INSTRUCTIONS) <= 512
    assert result["instructions"] == INSTRUCTIONS
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
        assert capability.input_schema["additionalProperties"] is False
        assert capability.output_schema["type"] == "object"
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs/CAPABILITIES.md").read_text() == registry.markdown()


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
