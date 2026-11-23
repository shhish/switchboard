"""Owner-surface answers and opt-in trusted harness confirmation."""

from datetime import UTC, datetime

from ..schema import obj, string
from .common import array, output

DETAIL = {
    **array(
        obj({"label": string(80), "value": {"type": "string", "maxLength": 80}}, ("label", "value"))
    ),
    "maxItems": 4,
}
PENDING = obj(
    {
        "id": string(128),
        "client": string(),
        "capability": string(),
        "title": string(),
        "summary": string(),
        "detail": DETAIL,
        "created": string(),
        "expires": string(),
    },
    ("id", "client", "capability", "title", "summary", "detail", "created", "expires"),
)


def register(registry):
    @registry.declare(
        "approvals_pending",
        "List pending one-time approvals",
        "Pending one-time approvals for the owner. Use when showing Needs you in the Server "
        "app. Requires board:answer. Returns up to 20 unexpired pending approvals, newest "
        "first, with safe details; excludes your own requests.",
        scope="board:answer",
        tier="read",
        output_schema=output(approvals={**array(PENDING), "maxItems": 20}),
        memory=None,
        memory_optout="Owner approval queue; no backend entity",
    )
    async def pending(core, client, args):
        approvals = []
        for row in core.approvals.pending(client):
            cap = registry.capabilities.get(row["capability"])
            approvals.append(
                {
                    "id": row["id"],
                    "client": row["client"],
                    "capability": row["capability"],
                    "title": cap.title if cap else row["capability"],
                    "summary": row["summary"],
                    "detail": row["detail"] or [],
                    "created": datetime.fromtimestamp(row["created_at"], UTC).isoformat(),
                    "expires": datetime.fromtimestamp(row["expires_at"], UTC).isoformat(),
                }
            )
        return {"status": "ok", "approvals": approvals}

    @registry.declare(
        "approvals_answer",
        "Answer one-time approval",
        "One-time approval answer on the owner's behalf. Use when the owner chooses Allow "
        "once or Deny in the Server app. Requires board:answer and another client's pending "
        "unexpired approval. Returns the approved or denied grant; every answer is audited.",
        obj(
            {
                "approval_id": string(128),
                "decision": {"type": "string", "enum": ["approve", "deny"]},
            },
            ("approval_id", "decision"),
        ),
        scope="board:answer",
        tier="write",
        idempotent=False,
        output_schema=output(approval_id=string(128)),
        memory=None,
        memory_optout="Owner approval answer; no backend entity",
    )
    async def answer(core, client, args):
        return core.approvals.resolve(
            args["approval_id"],
            actor=client,
            approve=args["decision"] == "approve",
            owner_surface_answer=True,
        )

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
