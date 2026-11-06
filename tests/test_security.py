import json
import stat

import pytest

from switchboard.auth import Tokens, loopback_host, read_token
from switchboard.errors import CapabilityError

from .conftest import bearer, change_policy


async def test_call_enforces_hidden_scope_and_audits(core, config):
    assert "media_library_status" not in [tool["name"] for tool in core.tools("agy")]
    result = await core.call("agy", "media_library_status", {})
    assert result["error"]["code"] == "scope_denied"
    assert core.audit.tail()[-1]["decision"] == "denied"
    assert core.audit.tail()[-1]["client"] == "agy"
    change_policy(config, lambda data: data["clients"]["dot"].update(scopes=[]))
    assert (await core.call("dot", "home_list", {}))["error"]["code"] == "scope_denied"
    assert core.tools("dot") == []


async def test_unknown_client_and_bad_policy(core, config):
    assert (await core.call("untrusted", "home_list", {}))["error"]["code"] == "unknown_client"
    (config.config_dir / "policy.yaml").write_text("clients: [bad]")
    assert (await core.call("dot", "home_list", {}))["error"]["code"] == "policy_invalid"


async def test_policy_allowlist(core):
    core.policy.require_allowed("home", "scenes", "safe")
    with pytest.raises(CapabilityError, match="allowlisted"):
        core.policy.require_allowed("home", "scenes", "unsafe")


async def test_http_auth_origin_host_and_health(server, config):
    core, url, session = server
    response = await session.get(url + "/health")
    assert response.status == 200
    assert await response.json() == {"status": "ok", "service": "switchboard"}
    for overrides, expected in [
        ({}, 401),
        ({"Authorization": "Bearer unknown"}, 401),
        ({**bearer(config), "Origin": "http://evil"}, 403),
        ({**bearer(config), "Host": "evil.example"}, 403),
        ({**bearer(config), "Host": "localhost"}, 403),
    ]:
        response = await session.post(url + "/v1/call/home.list", json={}, headers=overrides)
        assert response.status == expected
    response = await session.post(
        url + "/v1/call/home.list", json={}, headers=bearer(config, "agy")
    )
    assert (await response.json())["error"]["code"] == "scope_denied"
    assert any(row["outcome"] == "unauthorized" for row in core.audit.tail(100))


@pytest.mark.parametrize(
    "host,accepted",
    [
        ("127.0.0.1:18800", True),
        ("[::1]:18800", True),
        ("0.0.0.0:18800", False),
        ("localhost:18800", False),
        ("127.0.0.1@evil", False),
        ("127.0.0.1/anything", False),
        ("127.0.0.1:0", False),
        ("127.0.0.1:99999", False),
    ],
)
def test_loopback_host(host, accepted):
    assert loopback_host(host) is accepted


def test_token_modes_hash_only_revocation(config):
    directory = config.token_dir
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    token_path = directory / "dot.token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    value = read_token(token_path)
    tokens = Tokens(directory)
    assert tokens.authenticate("Bearer " + value) == "dot"
    assert value not in repr(tokens.__dict__)
    token_path.unlink()
    assert tokens.authenticate("Bearer " + value) is None
    token_path.write_text(value)
    token_path.chmod(0o644)
    assert tokens.authenticate("Bearer " + value) is None
    token_path.unlink()
    token_path.symlink_to(directory / "orb.token")
    with pytest.raises(OSError):
        read_token(token_path)


def test_duplicate_tokens_fail_closed(config):
    value = read_token(config.token_dir / "orb.token")
    (config.token_dir / "dot.token").write_text(value)
    assert Tokens(config.token_dir).authenticate("Bearer " + value) is None


async def test_audit_redaction(core):
    args = {
        "token": "PRIVATE",
        "nested": [
            {
                "apiKey": "PRIVATE",
                "password": "PRIVATE",
                "authorization": "PRIVATE",
                "secret": "PRIVATE",
                "title": "visible",
            }
        ],
    }
    await core.call("dot", "media_library_status", args)
    row = core.audit.tail()[-1]
    assert "PRIVATE" not in json.dumps(row)
    assert row["args"]["nested"][0]["title"] == "visible"
    assert "PRIVATE" not in core.audit.path.read_text()
    assert row["duration"] >= 0
    assert {
        "ts",
        "client",
        "capability",
        "args",
        "tier",
        "decision",
        "approval_id",
        "job_id",
        "outcome",
        "duration",
        "trace_id",
        "entry_hash",
    } <= set(row)


async def test_identity_cannot_come_from_body(server, config):
    _, url, session = server
    response = await session.post(
        url + "/v1/call/media.library_status",
        json={"client": "owner"},
        headers=bearer(config, "agy"),
    )
    assert (await response.json())["error"]["code"] == "scope_denied"
