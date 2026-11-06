"""Watch contracts use ephemeral fake Plex/Tautulli and private fixture snapshots only."""

import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml
from aiohttp import web

from switchboard.config import Config
from switchboard.core import Core
from switchboard.domains.watch import number
from switchboard.errors import CapabilityError
from switchboard.maintenance import read_status

from .conftest import change_policy, running_app

READS = [
    ("media_continue_watching", {}),
    ("media_recently_added", {}),
    ("media_watch_history", {}),
    ("media_now_playing", {}),
    ("media_find_unwatched", {"kind": "movie"}),
    ("media_maintenance_status", {}),
]
WRITES = [
    ("media_mark_watched", {"rating_key": "42"}),
    ("media_mark_unwatched", {"rating_key": "42"}),
]


def movie(key="42", **changes):
    return {
        "ratingKey": key,
        "title": "Arrival",
        "type": "movie",
        "year": 2016,
        "duration": 6000000,
        "rating": "8.1",
        "Genre": [{"tag": "Drama"}, {"tag": "Science Fiction"}],
        "viewCount": 0,
        "viewOffset": 3000000,
        "Media": [{"Part": [{"file": "/PRIVATE/library/movie.mkv"}]}],
        "secret": "PRIVATE",
        **changes,
    }


def container(values, key="Metadata", **extra):
    return {"MediaContainer": {key: values, "size": len(values), **extra}}


def snapshot(**changes):
    return {
        "version": 1,
        "generatedAt": time.time(),
        "compatibilityPending": 3,
        "compressionPending": 2,
        "compressionInstallPending": 1,
        "repairHolds": 4,
        "lanes": [
            {"id": "requests", "pending": 1, "status": "idle", "title": "PRIVATE"},
            {"id": "compatibility", "pending": 3, "status": "working", "lastProgressAt": 100},
            {"id": "compression", "pending": 2, "status": "working", "lastProgressAt": None},
        ],
        "items": [
            {
                "title": "/PRIVATE/media/Arrival",
                "stage": "repair",
                "blocker": "/PRIVATE/error",
                "detail": "PRIVATE",
            },
            {"title": "Ready", "stage": "ready", "blocker": None},
        ],
        "playback": {"sessions": [{"path": "/PRIVATE/video.mkv", "id": "PRIVATE"}]},
        **changes,
    }


@pytest.fixture
async def watch(config, fake_backends):
    calls = []
    path = Path(config.backends["maintenance"]["path"])
    await asyncio.to_thread(path.write_text, json.dumps(snapshot()))
    await asyncio.to_thread(path.chmod, 0o600)
    for name, field, value in (
        ("plex", "token_file", "fake-plex-secret"),
        ("tautulli", "api_key_file", "fake-tautulli-secret"),
    ):
        secret = config.config_dir / (name + ".secret")
        secret.write_text(value)
        secret.chmod(0o600)
        config.backends[name].update({field: str(secret), "enabled": True})
    config.backends["maintenance"]["enabled"] = True
    change_policy(config, lambda p: p["clients"]["dot"]["scopes"].append("media:watch_state"))
    change_policy(
        config,
        lambda p: p["allowlists"].update(plex={"rating_keys": ["42"]}, media={"allow_all": True}),
    )
    state = {
        "on_deck": container(
            [
                movie(),
                movie(
                    "43",
                    title="Pilot",
                    type="episode",
                    grandparentTitle="A Show",
                    parentIndex=1,
                    index=2,
                ),
            ]
        ),
        "recent": container([movie()]),
        "sections": container(
            [
                {"key": "1", "type": "movie"},
                {"key": "2", "type": "show"},
                {"key": "3", "type": "artist"},
            ],
            "Directory",
        ),
        "genres": container([{"key": "9", "title": "Drama"}], "Directory"),
        "candidates": [movie()],
        "metadata": movie(),
        "history": {
            "data": [
                {
                    "date": int(time.time()),
                    "full_title": "Arrival",
                    "user": "Other user",
                    "media_type": "movie",
                    "rating_key": 42,
                    "percent_complete": "93",
                    "ip_address": "PRIVATE",
                    "machine_id": "PRIVATE",
                }
            ],
            "recordsFiltered": 1,
        },
        "sessions": container(
            [
                movie(
                    User={"title": "Viewer", "id": "PRIVATE"},
                    Player={
                        "product": "Plex for Roku",
                        "local": True,
                        "address": "PRIVATE",
                        "machineIdentifier": "PRIVATE",
                    },
                    Session={"bandwidth": 12000, "location": "lan", "id": "PRIVATE"},
                    TranscodeSession={"videoDecision": "copy", "audioDecision": "transcode"},
                    viewOffset=2550000,
                )
            ]
        ),
        "mutate": True,
    }

    async def handle(request):
        if request.path == "/api/v2":
            assert request.query["apikey"] == "fake-tautulli-secret"
            assert "Authorization" not in request.headers
            query = {k: v for k, v in request.query.items() if k != "apikey"}
        else:
            assert request.headers["X-Plex-Token"] == "fake-plex-secret"
            assert request.headers["Accept"] == "application/json"
            assert "Authorization" not in request.headers
            assert "X-Plex-Token" not in request.query
            query = dict(request.query)
        calls.append((request.method, request.path, query))
        if "http_status" in state:
            return web.Response(status=state["http_status"], text="PRIVATE")
        if request.path == "/api/v2":
            assert request.method == "GET"
            command = request.query["cmd"]
            assert command in ("get_history", "get_activity")
            return web.json_response(
                state.get(
                    "envelope",
                    {
                        "response": {
                            "result": "success",
                            "data": state["history"]
                            if command == "get_history"
                            else {"sessions": []},
                        }
                    },
                )
            )
        if request.path in ("/:/scrobble", "/:/unscrobble"):
            assert request.method == "PUT"
            assert query == {"key": "42", "identifier": "com.plexapp.plugins.library"}
            if state["mutate"]:
                watched = request.path == "/:/scrobble"
                state["metadata"]["viewCount"] = int(watched)
                if "leafCount" in state["metadata"]:
                    state["metadata"]["viewedLeafCount"] = (
                        state["metadata"]["leafCount"] if watched else 0
                    )
            return web.Response(text=state.get("receipt", ""), content_type="text/html")
        assert request.method == "GET"
        fixed = {
            "/status/sessions": state["sessions"],
            "/library/onDeck": state["on_deck"],
            "/library/recentlyAdded": state["recent"],
            "/library/sections": state["sections"],
            "/library/sections/1/genre": state["genres"],
            "/library/sections/2/genre": state["genres"],
            "/library/metadata/42": state.get("metadata_response", container([state["metadata"]])),
        }
        if request.path.endswith("/all"):
            start = int(query["X-Plex-Container-Start"])
            size = int(query["X-Plex-Container-Size"])
            values = state["candidates"]
            return web.json_response(container(values[start : start + size], totalSize=len(values)))
        return (
            web.json_response(fixed[request.path])
            if request.path in fixed
            else web.Response(status=404)
        )

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with running_app(app) as url:
        for name in ("plex", "tautulli"):
            config.backends[name]["url"] = url
        core = Core(config)
        await core.start()
        try:
            yield core, state, calls, path
        finally:
            await core.close()


async def test_plex_lists_progress_and_privacy(watch):
    core, state, calls, _ = watch
    result = await core.call("dot", "media_continue_watching", {})
    assert result["status"] == "ok"
    assert result["items"][0]["progress_percent"] == 50
    assert result["items"][1]["show"] == "A Show"
    assert result["items"][1]["season"] == 1
    assert result["items"][1]["episode"] == 2
    for row in result["items"]:
        for key in ("year", "season", "episode"):
            assert row[key] is None or type(row[key]) is int
    assert "PRIVATE" not in json.dumps(result)
    state["recent"] = container([movie(str(i)) for i in range(1, 24)], totalSize=100)
    result = await core.call("dot", "media_recently_added", {"limit": 20, "kind": "movie"})
    assert len(result["items"]) == 20 and result["truncated"]
    assert calls[-1][2]["type"] == "1"
    state["recent"] = {"MediaContainer": {"size": 0}}
    assert (await core.call("dot", "media_recently_added", {}))["items"] == []


async def test_history_users_window_and_kind(watch):
    core, state, calls, _ = watch
    state["history"]["data"] += [
        {**state["history"]["data"][0], "date": 1},
        {**state["history"]["data"][0], "media_type": "track"},
    ]
    result = await core.call(
        "dot", "media_watch_history", {"days": 90, "limit": 50, "kind": "movie"}
    )
    assert len(result["history"]) == 1
    assert result["history"][0]["user"] == "Other user"
    assert result["history"][0]["percent_complete"] == 93
    assert "PRIVATE" not in json.dumps(result)
    query = calls[-1][2]
    assert query["media_type"] == "movie" and query["length"] == "51"
    assert query["after"] and query["include_activity"] == "0"
    state["history"]["data"][0]["media_type"] = "episode"
    result = await core.call("dot", "media_watch_history", {"kind": "show", "limit": 1})
    assert result["history"][0]["kind"] == "show"
    assert result["truncated"]
    assert calls[-1][2]["media_type"] == "episode"


async def test_now_playing_identity_network_progress_and_privacy(watch):
    core, state, calls, _ = watch
    core.config.backends["tautulli"]["enabled"] = False
    result = await core.call("dot", "media_now_playing", {})
    session = result["sessions"][0]
    assert session == {
        "title": "Arrival",
        "user": "Viewer",
        "product": "Plex for Roku",
        "progress_percent": 42.5,
        "transcode": True,
        "rating_key": "42",
        "kind": "movie",
        "show": None,
        "season": None,
        "episode": None,
        "bandwidth_kbps": 12000,
        "location": "lan",
        "local": True,
    }
    assert calls == [("GET", "/status/sessions", {})]
    assert result["source"] == "plex"
    assert "PRIVATE" not in json.dumps(result)
    row = state["sessions"]["MediaContainer"]["Metadata"][0]
    row.update(
        type="episode",
        title="Pilot",
        grandparentTitle="A Show",
        parentIndex="18",
        index="6",
        parentRatingKey="100",
        grandparentRatingKey=99,
        viewOffset="1",
        duration="4",
        Session={"bandwidth": "3000", "location": "wan"},
        Player={"product": "Plex Web", "local": "0"},
        TranscodeSession={"videoDecision": "copy", "audioDecision": "copy"},
    )
    session = (await core.call("dot", "media_now_playing", {}))["sessions"][0]
    assert session["transcode"] is False and session["progress_percent"] == 25
    assert session["title"] == "A Show - Pilot"
    assert (session["show"], session["season"], session["episode"]) == ("A Show", 18, 6)
    assert type(session["season"]) is int and type(session["episode"]) is int
    assert (
        session["rating_key"],
        session["parent_rating_key"],
        session["grandparent_rating_key"],
    ) == ("42", "100", "99")
    assert session["bandwidth_kbps"] == 3000 and type(session["bandwidth_kbps"]) is int
    assert session["location"] == "wan" and session["local"] is False
    state["sessions"] = {"MediaContainer": {"size": 0}}
    assert (await core.call("dot", "media_now_playing", {}))["sessions"] == []


@pytest.mark.parametrize("kind,expected", [("track", "track"), ("clip", "other")])
async def test_now_playing_missing_optional_fields_and_bounds(watch, kind, expected):
    core, state, _, _ = watch
    state["sessions"] = container([{"title": "A Title", "type": kind}] * 21)
    result = await core.call("dot", "media_now_playing", {})
    assert len(result["sessions"]) == 20 and result["truncated"]
    session = result["sessions"][0]
    assert session["kind"] == expected
    assert "rating_key" not in session and "parent_rating_key" not in session
    for field in (
        "show",
        "season",
        "episode",
        "progress_percent",
        "bandwidth_kbps",
        "location",
        "local",
        "transcode",
    ):
        assert session[field] is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"MediaContainer": {"size": 1}},
        container([{"title": "A", "Player": []}]),
        container([{"title": "A", "ratingKey": "../42"}]),
    ],
)
async def test_now_playing_malformed_response(watch, payload):
    core, state, _, _ = watch
    state["sessions"] = payload
    result = await core.call("dot", "media_now_playing", {})
    assert result["error"]["code"] == "backend_invalid"


async def test_unwatched_pool_filters_paging_and_limits(watch):
    core, state, calls, _ = watch
    state["candidates"] = [movie(str(i), viewCount=1) for i in range(1, 101)] + [
        movie("101"),
        movie("102", year=1980),
        movie("103", duration=10000000),
        movie("104", Genre=[{"tag": "Horror"}]),
    ]
    result = await core.call(
        "dot",
        "media_find_unwatched",
        {"kind": "movie", "genre": "Drama", "max_minutes": 100, "min_year": 2000, "limit": 25},
    )
    assert result["status"] == "ok" and len(result["items"]) == 1
    candidate = result["items"][0]
    assert candidate["rating_key"] == "101" and candidate["rating"] == 8.1
    assert candidate["duration_minutes"] == 100
    assert candidate["genres"] == ["Drama", "Science Fiction"]
    assert not result["truncated"]
    scans = [c for c in calls if c[1].endswith("/all")]
    assert [c[2]["X-Plex-Container-Start"] for c in scans] == ["0", "100"]
    assert all(c[2]["unwatched"] == "1" and c[2]["genre"] == "9" for c in scans)
    state["candidates"] = [movie(str(i)) for i in range(1, 30)]
    result = await core.call("dot", "media_find_unwatched", {"kind": "movie", "limit": 25})
    assert len(result["items"]) == 25 and result["truncated"]
    assert "PRIVATE" not in json.dumps(result)


async def test_unwatched_shows_exclude_partial_and_scan_budget(watch):
    core, state, calls, _ = watch
    state["candidates"] = [
        movie(type="show", viewedLeafCount=1, leafCount=10),
        movie("43", type="show", viewedLeafCount=0, leafCount=10),
    ]
    result = await core.call("dot", "media_find_unwatched", {"kind": "show"})
    assert [r["rating_key"] for r in result["items"]] == ["43"]
    assert calls[-1][1] == "/library/sections/2/all"
    assert calls[-1][2]["type"] == "2"
    state["candidates"] = [movie(str(i), viewCount=1) for i in range(1, 1100)]
    calls.clear()
    result = await core.call("dot", "media_find_unwatched", {"kind": "movie"})
    assert result["items"] == [] and result["truncated"]
    assert len([c for c in calls if c[1].endswith("/all")]) == 10


@pytest.mark.parametrize("name,args", READS + WRITES)
async def test_watch_hidden_scope_and_missing_policy(watch, name, args):
    core, _, calls, _ = watch
    assert name not in [t["name"] for t in core.tools("agy")]
    assert (await core.call("agy", name, args))["error"]["code"] == "scope_denied"
    assert not calls
    assert core.audit.tail()[-1]["decision"] == "denied"
    core.policy.path.unlink()
    assert (await core.call("owner", name, args))["error"]["code"] == "policy_invalid"
    assert not calls


@pytest.mark.parametrize(
    "name,args",
    [
        ("media_recently_added", {"limit": 21}),
        ("media_recently_added", {"kind": "book"}),
        ("media_watch_history", {"days": 91}),
        ("media_watch_history", {"days": 0}),
        ("media_watch_history", {"limit": 51}),
        ("media_find_unwatched", {"kind": "movie", "limit": 26}),
        ("media_find_unwatched", {"kind": "movie", "max_minutes": True}),
        ("media_find_unwatched", {"kind": "movie", "genre": " "}),
        ("media_find_unwatched", {}),
        ("media_continue_watching", {"url": "PRIVATE"}),
        ("media_mark_watched", {"rating_key": "../42"}),
        ("media_mark_unwatched", {"rating_key": "42&key=99"}),
        ("media_mark_watched", {"rating_key": "４２"}),
        ("media_mark_watched", {"rating_key": "0"}),
        ("media_mark_watched", {"rating_key": 42}),
    ],
)
async def test_watch_invalid_inputs_before_io(watch, name, args):
    core, _, calls, _ = watch
    assert (await core.call("dot", name, args))["error"]["code"] == "invalid_arguments"
    assert not calls


async def test_mark_watched_and_unwatched_empty_receipt_readback(watch):
    core, state, calls, _ = watch
    result = await core.call("dot", "media_mark_watched", {"rating_key": "42"})
    assert result["status"] == "ok" and result["watched"] and result["changed"]
    assert [c[0] for c in calls] == ["GET", "PUT", "GET"]
    calls.clear()
    assert not (await core.call("dot", "media_mark_watched", {"rating_key": "42"}))["changed"]
    assert all(c[0] == "GET" for c in calls)
    state["receipt"] = "<html>PRIVATE</html>"
    result = await core.call("dot", "media_mark_unwatched", {"rating_key": "42"})
    assert result["status"] == "ok" and result["watched"] is False
    assert "PRIVATE" not in json.dumps(result)
    assert all("fake-" not in json.dumps(r) for r in core.audit.tail())
    assert "fake-" not in core.audit.path.read_text()


async def test_mark_concurrent_and_containers(watch):
    core, state, calls, _ = watch
    state["metadata"].update(type="season", leafCount=10, viewedLeafCount=1)
    results = await asyncio.gather(
        *(core.call("dot", "media_mark_watched", {"rating_key": "42"}) for _ in range(2))
    )
    assert all(r["watched"] for r in results)
    assert len([c for c in calls if c[0] == "PUT"]) == 1
    result = await core.call("dot", "media_mark_unwatched", {"rating_key": "42"})
    assert result["status"] == "ok" and state["metadata"]["viewedLeafCount"] == 0


async def test_mark_allowlist_deny_and_revocation(watch):
    core, state, calls, _ = watch
    change_policy(core.config, lambda p: p["allowlists"]["plex"].update(rating_keys=[]))
    assert (await core.call("dot", "media_mark_watched", {"rating_key": "42"}))["error"][
        "code"
    ] == "resource_denied"
    assert not calls  # media.allow_all cannot grant Plex writes.
    change_policy(
        core.config,
        lambda p: p["allowlists"]["plex"].update(rating_keys=["42"], deny_rating_keys=["42"]),
    )
    assert (await core.call("dot", "media_mark_watched", {"rating_key": "42"}))["error"][
        "code"
    ] == "resource_denied"
    change_policy(core.config, lambda p: p["allowlists"]["plex"].update(deny_rating_keys=[]))
    original = core.backends.get

    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        change_policy(core.config, lambda p: p["allowlists"]["plex"].update(rating_keys=[]))
        return result

    core.backends.get = revoke
    assert (await core.call("dot", "media_mark_watched", {"rating_key": "42"}))["error"][
        "code"
    ] == "resource_denied"
    assert not any(c[0] == "PUT" for c in calls)


@pytest.mark.parametrize(
    "value",
    [
        container([]),
        container([movie("43")]),
        container([movie(type="track")]),
        [],
        {"MediaContainer": {"size": 1}},
    ],
)
async def test_mark_missing_mismatched_or_unsupported_key(watch, value):
    core, state, calls, _ = watch
    state["metadata_response"] = value
    assert (await core.call("dot", "media_mark_watched", {"rating_key": "42"}))["status"] == "error"
    assert not any(c[0] == "PUT" for c in calls)


async def test_mark_unverified_state_is_uncertain(watch):
    core, state, calls, _ = watch
    state["mutate"] = False
    assert (await core.call("dot", "media_mark_watched", {"rating_key": "42"}))["error"][
        "code"
    ] == "watch_state_uncertain"
    assert len([c for c in calls if c[0] == "PUT"]) == 1


@pytest.mark.parametrize("name,field", [("plex", "token_file"), ("tautulli", "api_key_file")])
@pytest.mark.parametrize("mode", ["missing", "public", "symlink", "empty", "directory"])
async def test_watch_credentials_private_and_required(watch, name, field, mode):
    core, _, calls, _ = watch
    path = Path(core.config.backends[name][field])
    if mode == "public":
        await asyncio.to_thread(path.chmod, 0o644)
    elif mode == "empty":
        await asyncio.to_thread(path.write_text, "")
    else:
        await asyncio.to_thread(path.unlink)
        if mode == "symlink":
            await asyncio.to_thread(path.symlink_to, core.config.config_dir / "policy.yaml")
        elif mode == "directory":
            await asyncio.to_thread(path.mkdir)
    cap = "media_continue_watching" if name == "plex" else "media_watch_history"
    result = await core.call("dot", cap, {})
    assert result["error"]["code"] == "backend_credentials"
    assert not calls


@pytest.mark.parametrize("name,args", READS[:5] + WRITES)
async def test_watch_backend_error_sanitized(watch, name, args):
    core, state, _, _ = watch
    state["http_status"] = 502
    result = await core.call("dot", name, args)
    assert result["error"]["code"] == "backend_error"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "envelope",
    [
        [],
        {"response": []},
        {"response": {"result": "error", "message": "PRIVATE"}},
        {"response": {"result": "success", "data": []}},
        {"response": {"result": "success", "data": {"data": [None]}}},
    ],
)
async def test_tautulli_invalid_envelopes_sanitized(watch, envelope):
    core, state, _, _ = watch
    state["envelope"] = envelope
    result = await core.call("dot", "media_watch_history", {})
    assert result["status"] == "error"
    assert result["error"]["code"] in ("backend_invalid", "backend_error")
    assert "PRIVATE" not in json.dumps(result)


async def test_maintenance_compact_private_and_stale(watch):
    core, _, calls, path = watch
    result = await core.call("dot", "media_maintenance_status", {})
    assert result["status"] == "ok" and not result["stale"]
    assert result["queues"] == {"requests": 1, "compatibility": 3, "compression": 2}
    assert result["active_work"] == [
        {"lane": "compatibility", "last_progress_at": 100},
        {"lane": "compression", "last_progress_at": None},
    ]
    assert result["pending_installs"] == 1
    assert result["savings"] == {
        "freed_all_time_bytes": None,
        "jobs_all_time": None,
        "since": None,
        "freed_last_7_days_bytes": None,
        "jobs_last_7_days": None,
        "freed_last_30_days_bytes": None,
        "jobs_last_30_days": None,
        "queued_estimate_bytes": None,
        "reclaimable_estimate_bytes": None,
    }
    assert result["attention_items"] == [{"title": "Arrival", "stage": "repair"}]
    assert "PRIVATE" not in json.dumps(result)
    assert not calls
    path.write_text(
        json.dumps(
            snapshot(
                generatedAt=time.time() - 2 * 3600 - 60,
                items=[{"title": "Blocked", "stage": "repair", "blocker": "PRIVATE"}] * 12,
            )
        )
    )
    result = await core.call("dot", "media_maintenance_status", {})
    assert result["status"] == "degraded" and result["stale"]
    assert (
        result["attention_total"] == 12
        and len(result["attention_items"]) == 10
        and result["truncated"]
    )


@pytest.mark.parametrize("all_time_bytes", [22448121700562, 0, -100])
@pytest.mark.parametrize("since", [1772844376.0, 0, None])
async def test_maintenance_savings_exact_and_private(watch, all_time_bytes, since):
    core, _, calls, path = watch
    path.write_text(
        json.dumps(
            snapshot(
                savingsAllTime={"bytes": all_time_bytes, "jobs": 12603, "since": since},
                savingsLast30DaysBytes=3000,
                completedLast30Days=30,
                queuedSavingsEstimateBytes=4000,
                reclaimable={
                    "last_7_days": {"bytes": 700, "jobs": 7, "jobs_per_day": 1.0},
                    "last_30_days": {"bytes": 9999, "jobs": 99, "jobs_per_day": 3.3},
                    "measured_bytes": 5000,
                    "estimated_only_bytes": 6000,
                    "extra": {"path": "/PRIVATE/media", "token": "PRIVATE"},
                },
            )
        )
    )
    result = await core.call("dot", "media_maintenance_status", {})
    assert result["status"] == "ok"
    # The autouse conformance fixture validates the advertised output schema too.
    assert result["savings"] == {
        "freed_all_time_bytes": all_time_bytes,
        "jobs_all_time": 12603,
        "since": since,
        "freed_last_7_days_bytes": 700,
        "jobs_last_7_days": 7,
        "freed_last_30_days_bytes": 3000,
        "jobs_last_30_days": 30,
        "queued_estimate_bytes": 4000,
        "reclaimable_estimate_bytes": 11000,
    }
    assert "PRIVATE" not in json.dumps(result)
    assert not calls


@pytest.mark.parametrize(
    ("changes", "known"),
    [
        ({"savingsAllTime": {}, "reclaimable": {}}, {}),
        ({"savingsAllTime": {"jobs": 0}}, {"jobs_all_time": 0}),
        ({"reclaimable": {"last_7_days": {"bytes": 0}}}, {"freed_last_7_days_bytes": 0}),
        ({"reclaimable": {"measured_bytes": 10}}, {}),
        ({"reclaimable": {"estimated_only_bytes": 20}}, {}),
        (
            {"reclaimable": {"measured_bytes": 0, "estimated_only_bytes": 0}},
            {"reclaimable_estimate_bytes": 0},
        ),
        ({"reclaimable": {"last_30_days": "PRIVATE", "unknown": False}}, {}),
    ],
)
async def test_maintenance_partial_savings(watch, changes, known):
    core, _, _, path = watch
    path.write_text(json.dumps(snapshot(**changes)))
    result = await core.call("dot", "media_maintenance_status", {})
    assert result["status"] == "ok"
    assert {key: value for key, value in result["savings"].items() if value is not None} == known


SAVINGS_INTEGER_PATHS = [
    ("savingsAllTime", "bytes"),
    ("savingsAllTime", "jobs"),
    ("savingsLast30DaysBytes",),
    ("completedLast30Days",),
    ("queuedSavingsEstimateBytes",),
    ("reclaimable", "last_7_days", "bytes"),
    ("reclaimable", "last_7_days", "jobs"),
    ("reclaimable", "measured_bytes"),
    ("reclaimable", "estimated_only_bytes"),
]


@pytest.mark.parametrize("field_path", [*SAVINGS_INTEGER_PATHS, ("savingsAllTime", "since")])
@pytest.mark.parametrize("value", [True, "PRIVATE", float("nan"), float("inf"), -float("inf")])
async def test_maintenance_invalid_savings_numbers(watch, field_path, value):
    await assert_invalid_savings(watch, field_path, value)


@pytest.mark.parametrize("field_path", SAVINGS_INTEGER_PATHS)
@pytest.mark.parametrize("value", [None, 1.5])
async def test_maintenance_savings_require_integers(watch, field_path, value):
    await assert_invalid_savings(watch, field_path, value)


@pytest.mark.parametrize("field_path", [*SAVINGS_INTEGER_PATHS[1:], ("savingsAllTime", "since")])
async def test_maintenance_savings_require_nonnegative(watch, field_path):
    await assert_invalid_savings(watch, field_path, -1)


@pytest.mark.parametrize(
    "field_path", [("savingsAllTime",), ("reclaimable",), ("reclaimable", "last_7_days")]
)
@pytest.mark.parametrize("value", [None, False, "PRIVATE", [], 1])
async def test_maintenance_invalid_savings_objects(watch, field_path, value):
    await assert_invalid_savings(watch, field_path, value)


async def assert_invalid_savings(watch, field_path, value):
    core, _, _, path = watch
    data = snapshot()
    parent = data
    for key in field_path[:-1]:
        parent = parent.setdefault(key, {})
    parent[field_path[-1]] = value
    path.write_text(json.dumps(data))
    result = await core.call("dot", "media_maintenance_status", {})
    assert result["error"]["code"] == "backend_invalid"
    assert "PRIVATE" not in json.dumps(result)


async def test_maintenance_reads_snapshot_over_one_mib(watch):
    core, _, _, path = watch
    items = [{"title": f"Item {n}", "stage": "ready", "blocker": None} for n in range(30000)]
    path.write_text(json.dumps(snapshot(items=items)))
    assert path.stat().st_size > 1024 * 1024
    result = await core.call("dot", "media_maintenance_status", {})
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    "mode", ["missing", "public", "symlink", "directory", "invalid_json", "too_large"]
)
async def test_maintenance_refuses_unsafe_file(watch, mode):
    core, _, _, path = watch
    if mode == "public":
        await asyncio.to_thread(path.chmod, 0o644)
    elif mode == "invalid_json":
        path.write_text("PRIVATE")
    elif mode == "too_large":
        path.write_text("x" * (16 * 1024 * 1024 + 1))
    else:
        await asyncio.to_thread(path.unlink)
        if mode == "directory":
            await asyncio.to_thread(path.mkdir)
        elif mode == "symlink":
            await asyncio.to_thread(path.symlink_to, core.config.config_dir / "policy.yaml")
    result = await core.call("dot", "media_maintenance_status", {})
    assert result["error"]["code"] == "backend_invalid"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"generatedAt": float("nan")},
        {"generatedAt": time.time() + 10000},
        {"lanes": []},
        {"items": [False]},
        {"compressionInstallPending": "PRIVATE"},
        {"repairHolds": -1},
    ],
)
def test_maintenance_invalid_contract(tmp_path, changes):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(snapshot(**changes)))
    path.chmod(0o600)
    with pytest.raises(CapabilityError, match="maintenance:"):
        read_status(path)


async def test_watch_doctor_and_disabled(watch):
    core, _, _, _ = watch
    for name in ("plex", "tautulli", "maintenance"):
        assert (await core.backends.probe(name))["status"] == "ok"
        core.config.backends[name]["enabled"] = False
        assert (await core.backends.probe(name))["status"] == "disabled"
    for name, args in READS + WRITES:
        assert (await core.call("dot", name, args))["error"]["code"] == "backend_disabled"


def test_watch_config_and_policy_example(config):
    root = Path(__file__).resolve().parents[1]
    config_path = config.config_dir / "config.yaml"
    config_path.write_text((root / "config/config.example.yaml").read_text())
    loaded = Config.load()
    assert loaded.backends["plex"]["url"] == "http://127.0.0.1:32400"
    assert loaded.backends["tautulli"]["api_key_file"].endswith("tautulli.key")
    policy = yaml.safe_load((root / "config/policy.example.yaml").read_text())
    assert all(
        "media:watch_state" in c["scopes"]
        for client, c in policy["clients"].items()
        if client not in ("cortex", "deepseek")
    )
    assert "media:watch_state" not in policy["clients"]["cortex"]["scopes"]
    assert policy["allowlists"]["plex"]["rating_keys"] == []
    assert policy["allowlists"]["plex"]["deny_rating_keys"] == []
    for settings in ({"path": "relative.json"}, {"path": 1}, {"url": "http://127.0.0.1:1234"}):
        config_path.write_text(yaml.safe_dump({"backends": {"maintenance": settings}}))
        with pytest.raises(ValueError):
            Config.load()


@pytest.mark.parametrize("value", [None, True, [], {}, "no", "NaN", "inf", -1, 10**1000])
def test_bad_optional_numeric_metadata(value):
    assert number(value) is None


def test_plex_allow_all_admits_any_key_but_deny_wins(core):
    import pytest

    from switchboard.errors import CapabilityError

    change_policy(
        core.config,
        lambda p: p["allowlists"].update(
            plex={"allow_all": True, "rating_keys": [], "deny_rating_keys": ["13"]}
        ),
    )
    assert core.policy.require_allowed("plex", "rating_keys", "999") is None
    with pytest.raises(CapabilityError):
        core.policy.require_allowed("plex", "rating_keys", "13")


@pytest.mark.parametrize(
    "name,args",
    [
        ("media_continue_watching", {}),
        ("media_recently_added", {}),
        ("media_find_unwatched", {"kind": "movie"}),
    ],
)
async def test_watch_year_season_episode_never_serialize_as_floats(watch, name, args):
    core, _, _, _ = watch
    result = await core.call("dot", name, args)
    assert result["items"]
    for row in result["items"]:
        for field in ("year", "season", "episode"):
            assert row[field] is None or type(row[field]) is int
