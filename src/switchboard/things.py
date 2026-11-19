"""Things operations over the existing private native-helper socket."""

from .errors import CapabilityError
from .eventkit import EventKit

METHODS = {
    "things.status",
    "things.lists",
    "things.search",
    "things.projects",
    "things.areas",
    "things.add",
    "things.add_project",
    "things.complete",
    "things.update",
}


class Things(EventKit):
    def __init__(self, config, timeout=10):
        super().__init__(config, timeout, backend="things")

    async def call(self, method, args=None):
        if method not in METHODS:
            raise CapabilityError("invalid_arguments", "Unknown Things operation")
        return await self._request(method, args or {}, self.timeout)

    async def request_access(self):
        """Local authenticated owner CLI only; never a capability."""
        return await self._request("things.request_access", {}, 180)

    async def project_todos(self, name):
        """Resolve one project and reuse existing bounded native reads, never scripts.

        The helper has no project-filtered query. Its four open views cover active,
        scheduled and Someday captures; deduplicate IDs shared by those views.
        """
        from .domains.tasks import rows

        data = await self.call("things.projects")
        projects = rows(data, "projects", 200, sanitize=False)
        if data.get("truncated", False):
            raise CapabilityError("backend_invalid", "Things project discovery truncated")
        matches = [p for p in projects if p["name"] == name and p["status"] == "open"]
        if not matches:
            raise CapabilityError("things_project_missing", "Things capture project missing")
        if len(matches) != 1:
            raise CapabilityError("things_project_ambiguous", "Things capture project ambiguous")
        project = matches[0]
        todos, seen, truncated = [], set(), False
        for view in ("Anytime", "Today", "Upcoming", "Someday"):
            data = await self.call("things.lists", {"list": view, "limit": 200})
            truncated |= bool(data.get("truncated", False))
            # Validate raw source fields so forwarding preserves note line breaks.
            # The board sanitizes all returned prose; these strings are never instructions.
            for todo in rows(data, "todos", 200, sanitize=False):
                if (
                    todo["project_id"] == project["id"]
                    and todo["status"] == "open"
                    and todo["id"] not in seen
                ):
                    todos.append(todo)
                    seen.add(todo["id"])
        return {"project": project, "todos": todos, "truncated": truncated}

    async def probe(self):
        try:
            data = await self.call("things.status")
            permission = data.get("automation")
            running = data.get("running")
            if (
                type(running) is not bool
                or not isinstance(permission, str)
                or permission
                not in {
                    "authorized",
                    "not_determined",
                    "denied",
                    "unavailable",
                }
            ):
                raise CapabilityError("backend_invalid", "things: invalid status")
            # A closed Things can't report its grant; the next read launches it.
            closed = not running and permission == "unavailable"
            return {
                "status": "ok"
                if (running and permission == "authorized") or closed
                else "degraded",
                "reason": "reachable; Things is closed and opens on demand"
                if closed
                else "reachable; Automation status reported without prompting",
                "running": running,
                "automation": permission,
            }
        except CapabilityError as exc:
            return {
                "status": "degraded" if exc.code == "backend_disabled" else "down",
                "reason": str(exc),
            }
