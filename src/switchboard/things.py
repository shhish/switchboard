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
