"""Bounded NDJSON transport for the separately authorized native helper."""

import asyncio
import contextlib
import json
import os
import stat
from pathlib import Path

from .errors import CapabilityError

MAX_RESPONSE = 2 * 1024 * 1024
METHODS = {
    "status",
    "net.probe",
    "remote.ssh",
    "calendars.list",
    "events.list",
    "events.create",
    "reminder_lists.list",
    "reminders.list",
    "reminders.add",
    "reminders.complete",
}
ERRORS = {
    "timeout": "Native operation timed out; inspect reads before retrying a write",
    "unauthorized": "EventKit access unavailable; owner must run sb eventkit request-access",
    "invalid_arguments": "EventKit rejected the request arguments",
    "not_found": "EventKit resource not found",
    "resource_denied": "EventKit resource is not writable or not allowlisted",
    "backend_error": "EventKit operation failed; inspect status before retrying a write",
}


class EventKit:
    def __init__(self, config, timeout=10, *, backend="eventkit"):
        self.config = config
        self.backend = backend
        self.timeout = timeout

    def _socket_path(self):
        path = Path(self.config.backends["eventkit"]["socket"]).expanduser()
        info = path.lstat()
        parent = path.parent.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.getuid()
        ):
            raise CapabilityError(
                "backend_invalid", f"{self.backend}: unsafe socket ownership or mode"
            )
        return path

    async def _request(self, method, args, deadline_s):
        settings = self.config.backends[self.backend]
        if not settings.get("enabled", False):
            raise CapabilityError("backend_disabled", f"{self.backend} is disabled")
        writer = None
        try:
            async with asyncio.timeout(deadline_s):
                path = await asyncio.to_thread(self._socket_path)
                reader, writer = await asyncio.open_unix_connection(path, limit=MAX_RESPONSE)
                writer.write(
                    json.dumps({"method": method, "args": args}, allow_nan=False).encode() + b"\n"
                )
                await writer.drain()
                from .server import load_json

                line = await reader.readline()
                if not line.endswith(b"\n"):
                    raise ValueError
                data = load_json(line)
                if not isinstance(data, dict) or type(data.get("ok")) is not bool:
                    raise ValueError
                if data["ok"]:
                    if set(data) != {"ok", "result"} or not isinstance(data["result"], dict):
                        raise ValueError
                    return data["result"]
                error = data.get("error")
                if not isinstance(error, dict):
                    raise ValueError
                code = error.get("code")
                if not isinstance(code, str) or code not in (ERRORS.keys() | {"unsupported"}):
                    raise ValueError
                message = ERRORS.get(code, "Native helper operation unsupported")
                if self.backend == "things":
                    message = {
                        "unauthorized": (
                            "Things Automation unavailable; run sb things request-access as owner"
                        ),
                        "unsupported": ("Native checklist/Evening unsupported; nothing created"),
                    }.get(code, "Things operation failed; inspect reads before retrying writes")
                raise CapabilityError(self.backend + "_" + code, message)
        except CapabilityError:
            raise
        except (OSError, TimeoutError):
            raise CapabilityError(
                "backend_down",
                f"{self.backend}: missing or timed out; "
                f"run sb {self.backend} status before retrying writes",
            ) from None
        except (ValueError, UnicodeError, RecursionError):
            raise CapabilityError("backend_invalid", f"{self.backend}: invalid response") from None
        finally:
            if writer:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    async with asyncio.timeout(1):
                        await writer.wait_closed()

    async def call(self, method, args=None):
        if method not in METHODS:
            raise CapabilityError("invalid_arguments", "Unknown EventKit operation")
        return await self._request(method, args or {}, self.timeout)

    async def net_request_access(self):
        from .probes import is_lan

        probe = next((p for p in self.config.probes if is_lan(p["url"])), None)
        if probe is None:
            raise CapabilityError("invalid_arguments", "Configure an RFC1918 probe first")
        return await self._request("net.request_access", {"url": probe["url"]}, 5)

    async def request_access(self):
        """Only the local owner CLI calls this; never exposed as a capability."""
        return await self._request("request_access", {}, 180)

    async def authorized(self, entity):
        try:
            status = await self.call("status")
            return status.get(entity) == "full_access"
        except CapabilityError as exc:
            if exc.code in {"backend_disabled", "backend_down"}:
                return False
            raise

    async def probe(self):
        try:
            data = await self.call("status")
            states = {key: data.get(key) for key in ("events", "reminders")}
            valid = {
                "not_determined",
                "restricted",
                "denied",
                "write_only",
                "full_access",
                "unknown",
            }
            if any(not isinstance(value, str) or value not in valid for value in states.values()):
                raise CapabilityError("backend_invalid", "eventkit: invalid authorization status")
            return {
                "status": "ok" if all(v == "full_access" for v in states.values()) else "degraded",
                "reason": "reachable; TCC status reported without prompting",
                "authorization": states,
            }
        except CapabilityError as exc:
            return {
                "status": "degraded" if exc.code == "backend_disabled" else "down",
                "reason": str(exc),
            }
