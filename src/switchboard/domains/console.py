"""PlayStation 5: power through the remote host agent, details and games through PS5Upload.

Power (status, wake, rest) goes through the Pi agent, which sits on the console's LAN:
Sony's discovery protocol needs no credentials, and wake/rest use a Remote Play pairing
the owner makes once there. Pairing is never a tool.

Hardware, the game library, launching and closing come from the PS5Upload engine on
loopback (`backends.ps5upload`), which talks to its helper payload on a jailbroken
console. Only fixed read routes plus launch/close are used: never its shell, files,
payloads, cheats, fan control, reboot/shutdown or raw helper call. Only titles with store
IDs (CUSA/PPSA) are launched; that keeps loaders such as Payload Manager out, but it is a
best-effort filter: homebrew can reuse a store-style ID and the console doesn't mark it.
"""

import asyncio
import ipaddress
import re

from ..errors import CapabilityError
from ..netrules import RFC1918_NETWORKS
from ..schema import obj, string
from .common import array, output

ENGINE = "ps5upload"
GAME_ID = re.compile(r"(CUSA|PPSA)[0-9]{5}")
ANY_ID = re.compile(r"[A-Z]{4}[0-9]{5}")
GAME_ID_SCHEMA = {**string(9), "pattern": "^(CUSA|PPSA)[0-9]{5}$"}

POWER = {"type": "string", "enum": ["awake", "rest", "off", "unknown"]}
TITLE = obj({"id": string(40), "name": string(120)}, ("id", "name"))
NULLABLE_TITLE = {**TITLE, "type": ["object", "null"]}
CONSOLE = obj(
    {
        "found": {"type": "boolean"},
        "power": POWER,
        "name": string(60),
        "address": string(15),
        "firmware": {"type": ["string", "null"], "maxLength": 10},
        "running_title": NULLABLE_TITLE,
    },
    ("found", "power"),
)
BRIDGE = obj(
    {"installed": {"type": "boolean"}, "paired": {"type": "boolean"}}, ("installed", "paired")
)
NUM = {"type": "number"}
DETAILS = obj(
    {
        "model": string(40),
        "cpu_temp_c": NUM,
        "soc_temp_c": NUM,
        "ssd_temp_c": NUM,
        "cpu_mhz": NUM,
        "uptime_s": {"type": "integer", "minimum": 0},
        "storage_total_bytes": {"type": "integer", "minimum": 0},
        "storage_free_bytes": {"type": "integer", "minimum": 0},
        "helper_version": string(20),
    }
)
STATUS = output(
    console=CONSOLE,
    bridge=BRIDGE,
    details=DETAILS,
    details_unavailable=string(120),
)
STATUS["required"] += ["console", "bridge"]
CONTROL = output(
    action={"type": "string", "enum": ["wake", "standby"]},
    changed={"type": "boolean"},
    reached={"type": "boolean"},
    console=CONSOLE,
)
CONTROL["required"] += ["action", "changed", "reached", "console"]
GAME = obj(
    {
        "title_id": string(9),
        "name": string(120),
        "platform": {"type": "string", "enum": ["ps5", "ps4", "homebrew"]},
        "launchable": {"type": "boolean"},
    },
    ("title_id", "name", "platform", "launchable"),
)
GAMES = output(
    games={**array(GAME), "maxItems": 300},
    running=NULLABLE_TITLE,
    running_unknown={"type": "boolean"},
)
GAMES["required"] += ["games", "running"]
LAUNCH = output(launched=TITLE)
CLOSE = output(closed={**array(TITLE), "maxItems": 8})
UNAVAILABLE = "Open the PS5Upload app on the Mac (its engine serves these)"


def bridge_host(core):
    hosts = core.config.remote_hosts or {}
    if len(hosts) != 1:
        raise CapabilityError(
            "backend_disabled", "Console control needs one configured remote host"
        )
    return next(iter(hosts))


async def ps5(core, action, schema):
    from .. import remote

    data = await remote.call(core, bridge_host(core), "ps5", action)
    return remote.project({**data, "status": "ok"}, schema)


# ─── PS5Upload engine ───────────────────────────────────────────────────────


def engine_ready(core):
    return core.config.backends.get(ENGINE, {}).get("enabled", False)


async def engine_get(core, path, addr):
    data = await core.backends.get(ENGINE, path, params={"addr": addr})
    if not isinstance(data, dict):
        raise CapabilityError("backend_invalid", "ps5upload: unexpected response")
    return data


async def engine_post(core, path, payload):
    data = await core.backends.request(ENGINE, path, payload=payload)
    if not isinstance(data, dict):
        raise CapabilityError("backend_invalid", "ps5upload: unexpected response")
    return data


async def engine_console(core):
    """The one console saved in PS5Upload, as a private IPv4 literal."""
    if not engine_ready(core):
        raise CapabilityError("backend_disabled", "ps5upload is disabled")
    data = await core.backends.get(ENGINE, "/api/consoles")
    rows = data.get("consoles") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CapabilityError("backend_disabled", "PS5Upload needs exactly one saved console")
    host = rows[0].get("host")
    try:
        if not isinstance(host, str):
            raise ValueError
        address = ipaddress.IPv4Address(host)
        if str(address) != host:
            raise ValueError
    except ValueError:
        raise CapabilityError("backend_invalid", "ps5upload: invalid console address") from None
    if not any(address in net for net in RFC1918_NETWORKS) or host.endswith((".0", ".255")):
        raise CapabilityError("backend_invalid", "ps5upload: console must be on the LAN")
    return host


def number(value, low, high):
    return value if type(value) in (int, float) and low <= value <= high else None


def platform(title_id):
    return (
        "ps5"
        if title_id.startswith("PPSA")
        else "ps4"
        if title_id.startswith("CUSA")
        else "homebrew"
    )


async def installed(core, addr):
    """Installed titles, live from the console; system titles are dropped."""
    data = await engine_get(core, "/api/ps5/apps/installed", addr)
    rows = data.get("titles")
    if not isinstance(rows, list):
        raise CapabilityError("backend_invalid", "ps5upload: unexpected title list")
    games = {}
    for row in rows[:500]:
        if not isinstance(row, dict) or row.get("system") is True:
            continue
        tid, name = row.get("title_id"), row.get("title_name")
        if not isinstance(tid, str) or not ANY_ID.fullmatch(tid) or tid.startswith("NPXS"):
            continue
        name = name.strip()[:120] if isinstance(name, str) and name.strip() else tid
        games[tid] = {
            "title_id": tid,
            "name": name,
            "platform": platform(tid),
            "launchable": bool(GAME_ID.fullmatch(tid)),
        }
    return games


async def running_apps(core, addr):
    """{title_id: app_id} for the games/apps open now (not system or payload processes)."""
    data = await engine_get(core, "/api/ps5/process/list", addr)
    rows = data.get("processes")
    if not isinstance(rows, list):
        raise CapabilityError("backend_invalid", "ps5upload: unexpected process list")
    apps = {}
    for row in rows[:1000]:
        if not isinstance(row, dict) or row.get("kind") in ("system", "payload"):
            continue
        tid, app_id = row.get("title_id"), row.get("app_id")
        if (
            isinstance(tid, str)
            and ANY_ID.fullmatch(tid)
            and not tid.startswith("NPXS")
            and type(app_id) is int
            and app_id > 0
        ):
            apps.setdefault(tid, app_id)
    return apps


async def names(core, addr, ids):
    """Title names for ids from the engine's last read of the library (no console call).

    Never fails: a missing or broken cache falls back to the title IDs themselves.
    """
    if not ids:
        return {}
    try:
        data = await core.backends.get(ENGINE, "/api/games/installed")
    except CapabilityError:
        data = []
    known = {}
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        tid, title = row.get("title_id"), row.get("title")
        if isinstance(tid, str) and tid in ids and isinstance(title, str) and title.strip():
            known[tid] = title.strip()[:120]
    return {tid: known.get(tid, tid) for tid in ids}


async def details(core):
    """Hardware, storage and the open title. Any single read may fail on its own."""
    addr = await engine_console(core)
    temps, storage, power, info, helper, apps = await asyncio.gather(
        engine_get(core, "/api/ps5/hw/temps", addr),
        engine_get(core, "/api/ps5/hw/storage", addr),
        engine_get(core, "/api/ps5/hw/power", addr),
        engine_get(core, "/api/ps5/hw/info", addr),
        engine_get(core, "/api/ps5/status", addr),
        running_apps(core, addr),
        return_exceptions=True,
    )
    reads = (temps, storage, power, info, helper, apps)
    if all(isinstance(r, BaseException) for r in reads):
        known = [r for r in reads if isinstance(r, CapabilityError)]
        raise known[0] if known else CapabilityError("backend_down", "ps5upload: unreachable")
    out = {}
    model = info.get("model") if isinstance(info, dict) else None
    if isinstance(model, str) and model.split():
        # Model only: the hardware answer also carries the serial number, which stays out.
        out["model"] = model.split()[0][:40]
    if isinstance(temps, dict) and temps.get("stale") is not True:
        for key, field in (
            ("cpu_temp", "cpu_temp_c"),
            ("soc_temp", "soc_temp_c"),
            ("m2_temp", "ssd_temp_c"),
        ):
            if (value := number(temps.get(key), 1, 125)) is not None:
                out[field] = value
        if (mhz := number(temps.get("cpu_freq_mhz"), 1, 10000)) is not None:
            out["cpu_mhz"] = mhz
    if (
        isinstance(power, dict)
        and (up := number(power.get("operating_time_sec"), 0, 10**9)) is not None
    ):
        out["uptime_s"] = int(up)
    if isinstance(storage, dict):
        total = number(storage.get("user_total_bytes"), 1, 2**53)
        free = number(storage.get("user_free_bytes"), 0, 2**53)
        if total is not None and free is not None and free <= total:
            out["storage_total_bytes"], out["storage_free_bytes"] = int(total), int(free)
    version = helper.get("version") if isinstance(helper, dict) else None
    if isinstance(version, str) and version.strip():
        out["helper_version"] = version.strip()[:20]
    # Ellipsis means unknown (the process read failed); None means nothing is open.
    running = ...
    if isinstance(apps, dict):
        running = None
        if apps:
            tid = next(iter(apps))
            running = {"id": tid, "name": (await names(core, addr, {tid}))[tid]}
    return {k: v for k, v in out.items() if v is not None}, running


def register(registry):
    @registry.declare(
        "console_status",
        "PlayStation status",
        "Use when checking whether the PS5 is on, in rest mode or off, how hot it runs, "
        "how much space is free or which game is open. Requires Home read access. Returns "
        "power and wake/rest pairing; temperatures, storage and uptime only while it is "
        "awake and the PS5Upload app is open on the Mac.",
        scope="home:read",
        service="console",
        timeout_s=25,
        output_schema=STATUS,
    )
    async def status(core, client, args):
        power, extra = await asyncio.gather(
            ps5(core, "status", STATUS), details(core), return_exceptions=True
        )
        if isinstance(power, BaseException):
            raise power
        if isinstance(extra, BaseException):
            if not isinstance(extra, Exception):
                raise extra  # cancellation and the like
            if power["console"]["power"] == "awake":
                down = isinstance(extra, CapabilityError) and extra.code in (
                    "backend_down",
                    "backend_disabled",
                )
                power["details_unavailable"] = (
                    UNAVAILABLE if down else "PS5Upload gave an answer Switchboard couldn't read"
                )
            return power
        if power["console"]["power"] == "awake":
            found, running = extra
            if found:
                power["details"] = found
            if running is ...:
                # Unknown: don't let the Pi's always-empty discovery field claim "nothing".
                power["console"].pop("running_title", None)
            else:
                power["console"]["running_title"] = running
        return power
        if power["console"]["power"] == "awake":
            found, running = extra
            if found:
                power["details"] = found
            power["console"]["running_title"] = running
        return power

    @registry.declare(
        "console_wake",
        "Wake PlayStation",
        "Use when turning the PS5 on from rest mode. Requires console:control and the "
        "owner's one-time pairing. Returns whether it reached awake within about 20s; "
        "a fully powered-off console cannot be woken.",
        scope="console:control",
        tier="write",
        service="console",
        timeout_s=75,
        output_schema=CONTROL,
    )
    async def wake(core, client, args):
        return await ps5(core, "wake", CONTROL)

    @registry.declare(
        "console_standby",
        "Put PlayStation in rest mode",
        "Use when turning the PS5 off into rest mode. Requires console:control and the "
        "owner's one-time pairing; any game in progress is suspended. Returns whether it "
        "reached rest within about 20s.",
        scope="console:control",
        tier="write",
        service="console",
        timeout_s=75,
        output_schema=CONTROL,
    )
    async def standby(core, client, args):
        return await ps5(core, "standby", CONTROL)

    @registry.declare(
        "console_games",
        "PlayStation game library",
        "Use when listing the games and apps installed on the PS5, or finding a title ID "
        "before console_launch. Requires Home read access and the PS5Upload app open on "
        "the Mac. Returns titles by name with platform, whether Switchboard may launch it, "
        "and the title open now.",
        scope="home:read",
        service="console",
        timeout_s=20,
        output_schema=GAMES,
    )
    async def games(core, client, args):
        addr = await engine_console(core)
        library, apps = await asyncio.gather(
            installed(core, addr), running_apps(core, addr), return_exceptions=True
        )
        if isinstance(library, BaseException):
            raise library
        rows = sorted(library.values(), key=lambda g: (not g["launchable"], g["name"].lower()))
        result = {"status": "ok", "games": rows[:300], "running": None}
        if isinstance(apps, BaseException):
            if not isinstance(apps, Exception):
                raise apps
            result["running_unknown"] = True
        elif apps:
            tid = next(iter(apps))
            result["running"] = {"id": tid, "name": library[tid]["name"] if tid in library else tid}
        return result

    @registry.declare(
        "console_launch",
        "Launch PlayStation game",
        "Use when starting an installed PS4 or PS5 game or app on the PS5. Requires "
        "console:control, an installed CUSA/PPSA title ID from console_games, the console "
        "awake and the PS5Upload app open. Loader-style homebrew IDs are refused. Returns "
        "the title it asked the console to start.",
        obj({"title_id": GAME_ID_SCHEMA}, ("title_id",)),
        scope="console:control",
        tier="write",
        service="console",
        idempotent=False,
        timeout_s=40,
        output_schema=LAUNCH,
    )
    async def launch(core, client, args):
        addr = await engine_console(core)
        library = await installed(core, addr)
        game = library.get(args["title_id"])
        if not game or not game["launchable"]:
            raise CapabilityError("not_found", "That title isn't installed as a game")
        data = await engine_post(
            core, "/api/ps5/app/launch", {"addr": addr, "title_id": game["title_id"]}
        )
        if data.get("ok") is not True:
            raise CapabilityError("backend_error", "The console did not start the title")
        return {"status": "ok", "launched": {"id": game["title_id"], "name": game["name"]}}

    @registry.declare(
        "console_close",
        "Close PlayStation game",
        "Use when closing the game or app open on the PS5. Requires console:control and the "
        "PS5Upload app open. Unsaved progress is lost, so confirm with the human first. "
        "Optional title_id closes only that title. Returns what was closed (empty if "
        "nothing was open).",
        obj({"title_id": GAME_ID_SCHEMA}),
        scope="console:control",
        tier="write",
        service="console",
        idempotent=False,
        timeout_s=40,
        output_schema=CLOSE,
    )
    async def close(core, client, args):
        addr = await engine_console(core)
        apps = await running_apps(core, addr)
        if "title_id" in args:
            apps = {k: v for k, v in apps.items() if k == args["title_id"]}
        closed = []
        titled = await names(core, addr, set(apps)) if apps else {}
        for tid, app_id in list(apps.items())[:8]:
            data = await engine_post(
                core, "/api/ps5/app/lifecycle", {"addr": addr, "action": "kill", "app_id": app_id}
            )
            if data.get("ok") is not True:
                raise CapabilityError("backend_error", "The console did not close the title")
            closed.append({"id": tid, "name": titled.get(tid, tid)})
        return {"status": "ok", "closed": closed}
