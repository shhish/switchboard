"""Read-only Apple Notes over the private native-helper socket."""

from .errors import CapabilityError
from .eventkit import EventKit
from .remote import project
from .schema import obj, string

FIELDS = {
    "id": string(1000),
    "title": {"type": "string", "maxLength": 200},
    "folder": {"type": "string", "maxLength": 200},
    "account": {"type": "string", "maxLength": 200},
    "modified": string(100),
}
SUMMARY = obj(
    {**FIELDS, "snippet": {"type": "string", "maxLength": 240}},
    (*FIELDS, "snippet"),
)
SEARCH = obj(
    {
        "results": {"type": "array", "items": SUMMARY, "maxItems": 20},
        "truncated": {"type": "boolean"},
    },
    ("results", "truncated"),
)
NOTE = obj(
    {
        **FIELDS,
        "created": string(100),
        "body": {"type": "string", "maxLength": 20000},
        "truncated": {"type": "boolean"},
    },
    (*FIELDS, "created", "body", "truncated"),
)
METHODS = {"apple_notes.status", "apple_notes.search", "apple_notes.get"}


class AppleNotes(EventKit):
    def __init__(self, config, timeout=60):
        super().__init__(config, timeout, backend="apple_notes")

    async def call(self, method, args=None):
        if method not in METHODS:
            raise CapabilityError("invalid_arguments", "Unknown Apple Notes operation")
        args = args or {}
        data = await self._request(
            method, args, 10 if method == "apple_notes.status" else self.timeout
        )
        if method == "apple_notes.status":
            return data
        try:
            if method == "apple_notes.search" and (
                not isinstance(data.get("results"), list)
                or len(data["results"]) > args.get("limit", 10)
            ):
                raise ValueError
            result = project(data, SEARCH if method == "apple_notes.search" else NOTE)
            rows = result["results"] if method == "apple_notes.search" else [result]
            if any(not row["id"].startswith("x-coredata://") for row in rows):
                raise ValueError
            if method == "apple_notes.get" and result["id"] != args["id"]:
                raise ValueError
            if method == "apple_notes.search" and len(rows) > args.get("limit", 10):
                raise ValueError
        except (CapabilityError, ValueError, KeyError):
            raise CapabilityError(
                "backend_invalid", "Invalid Apple Notes helper response"
            ) from None
        return result

    async def request_access(self):
        """Local authenticated owner CLI only; never a capability."""
        return await self._request("apple_notes.request_access", {}, 180)

    async def probe(self):
        if not self.config.backends["apple_notes"].get("enabled", False):
            return {"status": "disabled", "reason": "disabled in config"}
        try:
            data = await self.call("apple_notes.status")
            permission, running = data.get("automation"), data.get("running")
            if (
                type(running) is not bool
                or not isinstance(permission, str)
                or permission not in {"authorized", "not_determined", "denied", "unavailable"}
            ):
                raise CapabilityError("backend_invalid", "apple_notes: invalid status")
            closed = not running and permission == "unavailable"
            return {
                "status": "ok" if permission == "authorized" or closed else "degraded",
                "reason": "reachable; Apple Notes is closed and opens on demand"
                if closed
                else "reachable; Automation status reported without prompting",
                "running": running,
                "automation": permission,
            }
        except CapabilityError as exc:
            return {"status": "down", "reason": str(exc)}
