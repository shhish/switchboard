"""Fixed backend clients. No caller-controlled URL, method, header or credential."""

import asyncio
import json
from pathlib import Path

import aiohttp

from .auth import read_private
from .errors import CapabilityError
from .eventkit import EventKit
from .execution import ROUTINE_CAUSE
from .media_compress import MediaCompress
from .notes import Notes
from .nws import NWS
from .things import Things

MAX_BODY = 8 * 1024 * 1024


class Backends:
    def __init__(self, config):
        self.config = config
        self.session = None
        self.eventkit = EventKit(config)
        self.media_compress = MediaCompress(config)
        self.things = Things(config)
        self.notes = Notes(config)
        self.weather = NWS(config)

    async def start(self):
        await self.weather.start()
        self.session = aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        )

    async def close(self):
        await self.weather.close()
        if self.session:
            await self.session.close()

    def settings(self, name):
        settings = self.config.backends[name]
        if not settings.get("enabled", True):
            raise CapabilityError("backend_disabled", f"{name} is disabled")
        return settings

    def headers(self, name, *, control=False):
        settings = self.settings(name)
        headers = {}
        try:
            token_key = "control_token_file" if name == "home" and control else "token_file"
            if name in ("home", "plex") and not settings.get(token_key):
                raise ValueError
            if settings.get(token_key):
                token = read_private(Path(settings[token_key]).expanduser())
                if not token:
                    raise ValueError
                if name == "plex":
                    headers.update({"X-Plex-Token": token, "Accept": "application/json"})
                else:
                    headers["Authorization"] = "Bearer " + token
            if settings.get("api_key_file"):
                value = read_private(Path(settings["api_key_file"]).expanduser())
                if settings.get("api_key_json_path"):
                    value = json.loads(value)
                    for key in settings["api_key_json_path"].split("."):
                        value = value[key]
                if not isinstance(value, str) or not value:
                    raise ValueError
                headers["X-Api-Key"] = value
            if name in ("overseerr", "tautulli", "lazylibrarian") and not settings.get(
                "api_key_file"
            ):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise CapabilityError(
                "backend_credentials", f"{name}: secret file missing or invalid"
            ) from None
        return headers

    async def request(
        self, name, path="", *, params=None, payload=None, extra_headers=None, method=None
    ):
        # This method is internal only. The capability registry never exposes it.
        settings = self.settings(name)
        method = method or ("POST" if payload is not None else "GET")
        headers = {**self.headers(name, control=method != "GET"), **(extra_headers or {})}
        if name == "home" and (cause := ROUTINE_CAUSE.get()):
            headers["X-HomeRelay-Cause"] = cause
        if name in ("tautulli", "lazylibrarian"):
            # Supported by older Tautulli too; never log URLs or exception text.
            params = {**(params or {}), "apikey": headers.pop("X-Api-Key")}
        if self.session is None:
            raise RuntimeError("Backend session not started")
        url = settings["url"] + path
        try:
            async with self.session.request(
                method,
                url,
                params=params,
                json=payload,
                headers=headers,
                allow_redirects=False,
                # LL findBook can take 10-40 seconds; all other backends keep their default.
                **({"timeout": aiohttp.ClientTimeout(total=60)} if name == "lazylibrarian" else {}),
            ) as response:
                if response.status >= 300:
                    code = "backend_ambiguous" if response.status == 409 else "backend_error"
                    raise CapabilityError(code, f"{name}: HTTP {response.status}; run sb doctor")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise CapabilityError("backend_invalid", f"{name}: response too large")
                if name == "plex" and method == "PUT" and path in ("/:/scrobble", "/:/unscrobble"):
                    return {}  # PMS returns an empty/HTML receipt; handlers verify metadata.
                if name == "lazylibrarian":
                    # LL returns plain-text receipts ("OK", "Invalid id: ...") unquoted and
                    # JSON for everything else; callers validate either shape strictly.
                    try:
                        return json.loads(body)
                    except ValueError:
                        if len(body) > 4096:
                            raise
                        return body.decode("utf-8").strip()
                return json.loads(body)
        except CapabilityError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise CapabilityError(
                "backend_down", f"{name}: unreachable or timed out; run sb doctor"
            ) from None
        except (ValueError, UnicodeError):
            raise CapabilityError("backend_invalid", f"{name}: invalid JSON response") from None

    async def get(self, name, path="", **kwargs):
        return await self.request(name, path, **kwargs)

    async def probe(self, name):
        if name == "media_compress":
            return self.media_compress.probe()
        if name == "eventkit":
            return await self.eventkit.probe()
        if name == "things":
            return await self.things.probe()
        if name == "notes":
            return await self.notes.probe()
        settings = self.config.backends[name]
        if not settings.get("enabled", True):
            return {"status": "disabled", "reason": "disabled in config"}
        if name == "maintenance":
            from .maintenance import read_status

            try:
                data = await asyncio.to_thread(read_status, settings["path"])
                return {
                    "status": data["status"],
                    "reason": "snapshot stale" if data["stale"] else "private snapshot readable",
                }
            except CapabilityError as exc:
                return {"status": "down", "reason": str(exc)}
        paths = {
            "media": "/health",
            "recipes": "/api/health",
            # Authenticated endpoints, so a stale key after a migration shows as down.
            "overseerr": "/api/v1/auth/me",
            "home": "/health",
            "frame": "/api/state",
            "ps5upload": "/api/version",
            "plex": "/library/sections",
            "tautulli": "/api/v2",
            # Authenticated getVersion, so a wrong key shows as down.
            "lazylibrarian": "/api",
        }
        try:
            if name == "orb":
                from .protocol import CAPABILITIES_KEY, MODERN_VERSION, VERSION_KEY, request_headers

                message = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "ping",
                    "params": {"_meta": {VERSION_KEY: MODERN_VERSION, CAPABILITIES_KEY: {}}},
                }
                data = await self.request(
                    name, payload=message, extra_headers=request_headers(message)
                )
            else:
                params = {
                    "tautulli": {"cmd": "get_activity"},
                    "lazylibrarian": {"cmd": "getVersion"},
                }
                params = params.get(name)
                data = await self.get(name, paths[name], params=params)
            if not isinstance(data, dict):
                raise CapabilityError("backend_invalid", f"{name}: invalid health response")
            if name == "frame" and not all(
                isinstance(data.get(key), list) for key in ("events", "todos")
            ):
                raise CapabilityError("backend_invalid", "frame: missing events/todos")
            if name == "plex" and not isinstance(data.get("MediaContainer"), dict):
                raise CapabilityError("backend_invalid", "plex: invalid health response")
            if name == "tautulli":
                response = data.get("response")
                if not isinstance(response, dict) or response.get("result") != "success":
                    raise CapabilityError("backend_invalid", "tautulli: activity rejected")
            if name == "lazylibrarian" and data.get("Success") is not True:
                raise CapabilityError("backend_invalid", "lazylibrarian: version rejected")
            if name == "home":
                homes = await self.get(name, "/homes")
                if not isinstance(homes, list):
                    raise CapabilityError("backend_invalid", "home: invalid homes response")
            if name == "home" and data.get("homekit") != "ready":
                return {"status": "degraded", "reason": "HomeKit not ready; check Home permission"}
            if isinstance(data, dict) and data.get("status") in ("degraded", "down", "error"):
                return {"status": "degraded", "reason": "backend reports unhealthy"}
            if name == "orb" and "error" in data:
                return {"status": "degraded", "reason": "MCP ping rejected"}
            return {"status": "ok", "reason": "reachable"}
        except CapabilityError as exc:
            return {"status": "down", "reason": str(exc)}

    async def status(self):
        # The retiring aggregator is never contacted. Orb federation owns health
        # when client identities are mapped; its unused legacy backend is omitted.
        names = sorted(
            name
            for name in self.config.backends
            if name != "health"
            and not (name == "orb" and self.config.upstreams["orb"]["client_tokens"])
        )
        results = await asyncio.gather(*(self.probe(name) for name in names))
        return dict(zip(names, results, strict=True))


class OrbApprovalNotifier:
    """Only post a card. Orb callbacks/outbox and resolution integration are Phase 4."""

    def __init__(self, backends):
        self.backends = backends

    async def notify(self, approval):
        from .protocol import CAPABILITIES_KEY, MODERN_VERSION, VERSION_KEY, request_headers

        message = {
            "jsonrpc": "2.0",
            "id": approval["approval_id"],
            "method": "tools/call",
            "params": {
                "name": "orb_ask",
                "arguments": {
                    "card_id": approval["approval_id"],
                    "text": approval["summary"][:280],
                    "options": [
                        {"id": "approve", "label": "Approve"},
                        {"id": "deny", "label": "Deny"},
                    ],
                },
                "_meta": {VERSION_KEY: MODERN_VERSION, CAPABILITIES_KEY: {}},
            },
        }
        result = await self.backends.request(
            "orb", payload=message, extra_headers=request_headers(message)
        )
        if "error" in result or result.get("result", {}).get("isError"):
            raise CapabilityError("notification_failed", "Orb approval card was rejected")
