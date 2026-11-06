"""Media-api reads and idempotent Overseerr requests; no direct Arr writes."""

import asyncio
import time
from urllib.parse import quote

from ..errors import CapabilityError
from ..media_compress import NOTE, REQUEST_ID, STATUSES, validate_receipt, validate_status
from ..schema import obj, string
from .common import array, output, pick, sanitize_text, selected

COUNTS = selected(
    (
        "total",
        "monitored",
        "unmonitored",
        "downloaded",
        "missing",
        "total_size_gb",
        "continuing",
        "ended",
        "total_episodes",
        "downloaded_episodes",
        "missing_episodes",
        "total_authors",
        "total_books",
    )
)
DOWNLOAD_COUNTS = selected(("active", "paused", "queued", "total_speed"))
DOWNLOAD = selected(
    ("id", "title", "status", "progress", "speed", "eta", "client", "media_type", "date")
)
LIBRARY_MATCH = selected(("id", "title", "year", "tmdb_id", "tvdb_id", "kind", "available"))
SEERR_MATCH = obj(
    {
        "tmdb_id": {"type": ["integer", "null"]},
        "kind": string(),
        "title": {"type": "string", "maxLength": 200},
        "year": {"type": "string"},
        "availability": string(),
    },
    ("tmdb_id", "kind", "title", "year", "availability"),
)


def compress_preflight(core, client, args):
    core.backends.media_compress.settings()
    if "path" in args:
        core.policy.require_compress_path(args["path"])


async def movie_directory(core, tmdb_id):
    """Read-only: the movie's library folder from media-api, or None if not in the library.
    Authorizing the folder covers the file inside it, so admit never runs unauthorized."""
    data = await core.backends.get("media", "/movies/list")
    rows = data.get("movies", data) if isinstance(data, dict) else data
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("tmdb_id") == tmdb_id and row.get("has_file"):
            path = row.get("path")
            return path if isinstance(path, str) and path.startswith("/") else None
    return None


def recorded_compression(core, client, request_id):
    if not REQUEST_ID.fullmatch(request_id):
        raise CapabilityError("invalid_arguments", "Invalid compression request ID")
    row = core.storage.db.execute(
        "SELECT * FROM compress_requests WHERE request_id=? AND (client=? OR ?='owner')",
        (request_id, client, client),
    ).fetchone()
    if row is None:
        raise CapabilityError(
            "resource_denied", "Compression request is not recorded for this client"
        )
    return row


async def request_title(core, client, args, kind):
    identifier = args["tmdb_id"]
    policy_kind = "movies" if kind == "movie" else "shows"
    async with core.media_request_lock:
        core.policy.require_allowed("media", policy_kind, str(identifier))
        data = await core.backends.get("overseerr", f"/api/v1/{kind}/{identifier}")
        if not isinstance(data, dict):
            raise CapabilityError("backend_invalid", "overseerr: invalid title details")
        title = data.get("title" if kind == "movie" else "name")
        date = data.get("releaseDate" if kind == "movie" else "firstAirDate") or ""
        info = data.get("mediaInfo")
        info = {} if info is None else info
        if (
            data.get("id") != identifier
            or not isinstance(title, str)
            or not title
            or not isinstance(date, str)
            or not isinstance(info, dict)
            or (
                "status" in info
                and (type(info["status"]) is not int or info["status"] not in range(1, 8))
            )
            or not isinstance(info.get("requests", []), list)
        ):
            raise CapabilityError("backend_invalid", "overseerr: invalid title details")
        requests = info.get("requests", [])
        if any(
            not isinstance(row, dict)
            or type(row.get("status")) is not int
            # Seerr: pending 1, approved 2, declined 3, failed 4, completed 5.
            or row["status"] not in (1, 2, 3, 4, 5)
            for row in requests
        ):
            raise CapabilityError("backend_invalid", "overseerr: invalid request details")
        active = [row for row in requests if row.get("status") in (1, 2)]
        request_id = next((row["id"] for row in active if type(row.get("id")) is int), None)
        result = {"request_id": request_id, "title": sanitize_text(title, 200), "year": date[:4]}
        if info.get("status") == 5:
            return {"status": "already_available", **result}
        if info.get("status") == 6:
            # Seerr media status 6 = blocklisted; never submit a request for it.
            return {"status": "blocklisted", **result}
        if info.get("status") in (2, 3, 4) or active:
            return {"status": "already_requested", **result}
        db = core.storage.db
        previous = db.execute(
            "SELECT request_id FROM media_requests WHERE kind=? AND tmdb_id=?", (kind, identifier)
        ).fetchone()
        if previous is not None:
            if previous["request_id"] is None:
                raise CapabilityError(
                    "request_uncertain",
                    "Prior submission may have reached Overseerr; owner must reconcile it",
                )
            return {"status": "already_requested", **result, "request_id": previous["request_id"]}
        payload = {"mediaType": kind, "mediaId": identifier}
        if kind == "tv":
            payload["seasons"] = args.get("seasons", "all")
        # Check credentials before recording an intent. Persist before sending: an ambiguous
        # timeout, cancellation or crash must never trigger an automatic duplicate POST.
        core.backends.headers("overseerr")
        core.policy.require(
            client,
            core.registry.capabilities[
                "media_request_movie" if kind == "movie" else "media_request_show"
            ],
        )
        core.policy.require_allowed("media", policy_kind, str(identifier))
        with db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO media_requests(kind,tmdb_id) VALUES (?,?)",
                (kind, identifier),
            ).rowcount
        if not inserted:
            raise CapabilityError(
                "request_uncertain", "Submission already recorded; check Overseerr"
            )
        created = await core.backends.request("overseerr", "/api/v1/request", payload=payload)
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise CapabilityError("backend_invalid", "overseerr: invalid request receipt")
        with db:
            db.execute(
                "UPDATE media_requests SET request_id=? WHERE kind=? AND tmdb_id=?",
                (created["id"], kind, identifier),
            )
        return {"status": "requested", **result, "request_id": created["id"]}


def register(registry):
    @registry.declare(
        "media_compress",
        "Request per-title compression",
        "Use when requesting compression for one library title. Requires an enabled Media "
        "Doctor backend and an allowlisted library file path. Returns a request ID; exact "
        "human approval is required and lanes may skip ineligible titles.",
        {
            **obj(
                {
                    "tmdb_id": {"type": "integer", "minimum": 1, "maximum": 10**9},
                    "path": string(1024),
                }
            ),
            "oneOf": [{"required": ["tmdb_id"]}, {"required": ["path"]}],
        },
        scope="media:compress",
        tier="approval",
        service="media_compress",
        timeout_s=65,
        preflight=compress_preflight,
        output_schema=obj(
            {
                "status": {"type": "string", "enum": ["requested", "not_found"]},
                "request_id": string(33),
                "path": string(1024),
                "note": {"type": "string", "enum": [NOTE]},
            },
            ("status", "note"),
        ),
    )
    async def compress(core, client, args):
        compress_preflight(core, client, args)
        if "tmdb_id" in args:
            directory = await movie_directory(core, args["tmdb_id"])
            if directory is None:
                return {"status": "not_found", "note": NOTE}
            core.policy.require_compress_path(directory)  # authorize BEFORE admitting
            option, value = "--tmdb-id", args["tmdb_id"]
        else:
            option, value = "--path", args["path"]
        receipt = validate_receipt(await core.backends.media_compress.run("admit", option, value))
        if receipt["status"] == "not_found":
            return {"status": "not_found", "note": NOTE}
        core.policy.require_compress_path(receipt["path"])
        with core.storage.db:
            core.storage.db.execute(
                "INSERT OR IGNORE INTO compress_requests "
                "(request_id,client,tmdb_id,path,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    receipt["request_id"],
                    client,
                    args.get("tmdb_id"),
                    args.get("path", receipt["path"]),
                    time.time(),
                ),
            )
        return {
            "status": "requested",
            "request_id": receipt["request_id"],
            "path": receipt["path"],
            "note": NOTE,
        }

    @registry.declare(
        "media_compress_status",
        "Get compression status",
        "Use when checking a compression request. Requires a recorded request ID belonging "
        "to you or owner access. Returns lane status; poll sparingly and expect skipped "
        "titles.",
        obj({"request_id": string(33)}, ("request_id",)),
        scope="media:compress_read",
        service="media_compress",
        timeout_s=65,
        output_schema=obj(
            {
                "request_id": string(33),
                "path": string(1024),
                "status": {"type": "string", "enum": STATUSES},
                "reason": string(200),
                "updated_at": {"type": ["string", "number"]},
            },
            ("request_id", "path", "status", "updated_at"),
        ),
    )
    async def compress_status(core, client, args):
        core.backends.media_compress.settings()
        recorded_compression(core, client, args["request_id"])
        return validate_status(
            await core.backends.media_compress.run("status", "--request-id", args["request_id"]),
            args["request_id"],
        )

    @registry.declare(
        "media_compress_list",
        "List compression requests",
        "Use when recovering recent compression request IDs. Requires compression read "
        "access. Returns your recorded requests; owner sees all clients.",
        obj({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        scope="media:compress_read",
        output_schema=output(
            requests=array(
                obj(
                    {
                        "request_id": string(33),
                        "client": string(20),
                        "tmdb_id": {"type": ["integer", "null"]},
                        "path": string(1024),
                        "created_at": {"type": "number"},
                    },
                    ("request_id", "client", "tmdb_id", "path", "created_at"),
                )
            )
        ),
    )
    async def compress_list(core, client, args):
        limit = args.get("limit", 20)
        rows = core.storage.db.execute(
            "SELECT * FROM compress_requests WHERE client=? OR ?='owner' "
            "ORDER BY created_at DESC, request_id DESC, client LIMIT ?",
            (client, client, limit + 1),
        ).fetchall()
        return {
            "status": "ok",
            "requests": [dict(row) for row in rows[:limit]],
            "limit": limit,
            "truncated": len(rows) > limit,
        }

    request_output = output(
        request_id={"type": ["integer", "null"]}, title=string(200), year={"type": "string"}
    )

    @registry.declare(
        "media_request_movie",
        "Request movie",
        "Use when adding a movie to the library. Requires a confirmed TMDB ID and media "
        "request allowlisting. Returns Seerr acceptance or existing availability; "
        "blocklisted movies are never requested.",
        obj({"tmdb_id": {"type": "integer", "minimum": 1}}, ("tmdb_id",)),
        scope="media:request",
        tier="write",
        service="overseerr",
        timeout_s=25,
        idempotent=True,
        output_schema=request_output,
    )
    async def request_movie(core, client, args):
        return await request_title(core, client, args, "movie")

    @registry.declare(
        "media_request_show",
        "Request show",
        "Use when adding a TV show or seasons to the library. Requires a confirmed TMDB ID "
        "and media request allowlisting. Returns Seerr acceptance or existing availability; "
        "blocklisted shows are never requested.",
        obj(
            {
                "tmdb_id": {"type": "integer", "minimum": 1},
                "seasons": {
                    "oneOf": [
                        {"type": "string", "enum": ["all"]},
                        {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0},
                            "minItems": 1,
                            "maxItems": 200,
                        },
                    ]
                },
            },
            ("tmdb_id",),
        ),
        scope="media:request",
        tier="write",
        service="overseerr",
        timeout_s=25,
        idempotent=True,
        output_schema=request_output,
    )
    async def request_show(core, client, args):
        return await request_title(core, client, args, "tv")

    @registry.declare(
        "media_library_status",
        "Media library status",
        "Use when checking media counts, storage and library health. Requires media read "
        "access. Returns compact library totals and degraded services.",
        scope="media:read",
        service="media",
        output_schema=output(
            movies=COUNTS,
            shows=COUNTS,
            books=COUNTS,
            downloads=DOWNLOAD_COUNTS,
            plex_sessions={"type": "integer"},
            total_size_gb={"type": "number"},
            degraded_services={"type": "array", "items": {"type": "string"}},
        ),
    )
    async def library(core, client, args):
        data = await core.backends.get("media", "/library/summary")
        fields = (
            "total",
            "monitored",
            "unmonitored",
            "downloaded",
            "missing",
            "total_size_gb",
            "continuing",
            "ended",
            "total_episodes",
            "downloaded_episodes",
            "missing_episodes",
            "total_authors",
            "total_books",
        )
        result = {key: pick(data.get(key, {}), fields) for key in ("movies", "shows", "books")}
        result.update(pick(data, ("plex_sessions", "total_size_gb")))
        result["downloads"] = pick(
            data.get("downloads", {}), ("active", "paused", "queued", "total_speed")
        )
        result["degraded_services"] = sorted(data.get("errors", {}))
        return {"status": "ok", **result}

    @registry.declare(
        "media_downloads",
        "Media downloads",
        "Use when checking downloads and recent completions. Requires media read access. "
        "Returns active downloads and bounded history.",
        scope="media:read",
        service="media",
        output_schema=output(
            summary=DOWNLOAD_COUNTS,
            active=array(DOWNLOAD),
            history=array(DOWNLOAD),
            truncated={"type": "boolean"},
        ),
    )
    async def downloads(core, client, args):
        active, history = await asyncio.gather(
            core.backends.get("media", "/downloads/active"),
            core.backends.get("media", "/downloads/history", params={"limit": "10"}),
        )
        rows = active.get("downloads", [])
        keys = ("id", "title", "status", "progress", "speed", "eta", "client", "media_type")
        return {
            "status": "ok",
            "summary": pick(active, ("active", "paused", "queued", "total_speed")),
            "active": [pick(row, keys) for row in rows[:20]],
            "history": [
                pick(row, ("id", "title", "status", "date", "client", "media_type"))
                for row in history.get("history", [])[:10]
            ],
            "truncated": len(rows) > 20 or len(history.get("history", [])) > 10,
        }

    @registry.declare(
        "media_search",
        "Search media",
        "Use when finding a movie or TV show before requesting it. Requires a title query. "
        "Returns library and Seerr matches; resolve ambiguity using year, kind and TMDB ID.",
        obj(
            {"query": string(200), "kind": {"type": "string", "enum": ["movie", "show"]}},
            ("query",),
        ),
        scope="media:read",
        service="media+overseerr",
        timeout_s=25,
        output_schema=output(
            library=array(LIBRARY_MATCH),
            overseerr=array(SEERR_MATCH),
            warnings={"type": "array", "items": {"type": "string"}},
        ),
    )
    async def search(core, client, args):
        query = args["query"].strip().casefold()
        if not query:
            raise CapabilityError("invalid_arguments", "query must contain non-whitespace text")
        library, results, warnings = [], [], []
        for kind, path, key in (
            ("movie", "/movies/list", "movies"),
            ("show", "/shows/list", "shows"),
        ):
            if args.get("kind") and args["kind"] != kind:
                continue
            # media-api has no title filter; filter here on the server, before sending to agents.
            try:
                data = await core.backends.get("media", path)
                for row in data.get(key, []):
                    if query in row.get("title", "").casefold():
                        match = pick(row, ("id", "title", "year", "tmdb_id", "tvdb_id"))
                        match.update(
                            kind=kind,
                            available=bool(row.get("has_file"))
                            if kind == "movie"
                            else row.get("episode_file_count", 0) > 0,
                        )
                        library.append(match)
            except CapabilityError as exc:
                warnings.append(str(exc))
        settings = core.config.backends["overseerr"]
        if settings.get("enabled", False):
            try:
                # Overseerr rejects form-style "+" for spaces (HTTP 400), so encode
                # the query ourselves with %20 instead of passing params.
                data = await core.backends.get(
                    "overseerr", "/api/v1/search?query=" + quote(args["query"], safe="") + "&page=1"
                )
                for row in data.get("results", []):
                    kind = {"movie": "movie", "tv": "show"}.get(row.get("mediaType"))
                    if kind is None or (args.get("kind") and args["kind"] != kind):
                        continue
                    info = row.get("mediaInfo") or {}
                    availability = {
                        1: "unknown",
                        2: "pending",
                        3: "processing",
                        4: "partially_available",
                        5: "available",
                        # Seerr 3.x: 6 blocklisted, 7 deleted (can be requested again).
                        6: "blocklisted",
                        7: "deleted",
                    }.get(info.get("status"), "not_requested")
                    results.append(
                        {
                            "tmdb_id": row.get("id"),
                            "kind": kind,
                            "title": sanitize_text(row.get("title") or row.get("name") or "", 200),
                            "year": (row.get("releaseDate") or row.get("firstAirDate") or "")[:4],
                            "availability": availability,
                        }
                    )
                # Sonarr's list has TVDB IDs, not TMDB. Enrich only exact title/year matches.
                for match in library:
                    if match.get("tmdb_id") is not None:
                        continue
                    for found in results:
                        if (
                            match["kind"] == found["kind"]
                            and match["title"].casefold() == found["title"].casefold()
                            and str(match.get("year", "")) == found["year"]
                        ):
                            match["tmdb_id"] = found["tmdb_id"]
            except CapabilityError as exc:
                warnings.append(str(exc))
        else:
            warnings.append("Overseerr disabled; show TMDB IDs may be unavailable")
        if not library and not results and any("media:" in warning for warning in warnings):
            raise CapabilityError("backend_down", "Media search unavailable; run sb doctor")
        return {
            "status": "ok",
            "library": library[:10],
            "overseerr": results[:10],
            "warnings": warnings,
            "truncated": len(library) > 10 or len(results) > 10,
        }
