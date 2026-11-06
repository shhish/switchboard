"""Plex watch state and Tautulli evidence; library search stays with media-api."""

import asyncio
import math
import re
from datetime import UTC, datetime, timedelta

from ..errors import CapabilityError
from ..maintenance import STATUS_SCHEMA, read_status
from ..schema import obj, string
from .common import array, output, sanitize_text

KIND = {"type": "string", "enum": ["movie", "show"]}
KEY = string(20)
NUMBER = {"type": ["number", "null"]}
TEXT = {"type": ["string", "null"], "maxLength": 300}
ITEM = obj(
    {
        "title": string(300),
        "kind": {"type": "string", "enum": ["movie", "show", "season", "episode"]},
        "rating_key": KEY,
        "show": TEXT,
        "season": {"type": ["integer", "null"]},
        "episode": {"type": ["integer", "null"]},
        "year": {"type": ["integer", "null"]},
        "duration_minutes": NUMBER,
        "rating": NUMBER,
        "genres": array(string(100)),
        "progress_percent": NUMBER,
    },
    ("title", "kind", "rating_key"),
)
HISTORY_ITEM = obj(
    {
        "title": string(300),
        "kind": string(20),
        "rating_key": KEY,
        "user": TEXT,
        "date": NUMBER,
        "percent_complete": NUMBER,
    },
    ("title", "kind", "user", "date", "percent_complete"),
)
SESSION = obj(
    {
        "title": string(300),
        "user": TEXT,
        "product": TEXT,
        "progress_percent": NUMBER,
        "transcode": {"type": ["boolean", "null"]},
        "rating_key": KEY,
        "parent_rating_key": KEY,
        "grandparent_rating_key": KEY,
        "kind": {"type": "string", "enum": ["movie", "episode", "track", "other"]},
        "show": TEXT,
        "season": {"type": ["integer", "null"]},
        "episode": {"type": ["integer", "null"]},
        "bandwidth_kbps": {"type": ["integer", "null"], "minimum": 0},
        "location": {"type": ["string", "null"], "enum": ["lan", "wan", None]},
        "local": {"type": ["boolean", "null"]},
    },
    (
        "title",
        "user",
        "product",
        "progress_percent",
        "transcode",
        "kind",
        "show",
        "season",
        "episode",
        "bandwidth_kbps",
        "location",
        "local",
    ),
)


def session_boolean(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if value in ("0", "1"):
        return value == "1"
    return None


def invalid(service="plex"):
    raise CapabilityError("backend_invalid", f"{service}: invalid watch response")


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, OverflowError):
        return None


def integer(value):
    value = number(value)
    return int(value) if value is not None and value.is_integer() else None


def text(value):
    return sanitize_text(value) if isinstance(value, str) and value else None


def identifier(value, *, argument=False):
    value = str(value) if type(value) is int else value
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        if argument:
            raise CapabilityError("invalid_arguments", "Use a numeric Plex rating key")
        invalid()
    return value


def rows(data, key="Metadata"):
    container = data.get("MediaContainer") if isinstance(data, dict) else None
    if not isinstance(container, dict):
        invalid()
    values = container.get(key, [])
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        invalid()
    # PMS omits an empty child array, but a nonempty container must include it.
    if key not in container and number(container.get("size", 0)) != 0:
        invalid()
    return container, values


def percent(offset, duration):
    offset, duration = number(offset), number(duration)
    return round(min(100, offset / duration * 100), 1) if offset is not None and duration else None


def item(row):
    title, kind = text(row.get("title")), row.get("type")
    if not title or kind not in ("movie", "show", "season", "episode"):
        invalid()
    genres = row.get("Genre", [])
    if not isinstance(genres, list) or any(not isinstance(g, dict) for g in genres):
        invalid()
    duration = number(row.get("duration"))
    return {
        "title": title,
        "kind": kind,
        "rating_key": identifier(row.get("ratingKey")),
        "show": text(row.get("grandparentTitle"))
        if kind == "episode"
        else text(row.get("parentTitle"))
        if kind == "season"
        else None,
        "season": integer(row.get("parentIndex"))
        if kind == "episode"
        else integer(row.get("index"))
        if kind == "season"
        else None,
        "episode": integer(row.get("index")) if kind == "episode" else None,
        "year": integer(row.get("year")),
        "duration_minutes": round(duration / 60000, 1) if duration is not None else None,
        "rating": number(row.get("rating", row.get("audienceRating"))),
        "genres": [g["tag"][:100] for g in genres if text(g.get("tag"))][:20],
        "progress_percent": percent(row.get("viewOffset", 0), duration),
    }


def matches_kind(row, kind):
    return row.get("type") in (("show", "season", "episode") if kind == "show" else ("movie",))


async def plex_list(core, path, limit, kind=None):
    # Fetch an extra result to report truncation even on older servers without totals.
    params = {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": str(limit + 1)}
    if kind:
        params["type"] = "1" if kind == "movie" else "2"
    container, values = rows(await core.backends.get("plex", path, params=params))
    supported = [r for r in values if r.get("type") in ("movie", "show", "season", "episode")]
    selected = [r for r in supported if not kind or matches_kind(r, kind)]
    total = number(container.get("totalSize"))
    return {
        "status": "ok",
        "items": [item(r) for r in selected[:limit]],
        "limit": limit,
        "truncated": len(values) > limit or (total is not None and total > len(values)),
    }


async def tautulli(core, command, **params):
    data = await core.backends.get("tautulli", "/api/v2", params={"cmd": command, **params})
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        invalid("tautulli")
    if response.get("result") != "success":
        raise CapabilityError("backend_error", "tautulli: watch request rejected; run sb doctor")
    if not isinstance(response.get("data"), dict):
        invalid("tautulli")
    return response["data"]


def tautulli_rows(data, key):
    values = data.get(key)
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        invalid("tautulli")
    return values


async def metadata(core, key):
    _, values = rows(await core.backends.get("plex", f"/library/metadata/{key}"))
    if len(values) != 1 or identifier(values[0].get("ratingKey")) != key:
        raise CapabilityError("not_found", "Plex rating key was not found")
    item(values[0])  # Validate supported type and public metadata before any write.
    return values[0]


def watched(row):
    if row["type"] in ("show", "season"):
        total, viewed = number(row.get("leafCount")), number(row.get("viewedLeafCount", 0))
        return None if not total or viewed is None else viewed >= total
    count = number(row.get("viewCount", 0))
    return None if count is None else count > 0


async def mark(core, client, args, desired):
    async with core.watch_state_lock:
        return await mark_locked(core, client, args, desired)


async def mark_locked(core, client, args, desired):
    key = identifier(args["rating_key"], argument=True)
    core.policy.require_allowed("plex", "rating_keys", key)
    row = await metadata(core, key)
    cap = "media_mark_watched" if desired else "media_mark_unwatched"
    core.policy.require(client, core.registry.capabilities[cap])
    core.policy.require_allowed("plex", "rating_keys", key)
    if watched(row) is desired and (
        desired or number(row.get("viewedLeafCount", row.get("viewCount", 0))) == 0
    ):
        return {"status": "ok", "item": item(row), "watched": desired, "changed": False}
    await core.backends.request(
        "plex",
        "/:/scrobble" if desired else "/:/unscrobble",
        params={"key": key, "identifier": "com.plexapp.plugins.library"},
        method="PUT",
    )
    verified = await metadata(core, key)
    if watched(verified) is not desired or (
        not desired and number(verified.get("viewedLeafCount", verified.get("viewCount", 0))) != 0
    ):
        raise CapabilityError(
            "watch_state_uncertain",
            "Plex did not confirm watch state; inspect reads before retrying",
        )
    return {"status": "ok", "item": item(verified), "watched": desired, "changed": True}


async def find_unwatched(core, args):
    kind, limit = args["kind"], args.get("limit", 10)
    genre = args.get("genre", "").strip().casefold()
    if "genre" in args and not genre:
        raise CapabilityError("invalid_arguments", "genre must contain non-whitespace text")
    _, sections = rows(await core.backends.get("plex", "/library/sections"), "Directory")
    sections = [s for s in sections if s.get("type") == kind]
    results, seen, requests, truncated = [], set(), 0, len(sections) > 20
    for section in sections[:20]:
        section_key = identifier(section.get("key"))
        base = f"/library/sections/{section_key}"
        params = {"type": "1" if kind == "movie" else "2", "unwatched": "1"}
        if genre:
            _, genres = rows(
                await core.backends.get("plex", base + "/genre", params={"type": params["type"]}),
                "Directory",
            )
            matches = [g for g in genres if (text(g.get("title")) or "").casefold() == genre]
            if not matches:
                continue
            params["genre"] = ",".join(identifier(g.get("key")) for g in matches)
        start = 0
        while True:
            if requests >= 10:
                return {"status": "ok", "items": results[:limit], "truncated": True, "limit": limit}
            container, values = rows(
                await core.backends.get(
                    "plex",
                    base + "/all",
                    params={
                        **params,
                        "X-Plex-Container-Start": str(start),
                        "X-Plex-Container-Size": "100",
                    },
                )
            )
            requests += 1
            for row in values:
                if row.get("type") != kind:
                    continue
                count = number(row.get("viewedLeafCount" if kind == "show" else "viewCount", 0))
                if count != 0:
                    continue
                candidate = item(row)
                if genre and genre not in [g.casefold() for g in candidate["genres"]]:
                    continue
                if "max_minutes" in args and (
                    candidate["duration_minutes"] is None
                    or number(row.get("duration")) > args["max_minutes"] * 60000
                ):
                    continue
                if "min_year" in args and (
                    candidate["year"] is None or candidate["year"] < args["min_year"]
                ):
                    continue
                if candidate["rating_key"] not in seen:
                    seen.add(candidate["rating_key"])
                    results.append(candidate)
                if len(results) > limit:
                    return {
                        "status": "ok",
                        "items": results[:limit],
                        "truncated": True,
                        "limit": limit,
                    }
            start += len(values)
            total = number(container.get("totalSize"))
            if (
                not values
                or (total is not None and start >= total)
                or (total is None and len(values) < 100)
            ):
                break
    return {"status": "ok", "items": results, "truncated": truncated, "limit": limit}


def register(registry):
    list_output = output(items=array(ITEM), truncated={"type": "boolean"})

    @registry.declare(
        "media_continue_watching",
        "Continue watching",
        "Use when resuming a movie or TV episode. Requires configured Plex read access. "
        "Returns up to 20 On Deck items and progress.",
        scope="media:read",
        service="plex",
        output_schema=list_output,
    )
    async def continue_watching(core, client, args):
        return await plex_list(core, "/library/onDeck", 20)

    @registry.declare(
        "media_recently_added",
        "Recently added to Plex",
        "Use when finding new library arrivals. Requires configured Plex read access. "
        "Returns bounded recently added movies and shows.",
        obj({"kind": KIND, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}),
        scope="media:read",
        service="plex",
        output_schema=list_output,
    )
    async def recently_added(core, client, args):
        return await plex_list(
            core, "/library/recentlyAdded", args.get("limit", 10), args.get("kind")
        )

    @registry.declare(
        "media_watch_history",
        "Watch history",
        "Use when checking what people watched. Requires configured Tautulli read access. "
        "Returns bounded multi-user history; it may be truncated.",
        obj(
            {
                "days": {"type": "integer", "minimum": 1, "maximum": 90},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "kind": KIND,
            }
        ),
        scope="media:read",
        service="tautulli",
        output_schema=output(
            history=array(HISTORY_ITEM), truncated={"type": "boolean"}, coverage=string(200)
        ),
    )
    async def history(core, client, args):
        limit = args.get("limit", 20)
        cutoff = datetime.now(UTC) - timedelta(days=args.get("days", 30))
        params = {
            "after": cutoff.date().isoformat(),
            "length": str(limit + 1),
            "start": "0",
            "order_column": "date",
            "order_dir": "desc",
            "include_activity": "0",
            "grouping": "0",
        }
        if args.get("kind"):
            params["media_type"] = "movie" if args["kind"] == "movie" else "episode"
        data = await tautulli(core, "get_history", **params)
        values = tautulli_rows(data, "data")
        result = []
        for row in values:
            kind = row.get("media_type")
            if kind not in ("movie", "episode") or (
                args.get("kind") and kind != params["media_type"]
            ):
                continue
            date = number(row.get("date"))
            title = text(row.get("full_title")) or text(row.get("title"))
            if date is None or not title:
                invalid("tautulli")
            if date < cutoff.timestamp():
                continue
            result.append(
                {
                    "title": title,
                    "kind": "show" if kind == "episode" else "movie",
                    "rating_key": identifier(row.get("rating_key")),
                    "user": text(row.get("user")) or text(row.get("friendly_name")),
                    "date": date,
                    "percent_complete": min(100, n)
                    if (n := number(row.get("percent_complete"))) is not None
                    else None,
                }
            )
        total = number(data.get("recordsFiltered"))
        return {
            "status": "ok",
            "history": result[:limit],
            "limit": limit,
            "truncated": len(values) > limit or (total is not None and total > len(values)),
            "coverage": "All Tautulli users; dates are Unix seconds; "
            "do not infer the owner's history from other users",
        }

    @registry.declare(
        "media_now_playing",
        "Now playing",
        "Use when checking current Plex playback. Requires configured Plex read access. "
        "Returns bounded titles, users, products, episode identity, progress and bandwidth.",
        scope="media:read",
        service="plex",
        output_schema=output(sessions=array(SESSION), truncated={"type": "boolean"}),
    )
    async def now_playing(core, client, args):
        _, values = rows(await core.backends.get("plex", "/status/sessions"))
        result = []
        for row in values[:20]:
            title = text(row.get("title"))
            if not title:
                invalid()
            nested = [row.get(k, {}) for k in ("User", "Player", "Session", "TranscodeSession")]
            if any(not isinstance(value, dict) for value in nested):
                invalid()
            user, player, session, transcode = nested
            kind = row.get("type")
            kind = kind if kind in ("movie", "episode", "track") else "other"
            show = text(row.get("grandparentTitle")) if kind == "episode" else None
            decisions = [
                transcode.get(k) for k in ("videoDecision", "audioDecision", "subtitleDecision")
            ]
            known = any(
                d in ("transcode", "copy", "directplay", "directstream")
                for d in decisions
                if isinstance(d, str)
            )
            public = {
                "title": f"{show} - {title}"[:300] if show else title,
                "user": text(user.get("title")),
                "product": text(player.get("product")),
                "progress_percent": percent(row.get("viewOffset"), row.get("duration")),
                "transcode": "transcode" in decisions if known else None,
                "kind": kind,
                "show": show,
                "season": integer(row.get("parentIndex")) if kind == "episode" else None,
                "episode": integer(row.get("index")) if kind == "episode" else None,
                "bandwidth_kbps": integer(session.get("bandwidth")),
                "location": session.get("location")
                if session.get("location") in ("lan", "wan")
                else None,
                "local": session_boolean(player.get("local")),
            }
            for source, target in (
                ("ratingKey", "rating_key"),
                ("parentRatingKey", "parent_rating_key"),
                ("grandparentRatingKey", "grandparent_rating_key"),
            ):
                if row.get(source) not in (None, ""):
                    public[target] = identifier(row[source])
            result.append(public)
        return {"status": "ok", "sessions": result, "truncated": len(values) > 20}

    @registry.declare(
        "media_find_unwatched",
        "Find unwatched candidates",
        "Use when deciding what should I watch. Requires configured Plex read access and "
        "movie or show kind. Returns unwatched library candidates; bounded scans may be "
        "truncated.",
        obj(
            {
                "kind": KIND,
                "genre": string(100),
                "max_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "min_year": {"type": "integer", "minimum": 1800, "maximum": 3000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ("kind",),
        ),
        scope="media:read",
        service="plex",
        timeout_s=30,
        output_schema=list_output,
    )
    async def unwatched(core, client, args):
        return await find_unwatched(core, args)

    write_schema = obj({"rating_key": KEY}, ("rating_key",))
    write_output = output(item=ITEM, watched={"type": "boolean"}, changed={"type": "boolean"})

    @registry.declare(
        "media_mark_watched",
        "Mark Plex item watched",
        "Use when correcting a Plex item to watched. Requires an explicit owner request and "
        "allowlisted rating key. Returns verified watch state; already matching items are "
        "unchanged.",
        write_schema,
        scope="media:watch_state",
        tier="write",
        service="plex",
        output_schema=write_output,
    )
    async def mark_watched(core, client, args):
        return await mark(core, client, args, True)

    @registry.declare(
        "media_mark_unwatched",
        "Mark Plex item unwatched",
        "Use when correcting a Plex item to unwatched. Requires an explicit owner request "
        "and allowlisted rating key. Returns verified watch state; already matching items "
        "are unchanged.",
        write_schema,
        scope="media:watch_state",
        tier="write",
        service="plex",
        output_schema=write_output,
    )
    async def mark_unwatched(core, client, args):
        return await mark(core, client, args, False)

    @registry.declare(
        "media_maintenance_status",
        "Media Doctor status",
        "Use when checking Media Doctor queues or repairs. Requires a configured private "
        "maintenance snapshot. Returns published work lanes and attention titles; stale "
        "snapshots are marked.",
        scope="media:read",
        service="maintenance",
        output_schema=STATUS_SCHEMA,
    )
    async def maintenance(core, client, args):
        settings = core.backends.settings("maintenance")
        return await asyncio.to_thread(read_status, settings["path"])
