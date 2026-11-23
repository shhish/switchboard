"""Named Homebrew maintenance with held/major gates and bounded cached inventory."""

import asyncio
import copy
import json
import re
import time

from . import hostcmd
from .errors import CapabilityError

# Same list as homelab-ops config/brew-policy.json (weekly brew maintenance): services
# and their toolchains are upgraded only with the owner's approval.
DEFAULT_HELD = [
    "cloudflared",
    "gitea",
    "gitea-runner",
    "nginx",
    "python@*",
    "node",
    "node@*",
    "ffmpeg",
    "ffmpeg@*",
    "mkvtoolnix",
    "asc",
    "dotnet",
    "openssl@*",
    "sqlite",
    "docker-desktop",
    "postgresql@*",
]
MAX_PACKAGES = 100


def settings(value):
    if not isinstance(value, dict) or set(value) - {"held"}:
        raise ValueError("brew accepts only held")
    held = value.get("held", DEFAULT_HELD)
    if (
        not isinstance(held, list)
        or len(held) > 200
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+_.@-]{0,127}\*?", name)
            for name in held
        )
    ):
        raise ValueError("brew.held requires names with an optional trailing star")
    return {"held": list(dict.fromkeys(held))}


class Brew:
    def __init__(self, core):
        self.core = core
        self.runner = hostcmd.run
        self.lock = asyncio.Lock()
        self.cached = None
        self.cached_at = 0

    async def command(self, argv, timeout_s):
        code, stdout, _ = await self.runner(argv, timeout_s=timeout_s)
        if code:
            raise CapabilityError(
                "brew_unavailable", "Homebrew command failed or exceeded its limit"
            )
        return stdout

    def held(self, name):
        name = name.rsplit("/", 1)[-1]
        return any(
            name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern
            for pattern in self.core.config.brew["held"]
        )

    async def inventory(self, *, fresh=False):
        if not fresh and self.cached is not None and time.monotonic() - self.cached_at < 600:
            return copy.deepcopy(self.cached)
        text = await self.command([hostcmd.BREW, "outdated", "--json=v2"], 60)
        try:
            data = json.loads(text)
            result = {"status": "ok", "formulae": [], "casks": [], "truncated": False}
            for kind in ("formulae", "casks"):
                rows = data[kind]
                if not isinstance(rows, list):
                    raise ValueError
                result["truncated"] |= len(rows) > MAX_PACKAGES
                for row in rows[:MAX_PACKAGES]:
                    name = row["name"]
                    installed = row["installed_versions"]
                    # Homebrew cask JSON uses installed_versions too.
                    current = row["current_version"]
                    if isinstance(installed, str):
                        installed = [installed]
                    if (
                        not hostcmd.brew_name(name, listed=True)
                        or not isinstance(installed, list)
                        or not 1 <= len(installed) <= 8
                        or not all(isinstance(v, str) and 1 <= len(v) <= 100 for v in installed)
                        or not isinstance(current, str)
                        or not 1 <= len(current) <= 100
                        or type(row.get("pinned", False)) is not bool
                    ):
                        raise ValueError
                    result[kind].append(
                        {
                            "name": name,
                            "installed": installed,
                            "current": current,
                            "major": any(
                                v.split(".")[0] != current.split(".")[0] for v in installed
                            ),
                            "held": self.held(name),
                            "pinned": row.get("pinned", False),
                        }
                    )
        except (ValueError, KeyError, TypeError):
            raise CapabilityError("brew_unavailable", "Invalid Homebrew inventory") from None
        self.cached, self.cached_at = result, time.monotonic()
        return copy.deepcopy(result)

    async def outdated(self):
        async with self.lock:
            return await self.inventory()

    async def refresh(self):
        async with self.lock:
            self.cached = None
            await self.command([hostcmd.BREW, "update"], 300)
            return await self.inventory(fresh=True)

    async def target(self, name, allow_held):
        if not hostcmd.brew_name(name):
            raise CapabilityError("invalid_arguments", "Invalid Homebrew package name")
        data = await self.inventory(fresh=True)
        matches = [
            (kind, row)
            for kind in ("formulae", "casks")
            for row in data[kind]
            if row["name"] == name
        ]
        if len(matches) != 1:
            raise CapabilityError(
                "brew_not_outdated",
                "Package is not uniquely present in the bounded outdated inventory",
            )
        kind, row = matches[0]
        if not allow_held and row["held"]:
            raise CapabilityError(
                "brew_held", "Held package requires ops_brew_upgrade_held approval"
            )
        if not allow_held and row["major"]:
            raise CapabilityError(
                "brew_major", "Major upgrade requires ops_brew_upgrade_held approval"
            )
        return kind, row

    async def upgrade(self, name, allow_held=False):
        async with self.lock:
            kind, before = await self.target(name, allow_held)
            self.cached = None
            try:
                await self.command(
                    [hostcmd.BREW, "upgrade", *(["--cask"] if kind == "casks" else []), name], 900
                )
                after = await self.inventory(fresh=True)
            finally:
                self.cached = None
            remaining = next((r for r in after[kind] if r["name"] == name), None)
            if remaining or after["truncated"]:
                raise CapabilityError(
                    "brew_upgrade_unverified",
                    "Upgrade finished but inventory does not confirm completion",
                )
            return {
                "status": "ok",
                "name": name,
                "kind": kind,
                "before": before["installed"],
                "after": [before["current"]],
                "verification": "Successful upgrade; package no longer outdated",
            }
