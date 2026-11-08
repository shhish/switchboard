"""Native EventKit events with an explicit, limited FrameServer read fallback."""

from datetime import UTC, datetime, timedelta

from ..errors import CapabilityError
from ..memory_subjects import MemorySubject
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

EVENT_FIELDS = ("id", "calendar_id", "title", "start", "end", "all_day", "location")
EVENT = selected((*EVENT_FIELDS, "allDay"))
CALENDAR = selected(("id", "title", "writable", "default"))


def register(registry):
    @registry.declare(
        "calendar_upcoming",
        "Upcoming calendar events",
        "Calendar upcoming events and conflicts. Use when checking upcoming events or a "
        "calendar conflict. Requires calendar read access and a window up to 31 days. Returns "
        "events and coverage; FrameServer fallback has limited dates and no IDs.",
        obj({"days": DAYS, "calendar_ids": IDS}),
        scope="calendar:read",
        service="eventkit+frame",
        output_schema=output(
            events=array(EVENT),
            requested_days={"type": "integer"},
            coverage={"type": "string"},
            event_ids_available={"type": "boolean"},
            truncated={"type": "boolean"},
            **TIME,
        ),
        memory=MemorySubject("calendar", result_paths=("events[].title", "events[].id")),
    )
    async def upcoming(core, client, args):
        days = args.get("days", 2)
        now = datetime.now(UTC)
        end = now + timedelta(days=days)
        request = {"start": now.isoformat(), "end": end.isoformat(), "limit": 200}
        if "calendar_ids" in args:
            request["calendar_ids"] = args["calendar_ids"]
        data = await native_read(core, "events", "events.list", request)
        if data is not None:
            return {
                "status": "ok",
                "events": rows(data, "events", EVENT_FIELDS),
                "requested_days": days,
                "coverage": "EventKit covers the requested window (up to 200 events)",
                "event_ids_available": True,
                "truncated": bool(data.get("truncated", False)),
                **metadata(data),
            }
        require_unfiltered(args, "calendar_ids")
        data = await core.backends.get("frame", "/api/state")
        events = []
        for row in data.get("events", []):
            try:
                start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
                finish = datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
                if finish > now and start < end:
                    events.append(pick(row, ("title", "start", "end", "allDay", "location")))
            except (KeyError, ValueError, TypeError):
                raise CapabilityError("backend_invalid", "frame: invalid event dates") from None
        events.sort(key=lambda item: item["start"])
        return {
            "status": "ok",
            "events": events[:30],
            "requested_days": days,
            "coverage": "FrameServer supplies today and tomorrow only; not a full requested window",
            "event_ids_available": False,
            "truncated": len(events) > 30,
        }

    @registry.declare(
        "calendar_list_calendars",
        "List calendars",
        "Calendar IDs, defaults and writability. Use when choosing a calendar for an event. "
        "Requires authorized EventKit calendar access. Returns stable calendar IDs, defaults "
        "and writability.",
        scope="calendar:read",
        service="eventkit",
        output_schema=output(calendars=array(CALENDAR), truncated={"type": "boolean"}, **TIME),
        memory=MemorySubject("calendar", result_paths=("calendars[].title", "calendars[].id")),
    )
    async def list_calendars(core, client, args):
        data = await core.backends.eventkit.call("calendars.list")
        return {
            "status": "ok",
            "calendars": rows(data, "calendars", ("id", "title", "writable", "default")),
            "truncated": bool(data.get("truncated", False)),
            **metadata(data),
        }

    @registry.declare(
        "calendar_create_event",
        "Create calendar event",
        "Calendar event creation. Use when adding a calendar event. Requires an allowlisted "
        "calendar ID and confirmed dates and timezone. Returns created event metadata.",
        obj(
            {
                "calendar_id": string(1000),
                "title": string(200),
                "start": string(100),
                "end": string(100),
                "all_day": {"type": "boolean"},
                "location": string(1000),
                "notes": string(2000),
                "alarms_minutes": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 525600},
                    "maxItems": 10,
                },
            },
            ("calendar_id", "title", "start", "end"),
        ),
        scope="calendar:write",
        tier="write",
        idempotent=False,
        service="eventkit",
        output_schema=output(event=EVENT, **TIME),
        memory=MemorySubject(
            "calendar", arg_keys=("title", "event_id"), result_paths=("event.title", "event.id")
        ),
    )
    async def create_event(core, client, args):
        core.policy.require_allowed("calendar", "calendars", args["calendar_id"])
        data = await core.backends.eventkit.call("events.create", args)
        return {
            "status": "ok",
            "event": resource(data.get("event"), EVENT_FIELDS),
            **metadata(data),
        }

    @registry.declare(
        "calendar_update_event",
        "Update calendar event",
        "Calendar event edits for one discovered occurrence. Use when editing a discovered "
        "event after confirming the exact change with the human. Requires its current calendar "
        "to allow writes. Changes only the identified occurrence of recurring events "
        "(.thisEvent). Attendees or another organizer require Calendar. Returns updated event "
        "metadata.",
        obj(
            {
                "event_id": string(1000),
                "start": string(100),
                "end": string(100),
                "title": string(200),
                "location": {"type": "string", "maxLength": 1000},
                "notes": {"type": "string", "maxLength": 2000},
                "all_day": {"type": "boolean"},
            },
            ("event_id",),
        ),
        scope="calendar:write",
        tier="write",
        service="eventkit",
        output_schema=output(event=EVENT, **TIME),
        memory=MemorySubject(
            "calendar", arg_keys=("title", "event_id"), result_paths=("event.title", "event.id")
        ),
    )
    async def update_event(core, client, args):
        if len(args) == 1:
            raise CapabilityError("invalid_arguments", "At least one change is required")
        allowed = core.policy.allowed_ids("calendar", "calendars")
        if not allowed:
            raise CapabilityError("resource_denied", "No calendars are allowlisted")
        data = await core.backends.eventkit.call("events.update", {**args, "calendar_ids": allowed})
        return {
            "status": "ok",
            "event": resource(data.get("event"), EVENT_FIELDS),
            **metadata(data),
        }
