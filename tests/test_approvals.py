import asyncio
import json
import os
import sys

from switchboard.approvals import args_hash

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
    assert "PRIVATE" not in json.dumps(result)
    assert core.approvals.get(result["approval_id"])["status"] == "pending"


def test_canonical_json_hash():
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})
    assert args_hash({"a": 1}) != args_hash({"a": "1"})
