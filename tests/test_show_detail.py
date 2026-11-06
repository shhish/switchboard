"""Show completeness uses only ephemeral fake Seerr, Plex and library GETs."""

import copy
import json
from datetime import UTC, datetime

import pytest
from aiohttp import web

from switchboard.core import Core

from .conftest import change_policy, running_app


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        # Nov 5 UTC is still Nov 4 in New York.
        return datetime(2026, 11, 5, 2, tzinfo=UTC).astimezone(tz)


def container(values):
    return {"MediaContainer": {"Metadata": values, "size": len(values)}}


def season(number, count=8, air="2026-10-01"):
    return {"seasonNumber": number, "episodeCount": count, "airDate": air}


def episodes(count=8):
    return {
        "episodes": [
            {
                "episodeNumber": n,
                "airDate": "2026-11-04" if n <= 6 else "2026-11-05" if n == 7 else None,
                "name": "PRIVATE",
            }
            for n in range(1, count + 1)
        ]
    }


@pytest.fixture
async def shows(config, fake_backends, monkeypatch):
    monkeypatch.setattr("switchboard.domains.media.datetime", FixedDateTime)
    token = config.config_dir / "plex.secret"
    token.write_text("fake-plex-secret")
    token.chmod(0o600)
    config.backends["plex"].update(enabled=True, token_file=str(token))
    state = {
        "/api/v1/tv/901": {
            "id": 901,
            "name": "Dune Prophecy",
            "firstAirDate": "2024-01-01",
            "status": "Returning Series",
            "seasons": [season(0, 1), season(17, 10, "2025-01-01"), season(18)],
            "lastEpisodeToAir": {"seasonNumber": 18, "episodeNumber": 6, "airDate": "2026-11-04"},
            "nextEpisodeToAir": {"seasonNumber": 18, "episodeNumber": 7, "airDate": "2026-11-05"},
            "mediaInfo": {
                "status": 5,
                "ratingKey": "100",
                "seasons": [{"seasonNumber": 18, "status": 5}],
            },
            "private": "PRIVATE",
        },
        "/api/v1/tv/901/season/18": episodes(),
        "/api/v1/tv/901/season/17": episodes(10),
        "/api/v1/tv/901/season/0": {"episodes": [{"episodeNumber": 1, "airDate": "2025-01-01"}]},
        "/library/metadata/100/children": container(
            [
                {"index": 17, "leafCount": 10, "viewedLeafCount": 10, "ratingKey": "117"},
                {"index": 18, "leafCount": 6, "viewedLeafCount": 5, "ratingKey": "118"},
            ]
        ),
        "/library/metadata/118/children": container(
            [{"index": n, "title": "PRIVATE", "viewCount": int(n <= 5)} for n in range(1, 7)]
        ),
    }
    calls = []

    async def handle(request):
        assert request.method == "GET"
        if request.path.startswith("/api/v1/"):
            assert request.headers["X-Api-Key"] == "fake-overseerr-key"
            assert "X-Plex-Token" not in request.headers
        else:
            assert request.headers["X-Plex-Token"] == "fake-plex-secret"
            assert request.headers["Accept"] == "application/json"
            assert "X-Api-Key" not in request.headers
        calls.append(request.path)
        value = state.get(request.path, 404)
        if isinstance(value, int):
            return web.Response(status=value, text="PRIVATE")
        return web.json_response(value)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with running_app(app) as url:
        for backend in ("plex", "overseerr"):
            config.backends[backend]["url"] = url
        # Search stays with the shared library/Seerr fixture.
        state["/api/v1/search"] = fake_backends[0]["/api/v1/search"]
        core = Core(config)
        await core.start()
        try:
            yield core, state, calls
        finally:
            await core.close()


async def test_show_detail_latest_aired_complete_and_bounded_gets(shows):
    core, _, calls = shows
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["outcome"] == "ok" and not result["truncated"]
    assert result["show"] == {
        "title": "Dune Prophecy",
        "year": 2024,
        "tmdb_id": 901,
        "status": "Returning Series",
        "in_library": True,
    }
    assert result["seasons"] == [
        {
            "season": 17,
            "episodes_planned": 10,
            "aired": 10,
            "in_library": 10,
            "watched": 10,
            "complete": True,
            "missing_aired": [],
        },
        {
            "season": 18,
            "episodes_planned": 8,
            "aired": 6,
            "in_library": 6,
            "watched": 5,
            "complete": True,
            "missing_aired": [],
        },
    ]
    assert result["latest_season_complete"] is True
    assert result["last_aired"] == {"season": 18, "episode": 6, "date": "2026-11-04"}
    assert result["next_airing"] == {"season": 18, "episode": 7, "date": "2026-11-05"}
    assert (
        result["summary"]
        == "Season 18: all 6 aired episodes are in Plex (5 watched); next episode Nov 5."
    )
    assert calls == [
        "/api/v1/tv/901",
        "/library/metadata/100/children",
        "/api/v1/tv/901/season/18",
        "/library/metadata/118/children",
    ]
    assert "PRIVATE" not in json.dumps(result)


async def test_show_detail_missing_episode_ignores_seerr_availability(shows):
    core, state, _ = shows
    state["/library/metadata/100/children"]["MediaContainer"]["Metadata"][1]["leafCount"] = 5
    state["/library/metadata/118/children"]["MediaContainer"]["Metadata"].pop()
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["latest_season_complete"] is False
    assert result["seasons"][1]["missing_aired"] == [6]
    assert result["seasons"][1]["complete"] is False


async def test_show_detail_no_plex_key_and_specials(shows):
    core, state, calls = shows
    state["/api/v1/tv/901"]["mediaInfo"] = None
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901, "include_specials": True})
    assert not result["show"]["in_library"]
    assert all(s["in_library"] == 0 and s["watched"] == 0 for s in result["seasons"])
    assert result["seasons"][0]["season"] == 0
    assert result["seasons"][0]["missing_aired"] == [1]
    assert result["seasons"][-1]["missing_aired"] == list(range(1, 7))
    assert not any(p.startswith("/library/") for p in calls)


async def test_show_detail_title_resolution_uses_library_and_enrichment(shows):
    core, _, _ = shows
    result = await core.call("dot", "media_show_detail", {"title": "DUNE"})
    assert result["show"]["tmdb_id"] == 901


async def test_show_detail_ambiguity_and_not_found(shows, fake_backends):
    core, _, calls = shows
    source = fake_backends[0]["/shows/list"]["shows"]
    source.append({"id": 71, "title": "Dune Another", "tmdb_id": 902})
    result = await core.call("dot", "media_show_detail", {"title": "Dune"})
    assert result["outcome"] == "ambiguous" and len(result["candidates"]) == 2
    assert not any(p.startswith("/api/v1/tv/") for p in calls)
    result = await core.call("dot", "media_show_detail", {"title": "Absent"})
    assert result["outcome"] == "not_found"
    assert not any(p.startswith("/api/v1/tv/") for p in calls)


async def test_show_detail_title_truncation_never_selects_arbitrary_match(shows, fake_backends):
    core, _, calls = shows
    fake_backends[0]["/shows/list"]["shows"] = [
        {"title": f"A Show {n}", "tmdb_id": n + 1} for n in range(11)
    ]
    result = await core.call("dot", "media_show_detail", {"title": "A Show"})
    assert result["outcome"] == "ambiguous" and result["truncated"]
    assert len(result["candidates"]) == 10
    assert not any(p.startswith("/api/v1/tv/") for p in calls)


async def test_show_detail_season_budget_latest_first(shows):
    core, state, calls = shows
    state["/api/v1/tv/901"]["seasons"] = [season(n) for n in range(1, 10)]
    state["/api/v1/tv/901"]["mediaInfo"] = None
    for n in range(1, 10):
        state[f"/api/v1/tv/901/season/{n}"] = episodes()
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["truncated"]
    assert [int(p.rsplit("/", 1)[1]) for p in calls if "/season/" in p] == [9, 8, 7, 6, 5, 4]
    assert [s["season"] for s in result["seasons"]] == list(range(4, 10))
    assert all(type(s["aired"]) is int and type(s["complete"]) is bool for s in result["seasons"])
    assert result["seasons"][-1]["aired"] == 6


async def test_show_detail_missing_list_bound_and_no_next_episode(shows):
    core, state, _ = shows
    data = state["/api/v1/tv/901"]
    data.update(seasons=[season(18, 70)], mediaInfo=None, nextEpisodeToAir=None, status="Ended")
    state["/api/v1/tv/901/season/18"] = {
        "episodes": [{"episodeNumber": n, "airDate": "2025-01-01"} for n in range(1, 71)]
    }
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["seasons"][0]["aired"] == 70
    assert result["seasons"][0]["missing_aired"] == list(range(1, 61))
    assert result["next_airing"] is None and result["truncated"]
    assert result["show"]["status"] == "Ended"


async def test_show_detail_future_season_no_aired_episodes(shows):
    core, state, _ = shows
    state["/api/v1/tv/901"].update(
        seasons=[season(19, 1, "2027-01-01")], lastEpisodeToAir=None, nextEpisodeToAir=None
    )
    state["/api/v1/tv/901/season/19"] = {
        "episodes": [{"episodeNumber": 1, "airDate": "2027-01-01"}]
    }
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["seasons"][0]["aired"] == 0
    assert result["latest_season_complete"] is True
    assert result["last_aired"] is None
    assert result["summary"] == "Season 19: no episodes have aired yet."


@pytest.mark.parametrize("backend", ["plex", "overseerr"])
@pytest.mark.parametrize("failure", ["disabled", "credentials", "down", "http_error"])
async def test_show_detail_dependencies_unavailable_with_hint(shows, backend, failure):
    core, state, calls = shows
    if failure == "disabled":
        core.config.backends[backend]["enabled"] = False
    elif failure == "credentials":
        core.config.backends[backend].pop("token_file" if backend == "plex" else "api_key_file")
    elif failure == "down":
        async with running_app(web.Application()) as dead_url:
            pass
        core.config.backends[backend]["url"] = dead_url
    else:
        state["/api/v1/tv/901" if backend == "overseerr" else "/library/metadata/100/children"] = (
            502
        )
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["outcome"] == "unavailable"
    assert result["error"]["retryable"] and result["error"]["hint"]
    assert "PRIVATE" not in json.dumps(result)
    if failure in ("disabled", "credentials"):
        assert not calls


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"tmdb_id": 901, "title": "Dune"},
        {"tmdb_id": True},
        {"tmdb_id": 0},
        {"title": " "},
        {"title": ""},
        {"tmdb_id": 901, "include_specials": 1},
        {"tmdb_id": 901, "url": "PRIVATE"},
    ],
)
async def test_show_detail_invalid_arguments_before_io(shows, args):
    core, _, calls = shows
    assert (await core.call("dot", "media_show_detail", args))["error"][
        "code"
    ] == "invalid_arguments"
    assert not calls


async def test_show_detail_denial_hidden_and_missing_policy(shows):
    core, _, calls = shows
    assert "media_show_detail" not in [t["name"] for t in core.tools("agy")]
    result = await core.call("agy", "media_show_detail", {"tmdb_id": 901})
    assert result["outcome"] == "blocked" and not calls
    assert core.audit.tail()[-1]["decision"] == "denied"
    change_policy(core.config, lambda p: p["clients"]["dot"].update(scopes=[]))
    assert (await core.call("dot", "media_show_detail", {"tmdb_id": 901}))["error"][
        "code"
    ] == "scope_denied"
    core.policy.path.unlink()
    assert (await core.call("owner", "media_show_detail", {"tmdb_id": 901}))["error"][
        "code"
    ] == "policy_invalid"
    assert not calls


@pytest.mark.parametrize(
    "path,value",
    [
        ("/api/v1/tv/901", []),
        ("/api/v1/tv/901/season/18", {"episodes": [None]}),
        ("/api/v1/tv/901/season/18", {"episodes": [{"episodeNumber": 1, "airDate": "PRIVATE"}]}),
        ("/library/metadata/100/children", {"MediaContainer": {"size": 1}}),
        ("/library/metadata/100/children", container([{"index": 18, "leafCount": "PRIVATE"}])),
        ("/library/metadata/118/children", container([{"index": "PRIVATE"}])),
    ],
)
async def test_show_detail_malformed_contracts(shows, path, value):
    core, state, _ = shows
    state[path] = copy.deepcopy(value)
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["error"]["code"] == "backend_invalid"
    assert "PRIVATE" not in json.dumps(result)


async def test_show_detail_count_complete_latest_still_checks_dates(shows):
    core, state, calls = shows
    state["/library/metadata/100/children"]["MediaContainer"]["Metadata"][1]["leafCount"] = 8
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["seasons"][-1]["aired"] == 6
    assert "/api/v1/tv/901/season/18" in calls
    assert "/library/metadata/118/children" in calls


async def test_show_detail_empty_seasons(shows):
    core, state, _ = shows
    state["/api/v1/tv/901"].update(seasons=[], lastEpisodeToAir=None, nextEpisodeToAir=None)
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert result["seasons"] == [] and result["latest_season_complete"] is False
    assert result["last_aired"] is None and result["next_airing"] is None


async def test_show_detail_library_failure_is_unavailable(shows, fake_backends):
    core, _, _ = shows
    fake_backends[0]["/shows/list"] = 502
    result = await core.call("dot", "media_show_detail", {"title": "Dune"})
    assert result["outcome"] == "unavailable" and result["error"]["hint"]


async def test_show_detail_single_library_match_ignores_seerr_search_truncation(shows):
    core, state, _ = shows
    state["/api/v1/search"]["results"] += [
        {"id": n, "mediaType": "tv", "name": f"Another {n}", "firstAirDate": "2025-01-01"}
        for n in range(100, 120)
    ]
    result = await core.call("dot", "media_show_detail", {"title": "Dune"})
    assert result["outcome"] == "ok" and result["show"]["tmdb_id"] == 901


async def test_show_detail_latest_ignores_announced_unaired_season(shows):
    """An announced season with nothing aired must not make the show look caught up."""
    core, state, _ = shows
    data = state["/api/v1/tv/901"]
    data["seasons"] = [*data["seasons"], season(19, 10, "2027-03-01")]
    state["/api/v1/tv/901/season/19"] = {
        "episodes": [{"episodeNumber": 1, "airDate": "2027-03-01"}]
    }
    result = await core.call("dot", "media_show_detail", {"tmdb_id": 901})
    assert "Season 18" in result["summary"] and "Season 19" not in result["summary"]


async def test_show_detail_unique_exact_title_wins(shows, fake_backends):
    core, source, _ = shows
    source["/api/v1/tv/901"]["name"] = "Shark Tank"
    fake_backends[0]["/shows/list"]["shows"] = [
        {"title": "Shark Tank India", "tmdb_id": 902},
        {"title": "Shark Tank", "tmdb_id": 901},
    ]
    result = await core.call("dot", "media_show_detail", {"title": "  sHaRk TaNk  "})
    assert result["show"]["tmdb_id"] == 901
    # Duplicate exact titles remain ambiguous.
    fake_backends[0]["/shows/list"]["shows"].append({"title": "SHARK TANK", "tmdb_id": 903})
    result = await core.call("dot", "media_show_detail", {"title": "Shark Tank"})
    assert result["outcome"] == "ambiguous"


async def test_show_detail_exact_match_beyond_public_search_cap(shows, fake_backends):
    core, _, _ = shows
    fake_backends[0]["/shows/list"]["shows"] = [
        {"title": f"Dune Prophecy {n}", "tmdb_id": n + 1} for n in range(11)
    ] + [{"title": "Dune Prophecy", "tmdb_id": 901}]
    result = await core.call("dot", "media_show_detail", {"title": "Dune Prophecy"})
    assert result["show"]["tmdb_id"] == 901
