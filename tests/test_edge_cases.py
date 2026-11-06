import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

from switchboard import cli
from switchboard.config import Config
from switchboard.protocol import CAPABILITIES_KEY, VERSION_KEY, request_headers
from switchboard.registry import Capability
from switchboard.schema import obj
from switchboard.server import load_json

from .conftest import bearer, change_policy


@pytest.mark.parametrize("invalid", ['{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}'])
def test_strict_json_parser(invalid):
    with pytest.raises(ValueError):
        load_json(invalid)


@pytest.mark.parametrize(
    "invalid",
    [
        "[",
        "[]",
        "false",
        "backends: {media: {url: []}}",
        "backends: {home: {token_file: 12}}",
        "port: 0",
        "port: true",
        "approval_ttl_s: -1",
        "backends: {media: {url: 'http://0.0.0.0:8010'}}",
        "backends: {media: {url: 'http://127.0.0.1:8010/?secret=bad'}}",
        "backends: {media: {api_key: DO_NOT_ECHO}}",
    ],
)
def test_bad_config_fails_safely(config, invalid, monkeypatch, capsys):
    (config.config_dir / "config.yaml").write_text(invalid)
    monkeypatch.setattr(sys, "argv", ["sb", "token", "list"])
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "error"
    assert "DO_NOT_ECHO" not in captured.out


def test_example_config_policy_load(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("SWITCHBOARD_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text((root / "config/config.example.yaml").read_text())
    from switchboard.auth import CLIENTS
    from switchboard.policy import Policy

    policy = Policy(root / "config/policy.example.yaml")
    assert set(policy.load()["clients"]) == set(CLIENTS)
    loaded = Config.load()
    assert loaded.port == 18800
    assert loaded.backends["orb"]["enabled"] is False


async def test_owner_cannot_cross_confirm_even_if_trusted(core, config, fake_approval):
    change_policy(
        config,
        lambda p: p["clients"]["owner"].update(
            scopes=p["clients"]["owner"]["scopes"] + ["approvals:confirm"],
            trust_client_confirmation=True,
        ),
    )
    core.registry.add(fake_approval)
    approval_id = (await core.call("dot", "test.perform", {"value": "x"}))["approval_id"]
    result = await core.call("owner", "approvals_confirm", {"approval_id": approval_id})
    assert result["error"]["code"] == "approval_forbidden"
    assert core.resolve("owner", approval_id, True)["status"] == "approved"


async def test_approval_summary_redacts_and_describes_args(core):
    approval = core.approvals.request("dot", "test.action", {"title": "Movie", "secret": "PRIVATE"})
    assert "Movie" in approval["summary"]
    assert "PRIVATE" not in approval["summary"]
    assert "secret" not in approval["summary"]


async def test_immediate_job_cancel_cleans_task_registry(core):
    async def operation(_):
        await asyncio.Event().wait()

    handle = core.jobs.submit("test.work", "dot", operation)
    task = core.jobs.tasks[handle["job_id"]]
    core.jobs.cancel(handle["job_id"], "dot")
    await asyncio.gather(task, return_exceptions=True)
    assert handle["job_id"] not in core.jobs.tasks
    assert core.jobs.get(handle["job_id"], "dot")["status"] == "cancelled"


async def test_timeout_and_invalid_output_are_safe(core):
    async def blocked(*_):
        await asyncio.Event().wait()

    core.registry.add(
        Capability(
            "test.timeout", "Wait", "Test timeout", obj(), "ops:read", blocked, timeout_s=0.001
        )
    )
    result = await core.call("dot", "test.timeout", {})
    assert result["error"]["code"] == "timeout"

    async def invalid(*_):
        return {"status": "ok", "nested": {"value": float("inf")}}

    core.registry.add(
        Capability("test.invalid", "Invalid", "Test invalid output", obj(), "ops:read", invalid)
    )
    assert (await core.call("dot", "test.invalid", {}))["error"]["code"] == "handler_invalid"


async def test_doctor_bad_backend_shape_and_homekit_tcc(core, fake_backends):
    fake_backends[0]["/api/state"] = []
    fake_backends[0]["/api/v1/health"]["homekit"] = "notAuthorized"
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["backends"]["frame"]["status"] == "down"
    assert result["backends"]["home"]["status"] == "degraded"


async def test_movie_id_not_overridden_by_search(core):
    result = await core.call("dot", "media_search", {"query": "Dune 0", "kind": "movie"})
    assert result["library"][0]["tmdb_id"] == 100
    assert result["overseerr"][0]["tmdb_id"] == 900


async def test_json_cli_read_and_doctor(server, config):
    _, url, _ = server
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"port": config.port, "backends": config.backends})
    )
    result = await cli.execute(
        cli.parser().parse_args(
            ["--client", "codex", "call", "home_get", "--json", '{"accessory_id":"a1"}']
        ),
        config,
    )
    assert result["accessory"]["id"] == "a1"
    result = await cli.execute(cli.parser().parse_args(["doctor"]), Config.load())
    assert result["backends"]["media"]["status"] == "ok"
    assert "fake-home-read" not in json.dumps(result)


def test_token_cli_never_prints_value(config, monkeypatch, capsys):
    token_path = config.token_dir / "dot.token"
    token_path.unlink()
    monkeypatch.setattr(sys, "argv", ["sb", "token", "create", "dot"])
    assert cli.main() == 0
    from switchboard.auth import read_token

    value = read_token(token_path)
    assert value not in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["sb", "token", "list"])
    assert cli.main() == 0
    assert "dot" in json.loads(capsys.readouterr().out)["clients"]
    monkeypatch.setattr(sys, "argv", ["sb", "token", "revoke", "dot"])
    assert cli.main() == 0
    assert not token_path.exists()
    assert value not in capsys.readouterr().out


async def test_bad_transport_json_and_missing_header_and_owner_routes(server, config):
    _, url, session = server
    response = await session.post(
        url + "/mcp",
        data='{"value":1e999}',
        headers={
            **bearer(config),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32700
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ping",
        "params": {"_meta": {VERSION_KEY: "2026-07-28", CAPABILITIES_KEY: {}}},
    }
    headers = {
        **bearer(config),
        **request_headers(body),
        "Accept": "application/json, text/event-stream",
    }
    del headers["Mcp-Method"]
    response = await session.post(url + "/mcp", json=body, headers=headers)
    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32020
    for path in ("/v1/approvals", "/v1/audit"):
        response = await session.get(url + path, headers=bearer(config, "dot"))
        assert response.status == 403
        response = await session.get(url + path, headers=bearer(config, "owner"))
        assert response.status == 200
