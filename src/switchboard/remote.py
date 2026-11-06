"""Fixed forced-command SSH protocol for configured private hosts; no shell or stderr."""

import asyncio
import json
import os
import stat

from . import hostcmd
from .config import validate_remote_hosts
from .errors import CapabilityError
from .schema import validate


def private_files(host):
    # SSH reads the files itself; inspect metadata without opening/printing either.
    for field in ("key_file", "known_hosts_file"):
        info = os.stat(host[field], follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError("Unsafe remote credential file")


# Per-command deadlines; the native helper mirrors these in RemoteSSH.deadline(for:).
TIMEOUTS = {"restart": 100, "refresh-updates": 190, "ps5": 70}

# Agent refusals that mean something specific to the caller.
REFUSALS = {
    "not_allowed": ("resource_denied", "Remote service restart is not allowed", None),
    "not_paired": (
        "owner_required",
        "Console control is not paired yet",
        "The owner pairs it once with scripts/owner-setup-ps5.sh",
    ),
    "console_offline": ("backend_down", "Console is off or not on the network", None),
    "control_failed": ("backend_error", "Console did not accept the request", None),
}


async def via_helper(core, host, request, argv, deadline_s, hosts):
    """Run the fixed ssh through the native helper, which holds the Local Network grant.

    macOS Local Network privacy blocks ssh started by the launchd Python service ("No
    route to host"), exactly as it blocked direct LAN probes. The helper revalidates
    every field and builds the same argv itself.
    """
    from .eventkit import EventKit

    if not hostcmd.valid_argv(argv, hosts):
        raise CapabilityError("invalid_arguments", "Unsupported remote request")
    if not core.config.backends.get("eventkit", {}).get("enabled", False):
        raise CapabilityError("backend_down", "Remote hosts need the native helper")
    try:
        data = await EventKit(core.config, timeout=deadline_s + 10).call(
            "remote.ssh",
            {
                "host": host["address"],
                "user": host["user"],
                "request": request,
                "key_file": host["key_file"],
                "known_hosts_file": host["known_hosts_file"],
            },
        )
    except CapabilityError:
        return -1, ""
    code, stdout = data.get("exit_code"), data.get("stdout")
    if type(code) not in (int, float) or not isinstance(stdout, str):
        return -1, ""
    return int(code), stdout


async def call(core, host_name, *tokens):
    hosts = validate_remote_hosts(core.config.remote_hosts)
    if host_name not in hosts:
        raise CapabilityError("not_found", "Unknown configured remote host")
    if not tokens or not all(hostcmd.remote_token(t) for t in tokens):
        raise CapabilityError("invalid_arguments", "Unsupported remote request")
    request = " ".join(tokens)
    if not hostcmd.valid_request(request):
        raise CapabilityError("invalid_arguments", "Unsupported remote request")
    host = hosts[host_name]
    timeout = TIMEOUTS.get(tokens[0], 20)
    runner = getattr(getattr(core, "services", None), "runner", hostcmd.run)
    try:
        async with asyncio.timeout(timeout):
            try:
                await asyncio.to_thread(private_files, host)
            except (OSError, ValueError):
                raise CapabilityError(
                    "resource_denied", "Remote credential files must be owned regular 0600 files"
                ) from None
            argv = hostcmd.ssh_argv(host, request)
            if runner is hostcmd.run:
                code, stdout = await via_helper(core, host, request, argv, timeout, hosts)
            else:
                code, stdout, _ = await runner(argv, timeout_s=timeout, remote_hosts=hosts)
    except (OSError, TimeoutError):
        code, stdout = -1, ""
    if code in (-1, 255):
        raise CapabilityError("backend_down", f"{host_name} unreachable")
    try:
        data = json.loads(stdout)
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise CapabilityError("backend_invalid", "Invalid remote agent response") from None
    if not data["ok"]:
        if data.get("code") in REFUSALS:
            code, message, hint = REFUSALS[data["code"]]
            raise CapabilityError(code, message, hint=hint)
        invalid = isinstance(data.get("error"), str) and (
            data["error"] in {"unsupported request", "unknown service"}
            or (isinstance(data.get("error"), str) and data["error"].startswith("usage:"))
        )
        raise CapabilityError(
            "invalid_arguments" if invalid else "backend_error", "Remote agent rejected request"
        )
    if code:
        raise CapabilityError("backend_error", "Remote agent command failed")
    return data


def project(data, schema):
    """Keep declared fields only, bound collections, then validate untrusted JSON."""

    def kinds(spec):
        kind = spec.get("type")
        return set(kind) if isinstance(kind, list) else {kind}

    def select(value, spec):
        if isinstance(value, dict) and "object" in kinds(spec):
            return {
                key: select(value[key], child)
                for key, child in spec["properties"].items()
                if key in value
            }
        if isinstance(value, list) and "array" in kinds(spec):
            return [select(item, spec["items"]) for item in value[: spec.get("maxItems", 200)]]
        return value

    result = select(data, schema)
    try:
        validate(result, schema)
    except CapabilityError:
        raise CapabilityError("backend_invalid", "Invalid remote agent response") from None
    return result
