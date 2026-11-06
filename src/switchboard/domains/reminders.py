"""Native Reminders with list-gated writes and limited FrameServer read fallback."""

from datetime import UTC, datetime, timedelta

from ..errors import CapabilityError
from ..schema import obj, string
from .common import array, output, pick, selected
from .eventkit_common import (
    DAYS,
    IDS,
    TIME,
    metadata,
    native_read,
    require_unfiltered,
    resource,
    rows,
)

REMINDER_FIELDS = ("id", "list_id", "title", "due", "completed", "priority")
REMINDER = selected((*REMINDER_FIELDS, "source"))
LIST = selected(("id", "title", "writable", "default"))


def register(registry):
    @registry.declare(
        "reminders_due",
        "Due reminders",
        "Use when checking overdue and upcoming reminders. Requires reminder read access and "
        "a window up to 31 days. Returns reminders and coverage; FrameServer fallback lacks "
        "due dates and IDs.",
        obj({"days": DAYS, "list_ids": IDS}),
        scope="reminders:read",
        service="eventkit+frame",
        output_schema=output(
            reminders=array(REMINDER),
            requested_days={"type": "integer"},
            coverage={"type": "string"},
            due_dates_available={"type": "boolean"},
            truncated={"type": "boolean"},
            **TIME,
        ),
    )
    async def due(core, client, args):
        days = args.get("days", 1)
        request = {
            "due_before": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
            "limit": 200,
        }
        if "list_ids" in args:
            request["list_ids"] = args["list_ids"]
        data = await native_read(core, "reminders", "reminders.list", request)
        if data is not None:
            return {
                "status": "ok",
                "reminders": rows(data, "reminders", REMINDER_FIELDS),
                "requested_days": days,
                "coverage": "EventKit covers overdue and requested due dates (up to 200 reminders)",
                "due_dates_available": True,
                "truncated": bool(data.get("truncated", False)),
                **metadata(data),
            }
        require_unfiltered(args, "list_ids")
        data = await core.backends.get("frame", "/api/state")
        frame_rows = [row for row in data.get("todos", []) if row.get("source") == "reminders"]
        return {
            "status": "ok",
            "reminders": [pick(row, ("id", "title", "source")) for row in frame_rows[:30]],
            "requested_days": args.get("days", 1),
            "coverage": "FrameServer supplies today and overdue only; no future reminders",
            "due_dates_available": False,
            "truncated": len(frame_rows) > 30,
        }

    @registry.declare(
        "reminders_list_lists",
        "List reminder lists",
        "Use when choosing a reminder list. Requires authorized EventKit reminder access. "
        "Returns stable list IDs and defaults.",
        scope="reminders:read",
        service="eventkit",
        output_schema=output(lists=array(LIST), truncated={"type": "boolean"}, **TIME),
    )
    async def list_lists(core, client, args):
        data = await core.backends.eventkit.call("reminder_lists.list")
        return {
            "status": "ok",
            "lists": rows(data, "lists", ("id", "title", "writable", "default")),
            "truncated": bool(data.get("truncated", False)),
            **metadata(data),
        }

    @registry.declare(
        "reminders_add",
        "Add reminder",
        "Use when adding a reminder. Requires an allowlisted list ID and confirmed reminder "
        "details. Returns created reminder metadata.",
        obj(
            {
                "list_id": string(1000),
                "title": string(200),
                "due": string(100),
                "notes": string(2000),
                "priority": {"type": "integer", "minimum": 0, "maximum": 9},
            },
            ("list_id", "title"),
        ),
        scope="reminders:write",
        tier="write",
        idempotent=False,
        service="eventkit",
        output_schema=output(reminder=REMINDER, **TIME),
    )
    async def add(core, client, args):
        core.policy.require_allowed("reminders", "lists", args["list_id"])
        data = await core.backends.eventkit.call("reminders.add", args)
        return {
            "status": "ok",
            "reminder": resource(data.get("reminder"), REMINDER_FIELDS),
            **metadata(data),
        }

    @registry.declare(
        "reminders_complete",
        "Complete reminder",
        "Use when completing a reminder. Requires a discovered ID in an allowlisted list. "
        "Returns updated reminder metadata.",
        obj({"reminder_id": string(1000)}, ("reminder_id",)),
        scope="reminders:write",
        tier="write",
        service="eventkit",
        output_schema=output(reminder=REMINDER, **TIME),
    )
    async def complete(core, client, args):
        allowed = core.policy.allowed_ids("reminders", "lists")
        if not allowed:
            raise CapabilityError("resource_denied", "No reminder lists are allowlisted")
        # The helper checks the item's current list immediately before saving, avoiding
        # a capped-list lookup and a check/use race across separate socket requests.
        data = await core.backends.eventkit.call(
            "reminders.complete", {**args, "list_ids": allowed}
        )
        return {
            "status": "ok",
            "reminder": resource(data.get("reminder"), REMINDER_FIELDS),
            **metadata(data),
        }

    @registry.declare(
        "reminders_update",
        "Update reminder",
        "Use when editing a discovered reminder after confirming the exact change with the human. "
        "Requires its current list to allow writes. Returns updated reminder metadata; "
        "due accepts a date or datetime, or null to clear.",
        obj(
            {
                "reminder_id": string(1000),
                "title": string(200),
                "due": {"type": ["string", "null"], "minLength": 1, "maxLength": 100},
                "notes": {"type": "string", "maxLength": 2000},
                "priority": {"type": "integer", "minimum": 0, "maximum": 9},
            },
            ("reminder_id",),
        ),
        scope="reminders:write",
        tier="write",
        service="eventkit",
        output_schema=output(reminder=REMINDER, **TIME),
    )
    async def update(core, client, args):
        if len(args) == 1:
            raise CapabilityError("invalid_arguments", "At least one change is required")
        allowed = core.policy.allowed_ids("reminders", "lists")
        if not allowed:
            raise CapabilityError("resource_denied", "No reminder lists are allowlisted")
        data = await core.backends.eventkit.call("reminders.update", {**args, "list_ids": allowed})
        return {
            "status": "ok",
            "reminder": resource(data.get("reminder"), REMINDER_FIELDS),
            **metadata(data),
        }
