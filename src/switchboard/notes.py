"""Read-only vault access through the privacy-authorized native helper only."""

import unicodedata
from pathlib import Path

from .errors import CapabilityError
from .eventkit import EventKit
from .remote import project
from .schema import obj, string

PATH = string(4096)
SUMMARY = obj(
    {
        "path": PATH,
        "title": string(200),
        "snippet": {"type": "string", "maxLength": 240},
        "modified": string(100),
    },
    ("path", "title", "snippet", "modified"),
)
SEARCH = obj(
    {
        "results": {"type": "array", "items": SUMMARY, "maxItems": 20},
        "truncated": {"type": "boolean"},
        "searched": {"type": "integer", "minimum": 0, "maximum": 5000},
    },
    ("results", "truncated", "searched"),
)
NOTE = obj(
    {
        "path": PATH,
        "title": string(200),
        "content": {"type": "string", "maxLength": 20000},
        "modified": string(100),
        "truncated": {"type": "boolean"},
    },
    ("path", "title", "content", "modified", "truncated"),
)


def relative_path(value, *, folder=False):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    if folder:
        value = value.removesuffix("/")
    return (
        not value.startswith("/")
        and "\x00" not in value
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and (folder or value.endswith(".md"))
    )


def validate_settings(settings):
    """Lexical validation only: Python must never stat or resolve the vault."""
    root = settings.get("root")
    if root is not None or settings.get("enabled", False):
        home = str(Path.home()).rstrip("/") + "/"
        if (
            not isinstance(root, str)
            or not root.startswith(home)
            or root.rstrip("/") == home.rstrip("/")
            or len(root) > 4096
            or "\x00" in root
            or "\\" in root
            or any(p in {".", ".."} for p in root.split("/"))
        ):
            raise ValueError(
                "Notes root must be an absolute directory inside home without traversal"
            )
    exclude = settings.get("exclude")
    if (
        not isinstance(exclude, list)
        or len(exclude) > 100
        or any(not relative_path(p, folder=True) for p in exclude)
    ):
        raise ValueError("Notes exclude must contain relative folder prefixes")


def excluded(path, prefixes):
    path = unicodedata.normalize("NFD", path).casefold()
    return any(
        path.startswith(unicodedata.normalize("NFD", prefix.rstrip("/")).casefold() + "/")
        for prefix in prefixes
    )


class Notes(EventKit):
    def __init__(self, config, timeout=20):
        super().__init__(config, timeout, backend="notes")

    async def call(self, method, args=None):
        if method not in {"notes.search", "notes.get"}:
            raise CapabilityError("invalid_arguments", "Unknown Notes operation")
        settings = self.config.backends["notes"]
        if not settings.get("enabled", False):
            raise CapabilityError("backend_disabled", "notes is disabled")
        try:
            validate_settings(settings)
        except ValueError:
            raise CapabilityError("backend_invalid", "Invalid Notes configuration") from None
        args = dict(args or {})
        if method == "notes.get":
            path = args.get("path")
            if not relative_path(path):
                raise CapabilityError("invalid_arguments", "Use a relative Markdown note path")
            if excluded(path, settings["exclude"]):
                raise CapabilityError("resource_denied", "Note path is excluded")
        data = await self._request(
            method, {**args, "root": settings["root"], "exclude": settings["exclude"]}, self.timeout
        )
        try:
            result = project(data, SEARCH if method == "notes.search" else NOTE)
        except CapabilityError:
            raise CapabilityError("backend_invalid", "Invalid Notes helper response") from None
        if method == "notes.search":
            result["results"] = [
                row
                for row in result["results"]
                if relative_path(row["path"]) and not excluded(row["path"], settings["exclude"])
            ][: args.get("limit", 10)]
        elif result["path"] != path or excluded(result["path"], settings["exclude"]):
            raise CapabilityError("backend_invalid", "Invalid Notes helper path")
        return result

    async def request_access(self):
        """Local authenticated owner only. Discard all note data at the CLI boundary."""
        await self.call("notes.search", {"query": "switchboard-folder-access", "limit": 1})
        return {}

    async def probe(self):
        if not self.config.backends["notes"].get("enabled", False):
            return {"status": "disabled", "reason": "disabled in config"}
        try:
            validate_settings(self.config.backends["notes"])
            # No root is sent: reachability cannot establish folder permission.
            await self._request("status", {}, self.timeout)
            return {"status": "ok", "reason": "helper reachable; vault access not checked"}
        except (CapabilityError, ValueError):
            return {"status": "unavailable", "reason": "Notes helper or configuration unavailable"}
