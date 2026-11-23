import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime

import pytest

from switchboard.approvals import Approvals, args_hash
from switchboard.errors import CapabilityError
from switchboard.storage import Storage

from .conftest import change_policy


async def test_approval_required_exact_args_expiry_single_use(core, fake_approval):
    core.registry.add(fake_approval)
    requested = await core.call("dot", "test.perform", {"value": "same"})
    assert requested["status"] == "approval_required"
    approval_id = requested["approval_id"]
    assert requested["expires_at"]
    assert core.resolve("owner", approval_id, True)["status"] == "approved"
    changed = await core.call(
        "dot", "test.perform", {"value": "changed", "approval_id": approval_id}
    )
    assert changed["error"]["code"] == "approval_mismatch"
    wrong_client = await core.call(
        "orb", "test.perform", {"value": "same", "approval_id": approval_id}
    )
    assert wrong_client["error"]["code"] == "approval_mismatch"
    executed = await core.call("dot", "test.perform", {"value": "same", "approval_id": approval_id})
    assert executed["status"] == "executed"
    again = await core.call("dot", "test.perform", {"value": "same", "approval_id": approval_id})
    assert again["error"]["code"] == "approval_invalid"
    new = await core.call("dot", "test.perform", {"value": "same"})
    core.approvals.now = lambda: 999999999999
    assert core.resolve("owner", new["approval_id"], True)["error"]["code"] == "approval_expired"
    expired = await core.call(
        "dot", "test.perform", {"value": "same", "approval_id": new["approval_id"]}
    )
    assert expired["error"]["code"] == "approval_expired"


async def test_trusted_confirmation_cross_client_and_denial(core, config, fake_approval):
    core.registry.add(fake_approval)
    result = await core.call("dot", "test.perform", {"value": "a"})
    approval_id = result["approval_id"]
    cross = await core.call("orb", "approvals_confirm", {"approval_id": approval_id})
    assert cross["error"]["code"] == "approval_forbidden"
    untrusted = await core.call("claude", "approvals_confirm", {"approval_id": approval_id})
    assert untrusted["error"]["code"] == "confirmation_denied"
    assert "approvals_confirm" not in [t["name"] for t in core.tools("claude")]
    confirmed = await core.call("dot", "approvals_confirm", {"approval_id": approval_id})
    assert confirmed["status"] == "approved"
    assert (await core.call("dot", "test.perform", {"value": "a", "approval_id": approval_id}))[
        "status"
    ] == "executed"
    change_policy(config, lambda p: p["clients"]["dot"].update(trust_client_confirmation=False))
    assert "approvals_confirm" not in [t["name"] for t in core.tools("dot")]
    denied_id = (await core.call("dot", "test.perform", {"value": "b"}))["approval_id"]
    assert core.resolve("dot", denied_id, True)["error"]["code"] == "owner_required"
    assert core.resolve("owner", denied_id, False)["status"] == "denied"
    assert (await core.call("dot", "test.perform", {"value": "b", "approval_id": denied_id}))[
        "error"
    ]["code"] == "approval_invalid"


async def test_pending_grant_does_not_execute(core, fake_approval):
    core.registry.add(fake_approval)
    approval_id = (await core.call("dot", "test.perform", {"value": "a"}))["approval_id"]
    result = await core.call("dot", "test.perform", {"value": "a", "approval_id": approval_id})
    assert result["error"]["code"] == "approval_invalid"


async def test_approve_and_deny_real_cli(server, config, fake_approval):
    core, _, _ = server
    core.registry.add(fake_approval)
    (config.config_dir / "config.yaml").write_text(f"port: {config.port}\n")
    for command, status in (("approve", "approved"), ("deny", "denied")):
        approval_id = (await core.call("codex", "test.perform", {"value": "cli"}))["approval_id"]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "switchboard",
            command,
            approval_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout)["status"] == status
        if command == "approve":
            assert (
                await core.call(
                    "codex", "test.perform", {"value": "cli", "approval_id": approval_id}
                )
            )["status"] == "executed"


async def test_notification_cannot_approve_and_failure_is_safe(core, fake_approval):
    class FailingNotifier:
        async def notify(self, record):
            raise RuntimeError("PRIVATE")

    core.notifier = FailingNotifier()
    core.registry.add(fake_approval)
    result = await core.call("dot", "test.perform", {"value": "a"})
    assert result["status"] == "approval_required"
    assert "failed" in result["notification_status"]
    assert "Server app" in result["notification_status"]
    assert "PRIVATE" not in json.dumps(result)
    assert core.approvals.get(result["approval_id"])["status"] == "pending"


def test_canonical_json_hash():
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})
    assert args_hash({"a": 1}) != args_hash({"a": "1"})


@pytest.fixture
def answering_surface(core, config):
    change_policy(config, lambda p: p["clients"]["cortex"]["scopes"].append("board:answer"))
    return core


async def test_pending_excludes_own_expired_resolved_and_limits_newest(answering_surface):
    core = answering_surface
    clock = [1000]
    core.approvals.now = lambda: clock[0]
    expired = core.approvals.request("dot", "home_set_secure", {})
    clock[0] += core.approvals.ttl_s  # Exactly at expiry is excluded.
    own = core.approvals.request("cortex", "home_set_secure", {})
    resolved = core.approvals.request("dot", "home_set_secure", {})
    core.approvals.resolve(resolved["approval_id"], actor="owner")
    ids = []
    for _ in range(25):
        clock[0] += 1
        ids.append(core.approvals.request("dot", "home_set_secure", {})["approval_id"])
    result = await core.call("cortex", "approvals_pending", {})
    assert result["status"] == "ok", result
    approvals = result["approvals"]
    assert [r["id"] for r in approvals] == list(reversed(ids))[:20]
    assert not {expired["approval_id"], own["approval_id"], resolved["approval_id"]} & {
        r["id"] for r in approvals
    }
    assert set(approvals[0]) == {
        "id",
        "client",
        "capability",
        "title",
        "summary",
        "detail",
        "created",
        "expires",
    }
    assert approvals[0]["title"] == "Control sensitive accessory"
    assert approvals[0]["summary"] == "Approve home_set_secure for dot: exact requested action"
    assert approvals[0]["detail"] == []
    assert datetime.fromisoformat(approvals[0]["created"]).timestamp() == clock[0]
    assert (
        datetime.fromisoformat(approvals[0]["expires"]).timestamp()
        == clock[0] + core.approvals.ttl_s
    )
    # The pending API projects only display metadata, never hashes or stored status.
    assert "args_hash" not in json.dumps(result)


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_owner_surface_answer_consume_and_audit(answering_surface, fake_approval, decision):
    core = answering_surface
    core.registry.add(fake_approval)
    args = {"value": "same"}
    requested = await core.call("dot", "test_perform", args)
    approval_id = requested["approval_id"]
    answer = {"approval_id": approval_id, "decision": decision}
    result = await core.call("cortex", "approvals_answer", answer)
    assert result["status"] == ("approved" if decision == "approve" else "denied"), result
    audit = core.storage.db.execute(
        "SELECT client,approval_id,tier,args_json FROM audit_log "
        "WHERE capability='approvals_answer' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert (audit["client"], audit["approval_id"], audit["tier"]) == (
        "cortex",
        approval_id,
        "write",
    )
    assert json.loads(audit["args_json"]) == answer
    assert (await core.call("cortex", "approvals_pending", {}))["approvals"] == []
    consumed = await core.call("dot", "test_perform", {**args, "approval_id": approval_id})
    if decision == "approve":
        assert consumed["status"] == "executed", consumed
    else:
        assert consumed["error"]["code"] == "approval_invalid"
    assert (await core.call("cortex", "approvals_answer", answer))["error"][
        "code"
    ] == "approval_invalid"
    assert (await core.call("dot", "test_perform", {**args, "approval_id": approval_id}))["error"][
        "code"
    ] == "approval_invalid"


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_owner_surface_cannot_answer_itself(answering_surface, decision):
    core = answering_surface
    approval_id = core.approvals.request("cortex", "home_set_secure", {})["approval_id"]
    result = await core.call(
        "cortex", "approvals_answer", {"approval_id": approval_id, "decision": decision}
    )
    assert result["error"]["code"] == "approval_forbidden"
    assert core.approvals.get(approval_id)["status"] == "pending"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("approvals_pending", {}),
        ("approvals_answer", {"approval_id": "apr_missing", "decision": "approve"}),
    ],
)
async def test_owner_surface_scope_required(core, tool, args):
    assert tool not in {t["name"] for t in core.tools("dot")}
    assert (await core.call("dot", tool, args))["error"]["code"] == "scope_denied"


@pytest.mark.parametrize(
    "state,code",
    [
        ("unknown", "approval_invalid"),
        ("expired", "approval_expired"),
        ("approved", "approval_invalid"),
        ("denied", "approval_invalid"),
    ],
)
async def test_owner_surface_resolve_errors_unchanged(answering_surface, state, code):
    core = answering_surface
    approval_id = "apr_missing"
    if state != "unknown":
        approval_id = core.approvals.request("dot", "home_set_secure", {})["approval_id"]
        if state == "expired":
            core.approvals.now = lambda: 999999999999
        else:
            core.approvals.resolve(approval_id, actor="owner", approve=state == "approved")
    with pytest.raises(CapabilityError) as expected:
        core.approvals.resolve(approval_id, actor="owner")
    assert expected.value.code == code
    result = await core.call(
        "cortex", "approvals_answer", {"approval_id": approval_id, "decision": "approve"}
    )
    assert result["error"]["code"] == code


async def test_detail_adapter_bounded_sanitized_and_null_fallback(answering_surface, fake_approval):
    core = answering_surface
    core.registry.add(fake_approval)
    first = await core.call("dot", "test_perform", {"value": "PRIVATE raw arguments"})
    assert core.approvals.get(first["approval_id"])["detail"] is None
    assert (
        core.storage.db.execute(
            "SELECT detail FROM approvals WHERE id=?", (first["approval_id"],)
        ).fetchone()[0]
        is None
    )
    core.registry.capabilities["test_perform"] = replace(
        fake_approval,
        approval_detail=lambda core, args: (
            [{"label": "Change", "value": "\nhello\x00  " + "x" * 100}] * 6
        ),
    )
    second = await core.call("dot", "test_perform", {"value": "PRIVATE raw arguments"})
    row = core.approvals.get(second["approval_id"])
    assert row["detail"] == [{"label": "Change", "value": "hello " + "x" * 74}] * 4
    pending = await core.call("cortex", "approvals_pending", {})
    assert pending["approvals"][0]["detail"] == row["detail"]
    assert pending["approvals"][1]["detail"] == []
    assert "PRIVATE" not in json.dumps(pending)


@pytest.mark.parametrize("cache_state", ["missing", "fresh", "expired"])
async def test_home_approval_details(core, config, cache_state, monkeypatch):
    change_policy(config, lambda p: p["clients"]["dot"]["scopes"].append("home:secure"))
    cached = cache_state == "fresh"

    async def no_lookup(*args, **kwargs):
        raise AssertionError("Approval details must never fetch backend metadata")

    monkeypatch.setattr(core.backends, "get", no_lookup)
    if cache_state != "missing":
        core.context.cache["home"] = (
            time.monotonic() + 300,
            {
                "entities": [
                    {"kind": "accessory", "id": "lock-1", "name": "Front\n door\x00"},
                    {"kind": "scene", "id": "scene-1", "name": "Bedtime"},
                ]
            },
        )
        if cache_state == "expired":
            core.context.cache["home"] = (0, core.context.cache["home"][1])
    for tool, args, detail in [
        (
            "home_set_secure",
            {"accessory_id": "lock-1", "action": "set", "characteristic": "lockTarget", "value": 0},
            [
                {"label": "Accessory", "value": "Front door" if cached else "lock-1"},
                {"label": "Change", "value": "lockTarget → 0"},
            ],
        ),
        (
            "home_set_secure",
            {"accessory_id": "lock-1", "action": "off"},
            [
                {"label": "Accessory", "value": "Front door" if cached else "lock-1"},
                {"label": "Change", "value": "off"},
            ],
        ),
        (
            "home_run_scene_secure",
            {"scene_id": "scene-1"},
            [
                {"label": "Scene", "value": "Bedtime" if cached else "scene-1"},
                {"label": "Change", "value": "Run scene"},
            ],
        ),
    ]:
        requested = await core.call("dot", tool, args)
        assert requested["status"] == "approval_required", requested
        assert core.approvals.get(requested["approval_id"])["detail"] == detail
        assert requested["summary"] == f"Approve {tool} for dot: exact requested action"


def test_existing_resolution_paths_stay_separate(answering_surface):
    core = answering_surface
    own = core.approvals.request("owner", "home_set_secure", {})["approval_id"]
    assert core.approvals.resolve(own, actor="owner")["status"] == "approved"
    other = core.approvals.request("dot", "home_set_secure", {})["approval_id"]
    # Possessing board:answer does not change either existing resolver mode.
    for trusted in (False, True):
        with pytest.raises(CapabilityError) as error:
            core.approvals.resolve(other, actor="cortex", trusted=trusted)
        assert error.value.code == "approval_forbidden"
    assert core.approvals.resolve(other, actor="dot", trusted=True)["status"] == "approved"


def test_old_approval_database_migration(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    db = sqlite3.connect(root / "state.sqlite3")
    db.execute(
        "CREATE TABLE approvals (id TEXT PRIMARY KEY, client TEXT NOT NULL, "
        "capability TEXT NOT NULL, args_hash TEXT NOT NULL, status TEXT NOT NULL, "
        "summary TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "resolved_at REAL)"
    )
    db.execute(
        "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "apr_old",
            "dot",
            "home_set_secure",
            args_hash({}),
            "pending",
            "Original summary",
            1000,
            5000,
            None,
        ),
    )
    db.commit()
    db.close()
    for _ in range(2):
        storage = Storage(root)
        try:
            row = storage.db.execute("SELECT * FROM approvals WHERE id='apr_old'").fetchone()
            assert row["summary"] == "Original summary" and row["detail"] is None
            approvals = Approvals(storage, now=lambda: 2000)
            assert (
                next(r for r in approvals.pending("cortex") if r["id"] == "apr_old")["detail"]
                is None
            )
            new = approvals.request(
                "dot", "home_set_secure", {}, detail=[{"label": "Change", "value": "Lock"}]
            )
            assert approvals.get(new["approval_id"])["detail"] == [
                {"label": "Change", "value": "Lock"}
            ]
        finally:
            storage.close()
