"""Read-only views of the audit log: what agents did, why, and what went wrong.

`audit:read` shows a client its own calls (like jobs). `audit:read_all` (and the
owner) see every client. Stored arguments are already reduced to safe values.
"""

import time

from ..errors import CapabilityError
from ..schema import obj, string
from .common import array, output

FIELDS = (
    "id",
    "ts",
    "client",
    "client_name",
    "origin",
    "capability",
    "tier",
    "decision",
    "outcome",
    "error_code",
    "approval_id",
    "job_id",
    "trace_id",
    "episode_id",
    "session_key",
    "causation_id",
    "duration_ms",
    "args",
)
ID = string(160)
HOURS = {"type": "integer", "minimum": 1, "maximum": 24 * 30}
ROW = {"type": "object"}


def see_all(core, client):
    if client == "owner":
        return True
    try:
        core.policy.require_scope(client, "audit:read_all")
        return True
    except CapabilityError:
        return False


def compact(record):
    return {key: record.get(key) for key in FIELDS if record.get(key) is not None}


def window(args, default_hours=24):
    until_ms = int(time.time() * 1000)
    return until_ms - args.get("hours", default_hours) * 3_600_000, until_ms


def register(registry):
    @registry.declare(
        "audit_query",
        "Search the activity log",
        "Agent activity log: recent calls, failures and tool history. Use when checking what "
        "agents or routines did: recent calls, failures, denials or one tool's history. "
        "Requires audit:read (own calls) or audit:read_all (everyone). Returns newest-first "
        "compact rows with safe arguments; follow trace_id, approval_id or episode_id with "
        "audit_trace.",
        obj(
            {
                "hours": HOURS,
                "client": string(64),
                "capability": string(64),
                "decision": {"type": "string", "enum": ["allowed", "denied", "approval_required"]},
                "outcome": string(64),
                "origin": {
                    "type": "string",
                    "enum": ["agent", "owner", "routine", "event_rule", "schedule", "system"],
                },
                "problems_only": {"type": "boolean"},
                "arg_key": {"type": "string", "pattern": "^[a-z_]{1,40}$"},
                "arg_value": string(120),
                "trace_id": ID,
                "approval_id": ID,
                "job_id": ID,
                "episode_id": ID,
                "session_key": ID,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
        max_result_chars=500_000,  # Up to 200 rows with 8 KiB args each; Claude's hard ceiling.
        scope="audit:read",
        output_schema=output(rows=array(ROW), see_all={"type": "boolean"}),
    )
    async def query(core, client, args):
        since_ms, until_ms = window(args)
        everyone = see_all(core, client)
        rows, more = core.audit.query(
            viewer=client,
            see_all=everyone,
            filters={**args, "since_ms": since_ms, "until_ms": until_ms},
            limit=args.get("limit", 50),
        )
        return {
            "status": "ok",
            "rows": [compact(r) for r in rows],
            "truncated": more,
            "limit": args.get("limit", 50),
            "see_all": everyone,
        }

    @registry.declare(
        "audit_trace",
        "Trace why something happened",
        "Agent activity trace: why an action happened and what followed. Use when asking why an "
        "action happened or what followed it, e.g. why a service restarted. Requires any ID "
        "from audit_query: call, trace, approval, job, episode or session. Returns the ordered "
        "chain: the calls in that trace and episode, the approval request and answer, and calls "
        "it caused.",
        obj({"ref": ID, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("ref",)),
        max_result_chars=500_000,  # Same bounded rows as query; Claude's hard ceiling.
        scope="audit:read",
        output_schema=output(rows=array(ROW)),
    )
    async def trace(core, client, args):
        limit = args.get("limit", 100)
        rows = core.audit.trace(
            viewer=client, see_all=see_all(core, client), ref=args["ref"], limit=limit + 1
        )
        if not rows:
            raise CapabilityError("not_found", "No visible audit rows match that ID")
        return {
            "status": "ok",
            "rows": [compact(r) for r in rows[:limit]],
            "truncated": len(rows) > limit,
            "limit": limit,
        }

    @registry.declare(
        "audit_digest",
        "Summarize recent agent activity",
        "Agent activity summary: counts, errors, approvals and loops. Use when reporting what "
        "agents did overnight or spotting problems. Requires audit:read. Returns counts per "
        "client and tool against a 7-day baseline, errors and denials by code, approvals, write "
        "actions, repeated-call loops and catalog changes; write the narrative yourself.",
        obj({"hours": HOURS}),
        scope="audit:read",
        output_schema=output(
            since={"type": "string"},
            see_all={"type": "boolean"},
            clients=array(ROW),
            capabilities=array(ROW),
            errors=array(ROW),
            approvals=array(ROW),
            actions=array(ROW),
            loops=array(ROW),
            catalog_changes=array(ROW),
        ),
    )
    async def digest(core, client, args):
        from ..audit import timestamp

        since_ms, until_ms = window(args)
        everyone = see_all(core, client)
        report = core.audit.digest_report(
            viewer=client, see_all=everyone, since_ms=since_ms, until_ms=until_ms
        )
        return {"status": "ok", "since": timestamp(since_ms), "see_all": everyone, **report}
