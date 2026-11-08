"""Trusted harness confirmation is opt-in and restricted to its own approvals."""

from ..schema import obj, string
from .common import output


def register(registry):
    @registry.declare(
        "approvals_confirm",
        "Confirm approval",
        "Human approval: confirm an exact requested action. Use when confirming an exact "
        "requested action after a human agrees. Requires trusted confirmation policy and your "
        "own unexpired approval ID. Returns resolved single-use approval; notification answers "
        "alone grant nothing.",
        obj({"approval_id": string(128)}, ("approval_id",)),
        scope="approvals:confirm",
        tier="approval",
        idempotent=False,
        output_schema=output(approval_id={"type": "string"}),
        memory=None,
        memory_optout="No specific backend entity; aggregate or control-plane operation",
    )
    async def confirm(core, client, args):
        rule = core.policy.require(client, core.registry.capabilities["approvals_confirm"])
        return core.approvals.resolve(
            args["approval_id"], actor=client, trusted=rule.get("trust_client_confirmation", False)
        )
