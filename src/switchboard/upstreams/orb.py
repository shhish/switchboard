"""Federate Orb's existing contracts; the hub owns cards, events and webhooks."""

import asyncio
import copy
import hashlib
import time
from pathlib import Path

import aiohttp

from ..auth import local_url, read_token
from ..errors import CapabilityError

READ_TOOLS = frozenset({"orb_status", "orb_get_card", "orb_get_transcript", "orb_wait"})
WRITE_TOOLS = frozenset({"orb_show", "orb_ask", "orb_cancel"})
TOOLS = READ_TOOLS | WRITE_TOOLS
EVENT_NAMES = frozenset({"card.answered", "voice.transcribed"})
EVENT_METHODS = frozenset({"events/list", "events/subscribe", "events/unsubscribe"})
METHODS = EVENT_METHODS | {"tools/list", "tools/call", "server/discover"}
MAX_RESPONSE = 2 * 1024 * 1024
CACHE_SECONDS = 5


class OrbUpstream:
    def __init__(self, config, policy):
        self.config = config
        self.policy = policy
        self.session = None
        self.cache = {}
        self.locks = {}

    async def start(self):
        self.session = aiohttp.ClientSession(trust_env=False)

    async def close(self):
        if self.session:
            await self.session.close()

    def credentials(self, client):
        self.policy.require_scope(client, "orb:use")
        settings = self.config.upstreams["orb"]
        paths = settings["client_tokens"]
        if client not in paths:
            raise CapabilityError("upstream_unmapped", "No Orb identity mapped for this client")
        try:
            token = read_token(Path(paths[client]).expanduser())
            # Reject accidental shared credentials rather than merge two clients' ownership.
            for other, path in paths.items():
                if other == client:
                    continue
                try:
                    duplicate = read_token(Path(path).expanduser()) == token
                except (OSError, ValueError, UnicodeError):
                    continue
                if duplicate:
                    raise ValueError
            url = local_url(settings["url"])
        except (OSError, ValueError, UnicodeError, TypeError):
            self.cache.pop(client, None)
            raise CapabilityError(
                "upstream_credentials", "Orb mapping invalid; check private, distinct token files"
            ) from None
        return url, token

    async def request(self, client, method, params, *, version="2026-07-28", meta=None):
        from ..protocol import CAPABILITIES_KEY, MODERN_VERSION, VERSION_KEY, request_headers
        from ..server import load_json

        if method not in METHODS:
            raise CapabilityError("unknown_capability", "Unknown Orb method")
        url, token = self.credentials(client)
        if meta is None:
            meta = {VERSION_KEY: MODERN_VERSION, CAPABILITIES_KEY: {}}
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {**params, "_meta": meta},
        }
        headers = {
            **request_headers(message, version),
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        }
        if self.session is None:
            raise CapabilityError("upstream_down", "Orb unavailable; run sb doctor")
        try:
            async with self.session.post(
                url,
                json=message,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(
                    total=55
                    if method == "tools/call"
                    else 15
                    if method == "events/subscribe"
                    else 3
                ),
            ) as response:
                if response.status >= 300 and response.status not in (400, 404):
                    raise ValueError
                if response.content_type != "application/json":
                    raise ValueError
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise ValueError
                envelope = load_json(body)
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("jsonrpc") != "2.0"
                    or type(envelope.get("id")) is not int
                    or envelope["id"] != 1
                    or ("result" in envelope) == ("error" in envelope)
                ):
                    raise ValueError
                key = "error" if "error" in envelope else "result"
                value = envelope[key]
                if not isinstance(value, dict):
                    raise ValueError
                if key == "error" and (
                    type(value.get("code")) is not int or not isinstance(value.get("message"), str)
                ):
                    raise ValueError
                if key == "result" and response.status >= 300:
                    raise ValueError
                return {key: value}
        except (OSError, ValueError, TypeError, RecursionError, aiohttp.ClientError, TimeoutError):
            self.cache.pop(client, None)
            raise CapabilityError(
                "upstream_down", "Orb unavailable or response invalid; run sb doctor"
            ) from None

    async def tools(self, client):
        # Reload policy and credentials even for cache hits (revocation is immediate).
        url, token = self.credentials(client)
        identity = (url, hashlib.sha256(token.encode()).digest())
        lock = self.locks.setdefault(client, asyncio.Lock())
        async with lock:
            cached = self.cache.get(client)
            if cached and cached[0] == identity and cached[1] > time.monotonic():
                return self.visible_tools(cached[2])
            envelope = await self.request(client, "tools/list", {})
            result = envelope.get("result", {})
            tools = result.get("tools")
            if not isinstance(tools, list) or result.get("nextCursor") is not None:
                raise CapabilityError("upstream_invalid", "Orb tool catalog invalid; run sb doctor")
            # Never pick up future administrative, finance or other hub tools automatically.
            filtered = []
            seen = set()
            for tool in tools:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise CapabilityError("upstream_invalid", "Orb tool catalog invalid")
                name = tool["name"]
                if name not in TOOLS:
                    continue
                if name in seen or not isinstance(tool.get("inputSchema"), dict):
                    raise CapabilityError("upstream_invalid", "Orb tool catalog invalid")
                seen.add(name)
                filtered.append(tool)
            ttl = result.get("ttlMs", CACHE_SECONDS * 1000)
            ttl = min(CACHE_SECONDS * 1000, max(0, ttl)) / 1000 if type(ttl) is int else 0
            self.cache[client] = (identity, time.monotonic() + ttl, filtered)
            return self.visible_tools(filtered)

    def visible_tools(self, tools):
        result = []
        for tool in tools:
            if tool["name"] in WRITE_TOOLS:
                try:
                    self.policy.require_allowed("orb", "tools", tool["name"])
                except CapabilityError:
                    continue
            result.append(copy.deepcopy(tool))
        return result

    async def events_capability(self, client):
        try:
            response = await self.request(client, "server/discover", {})
            capabilities = response.get("result", {}).get("capabilities")
            events = capabilities.get("events") if isinstance(capabilities, dict) else None
            return copy.deepcopy(events) if isinstance(events, dict) else None
        except CapabilityError:
            return None

    async def status(self):
        clients = self.config.upstreams["orb"]["client_tokens"]
        if not clients:
            return {"status": "degraded", "reason": "not configured; Orb tools omitted"}

        async def probe(client):
            try:
                self.cache.pop(client, None)
                await self.tools(client)
                if await self.events_capability(client) is None:
                    raise CapabilityError("upstream_invalid", "Orb events not advertised")
                return {"status": "ok", "reason": "reachable with mapped identity"}
            except CapabilityError as exc:
                return {"status": "down", "reason": str(exc)}

        results = await asyncio.gather(*(probe(client) for client in clients))
        healthy = all(r["status"] == "ok" for r in results)
        return {
            "status": "ok" if healthy else "degraded",
            "reason": "reachable with mapped identities"
            if healthy
            else "Orb tools omitted or events unavailable for affected mappings",
            "clients": dict(zip(clients, results, strict=True)),
        }
