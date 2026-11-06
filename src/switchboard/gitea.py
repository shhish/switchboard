"""Private, bounded Gitea transport. Only the Git domain constructs API paths."""

import json
import re
from pathlib import Path

import aiohttp

from .auth import read_private
from .errors import CapabilityError

MAX_BODY = 2 * 1024 * 1024
PAGE_SIZE = 50
MAX_PAGES = 4
SLUG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"


def validate_settings(settings):
    from .config import validate_url

    settings["url"] = validate_url(settings["url"])
    if type(settings.get("enabled", False)) is not bool:
        raise ValueError("gitea.enabled must be boolean")
    owner = settings.get("owner", "")
    if not isinstance(owner, str) or (owner and not re.fullmatch(SLUG, owner)):
        raise ValueError("Invalid Gitea owner")
    token = settings.get("token_file")
    if token is not None:
        if (
            not isinstance(token, str)
            or not token
            or any(ord(c) < 32 or ord(c) == 127 for c in token)
        ):
            raise ValueError("Invalid Gitea token_file")
        path = Path(token).expanduser()
        if not path.is_absolute():
            raise ValueError("Gitea token_file must be absolute")
        settings["token_file"] = str(path)
    if settings.get("enabled") and (not token or not owner):
        raise ValueError("Enabled Gitea requires owner and token_file")
    return settings


class Gitea:
    def __init__(self, backends):
        self.backends = backends

    def settings(self):
        settings = self.backends.settings("gitea")
        try:
            return validate_settings(dict(settings))
        except (ValueError, KeyError, TypeError):
            raise CapabilityError("backend_invalid", "gitea: invalid configuration") from None

    async def request(self, path, *, params=None, payload=None, missing=False, text=False):
        settings = self.settings()
        try:
            token = read_private(Path(settings["token_file"]), max_bytes=4096)
            if not token or any(c.isspace() or ord(c) < 32 for c in token):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise CapabilityError(
                "backend_credentials", "gitea: unsafe or missing token file"
            ) from None
        if self.backends.session is None:
            raise RuntimeError("Backend session not started")
        try:
            async with self.backends.session.request(
                "POST" if payload is not None else "GET",
                settings["url"] + path,
                params=params,
                json=payload,
                headers={
                    "Authorization": "token " + token,
                    "Accept": "text/plain" if text else "application/json",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if missing and response.status == 404:
                    return None
                if response.status == 404:
                    raise CapabilityError("not_found", "Gitea object not found or inaccessible")
                if not 200 <= response.status < 300:
                    raise CapabilityError("backend_error", f"gitea: HTTP {response.status}")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise CapabilityError(
                            "backend_invalid", "gitea: response exceeds 2 MiB limit"
                        )
                if text:
                    return body.decode("utf-8", "replace")
                return json.loads(body) if body else {}
        except CapabilityError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise CapabilityError("backend_down", "gitea: unreachable or timed out") from None
        except (ValueError, UnicodeError, RecursionError):
            raise CapabilityError("backend_invalid", "gitea: invalid JSON response") from None

    async def pages(self, path, *, params=None, key=None, cap=200):
        """At most four 50-row pages; expose incomplete views rather than guessing."""
        rows = []
        limit = min(PAGE_SIZE, cap)
        for page in range(1, MAX_PAGES + 1):
            data = await self.request(path, params={**(params or {}), "page": page, "limit": limit})
            items = data.get(key) if key and isinstance(data, dict) else None if key else data
            if not isinstance(items, list) or not all(isinstance(row, dict) for row in items):
                raise CapabilityError("backend_invalid", "gitea: invalid list response")
            remaining = cap - len(rows)
            rows.extend(items[:remaining])
            if len(items) > remaining:
                return rows, True
            if len(items) > limit:
                return rows, True
            if len(items) < limit:
                return rows, False
            if len(rows) >= cap:
                return rows, True
        return rows, True

    async def probe(self):
        if not self.backends.config.backends["gitea"].get("enabled", False):
            return {"status": "disabled", "reason": "disabled in config"}
        try:
            # Authenticated owner search proves private-token readability and API reachability.
            data = await self.request(
                "/repos/search", params={"limit": 1, "page": 1, "private": "true"}
            )
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise CapabilityError("backend_invalid", "gitea: invalid search response")
            return {"status": "ok", "reason": "token file safe; API reachable"}
        except CapabilityError as exc:
            return {"status": "down", "reason": str(exc)}
