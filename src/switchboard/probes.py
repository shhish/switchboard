"""Credential-free GET probes of configured local services; no response bodies escape."""

import asyncio
import ipaddress
import math
from copy import deepcopy
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urlsplit

import aiohttp

from .errors import CapabilityError
from .eventkit import EventKit


def is_lan(url):
    return not ipaddress.ip_address(urlsplit(url).hostname).is_loopback


PROBE_TIMEOUT_S = 3
CACHE_TTL_S = 15


class HealthProbes:
    def __init__(self, config):
        self.config = config
        self.session = None
        self.task = None
        self.cached = None
        self.cached_at = 0
        self.lan_limit = asyncio.Semaphore(8)

    async def start(self):
        self.session = aiohttp.ClientSession(
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S),
        )

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.session is not None:
            await self.session.close()

    async def health(self):
        if self.cached is not None and monotonic() - self.cached_at < CACHE_TTL_S:
            return deepcopy(self.cached)
        return deepcopy(await self.run())

    async def run(self):
        # Monitoring bypasses the TTL. Concurrent callers may share only an active
        # fresh run, never a cached result; cancellation of a reader doesn't stop it.
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())
        return await asyncio.shield(self.task)

    async def _run(self):
        services = await asyncio.gather(*(self.probe(probe) for probe in self.config.probes))
        services.sort(key=lambda row: (row["group"], row["name"]))
        self.cached = {
            "status": "ok",
            "checked_at": datetime.now(UTC).isoformat(),
            "summary": {
                status: sum(row["status"] == status for row in services)
                for status in ("up", "degraded", "down")
            },
            "services": services,
        }
        unknown = sum(row["status"] == "unknown" for row in services)
        if unknown:
            self.cached["summary"]["unknown"] = unknown
        self.cached_at = monotonic()
        return self.cached

    async def probe(self, probe):
        if self.session is None:
            raise RuntimeError("Probe session not started")
        started = monotonic()
        result = {"name": probe["name"], "group": probe.get("group", ""), "status": "down"}
        if is_lan(probe["url"]) and self.config.backends["eventkit"].get("enabled", False):
            try:
                async with self.lan_limit:
                    # The helper applies its own short request timeout to LAN probes.
                    data = await EventKit(self.config, timeout=4).call(
                        "net.probe", {"url": probe["url"]}
                    )
                if data.get("local_network_denied") is True or data.get("error") in (
                    "denied",
                    "local_network_denied",
                ):
                    raise CapabilityError("permission_needed", "Local network access unavailable")
                if "error" in data:
                    if data["error"] not in ("timeout", "refused", "unreachable", "tls", "other"):
                        raise ValueError
                else:
                    code, latency = data.get("http_status"), data.get("latency_ms")
                    if (
                        type(code) is not int
                        or not 100 <= code <= 599
                        or type(latency) not in (int, float)
                        or not math.isfinite(latency)
                        or latency < 0
                    ):
                        raise ValueError
                    result.update(
                        status="up" if code < 500 else "degraded",
                        http_status=code,
                        latency_ms=latency,
                    )
                    return result
            except (CapabilityError, ValueError):
                result.update(status="unknown", reason="local network permission needed")
            result["latency_ms"] = round((monotonic() - started) * 1000, 2)
            return result
        try:
            async with self.session.get(
                probe["url"],
                ssl=probe.get("tls_verify", True),
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=probe.get("timeout_s", PROBE_TIMEOUT_S)),
            ) as response:
                result.update(
                    status="up" if response.status < 500 else "degraded",
                    http_status=response.status,
                )
        except (aiohttp.ClientError, TimeoutError, OSError):
            pass
        result["latency_ms"] = round((monotonic() - started) * 1000, 2)
        return result
