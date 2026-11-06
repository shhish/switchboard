"""Agents report problems and ideas here; coding agents read and close them."""

import secrets
import time

from ..errors import CapabilityError
from ..names import canonical_name
from ..schema import obj, string
from .common import array, output

KINDS = ["bug", "improvement", "missing_capability", "ops"]
MAX_OPEN_PER_CLIENT = 50

ISSUE = obj(
    {
        "issue_id": string(32),
        "client": string(64),
        "kind": string(64),
        "title": {"type": "string", "maxLength": 200},
        "details": {"type": "string"},
        "capability": {"type": ["string", "null"]},
        "status": string(32),
        "resolution": {"type": ["string", "null"]},
        "created_at": {"type": "number"},
    },
    (
        "issue_id",
        "client",
        "kind",
        "title",
        "details",
        "capability",
        "status",
        "resolution",
        "created_at",
    ),
)


def row(record):
    return {
        "issue_id": record["id"],
        "client": record["client"],
        "kind": record["kind"],
        "title": record["title"],
        "details": record["details"],
        "capability": canonical_name(record["capability"]),
        "status": record["status"],
        "resolution": record["resolution"],
        "created_at": record["created_at"],
    }


def register(registry):
    @registry.declare(
        "switchboard_report_issue",
        "Report a Switchboard issue",
        "Switchboard issue reporting: bugs and missing capabilities. Use when a Switchboard "
        "tool fails, is confusing or lacks a capability. Requires a short issue title and "
        "details without secrets. Returns a recorded issue ID; repeated reports may "
        "deduplicate.",
        obj(
            {
                "kind": {"type": "string", "enum": KINDS},
                "title": string(120),
                "details": string(2000),
                "capability": string(64),
            },
            ("kind", "title", "details"),
        ),
        always_load=True,
        scope="feedback:write",
        tier="write",
        idempotent=False,
        output_schema=output(issue_id={"type": "string"}, duplicate={"type": "boolean"}),
    )
    async def report(core, client, args):
        args = {**args, "capability": canonical_name(args.get("capability"))}
        db = core.storage.db
        existing = db.execute(
            "SELECT id FROM issues WHERE status='open' AND title=? "
            "AND REPLACE(IFNULL(capability,''),'.','_')=?",
            (args["title"], args.get("capability") or ""),
        ).fetchone()
        if existing:
            return {"status": "ok", "issue_id": existing["id"], "duplicate": True}
        (count,) = db.execute(
            "SELECT COUNT(*) FROM issues WHERE status='open' AND client=?", (client,)
        ).fetchone()
        if count >= MAX_OPEN_PER_CLIENT:
            raise CapabilityError("issue_limit", "Too many open issues from this client")
        now = time.time()
        issue_id = "iss_" + secrets.token_hex(6)
        with db:
            db.execute(
                """INSERT INTO issues
                (id,client,kind,title,details,capability,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'open',?,?)""",
                (
                    issue_id,
                    client,
                    args["kind"],
                    args["title"],
                    args["details"],
                    args.get("capability"),
                    now,
                    now,
                ),
            )
        return {"status": "ok", "issue_id": issue_id, "duplicate": False}

    @registry.declare(
        "switchboard_list_issues",
        "List Switchboard issues",
        "Switchboard reported issues and resolutions. Use when starting Switchboard maintenance "
        "or reviewing bugs. Requires feedback read access. Returns bounded reported issues and "
        "resolutions.",
        obj(
            {
                "status": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }
        ),
        scope="feedback:read",
        output_schema=output(issues=array(ISSUE)),
    )
    async def list_issues(core, client, args):
        status = args.get("status", "open")
        limit = args.get("limit", 50)
        where, params = ("", ()) if status == "all" else ("WHERE status=?", (status,))
        rows = core.storage.db.execute(
            f"SELECT * FROM issues {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        return {
            "status": "ok",
            "issues": [row(r) for r in rows[:limit]],
            "limit": limit,
            "truncated": len(rows) > limit,
        }

    @registry.declare(
        "switchboard_close_issue",
        "Close a Switchboard issue",
        "Switchboard issue resolution. Use when recording a fixed or declined Switchboard "
        "issue. Requires an open issue ID and resolution. Returns the closed issue ID.",
        obj({"issue_id": string(32), "resolution": string(500)}, ("issue_id", "resolution")),
        scope="feedback:manage",
        tier="write",
        output_schema=output(issue_id={"type": "string"}),
    )
    async def close(core, client, args):
        db = core.storage.db
        with db:
            changed = db.execute(
                "UPDATE issues SET status='closed', resolution=?, updated_at=? "
                "WHERE id=? AND status='open'",
                (args["resolution"], time.time(), args["issue_id"]),
            ).rowcount
        if not changed:
            raise CapabilityError("issue_not_open", "No open issue with that id")
        return {"status": "ok", "issue_id": args["issue_id"]}
