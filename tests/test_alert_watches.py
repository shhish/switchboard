"""One-off watches use fake backends and a controlled clock, never live delivery."""

import json
from types import SimpleNamespace

import pytest

from switchboard.storage import Storage

from .conftest import change_policy
from .test_show_detail import shows as shows


@pytest.fixture
def watch_clock(monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr("switchboard.watches.time", SimpleNamespace(time=lambda: clock[0]))
    return clock


async def notifications(core):
    await core.alerts.close()
    core.config.alerts = {"ntfy": {}, "email": {}}


def notices(core):
    return [dict(row) for row in core.storage.db.execute("SELECT * FROM alert_outbox")]


async def add(core, args, client="dot"):
    result = await core.call(client, "alerts_watch_add", args)
    assert result["outcome"] == "ok", result
    return result["watch_id"]


async def test_movie_single_notification_per_channel_and_retention(
    core, fake_backends, watch_clock
):
    await notifications(core)
    source = fake_backends[0]["/movies/list"]["movies"][0]
    source["has_file"] = False
    ident = await add(
        core, {"kind": "media_available", "tmdb_id": 100, "media": "movie", "note": "Movie night"}
    )
    await core.watches.evaluate()
    assert not notices(core)
    source["has_file"] = True
    watch_clock[0] += 599
    await core.watches.evaluate()
    assert not notices(core)
    watch_clock[0] += 1
    await core.watches.evaluate()
    rows = notices(core)
    assert len(rows) == 2 and {row["channel"] for row in rows} == {"ntfy", "email"}
    assert len({row["dedupe_key"] for row in rows}) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["title"] == "[Dune 0] [✅ Available]"
    assert payload["body"] == "The movie is now in Plex.\nMovie night"
    await core.watches.evaluate(watch_clock[0] + 600)
    assert len(notices(core)) == 2
    listed = (await core.call("dot", "alerts_watch_list", {}))["watches"]
    assert listed[0]["watch_id"] == ident and listed[0]["fired_at"] == watch_clock[0]
    watch_clock[0] += 7 * 86400
    assert not (await core.call("dot", "alerts_watch_list", {}))["watches"]


async def test_watch_ownership_limit_and_expiry(core, watch_clock):
    args = {"kind": "download_stalled", "title_contains": "Dune", "expires_days": 1}
    ident = await add(core, args)
    assert not (await core.call("codex", "alerts_watch_list", {}))["watches"]
    assert (await core.call("codex", "alerts_watch_remove", {"watch_id": ident}))[
        "outcome"
    ] == "not_found"
    assert len((await core.call("owner", "alerts_watch_list", {}))["watches"]) == 1
    for _ in range(19):
        await add(core, args)
    assert (await core.call("dot", "alerts_watch_add", args))["error"]["code"] == "watch_limit"
    await add(core, args, "codex")
    assert (await core.call("owner", "alerts_watch_remove", {"watch_id": ident}))["status"] == "ok"
    await add(core, args)
    watch_clock[0] += 86400
    await core.watches.evaluate()
    assert not (await core.call("owner", "alerts_watch_list", {}))["watches"]
    assert not notices(core)


async def test_download_progress_individual_items_and_missing_reset(
    core, fake_backends, watch_clock
):
    await notifications(core)
    source = fake_backends[0]["/downloads/active"]["downloads"]
    source.append({"id": "dl3", "title": "Dune second", "progress": 1, "status": "downloading"})
    await add(core, {"kind": "download_stalled", "title_contains": "dUnE"})
    await core.watches.evaluate()
    for _ in range(3):
        watch_clock[0] += 600
        source[0]["progress"] += 1
        await core.watches.evaluate()
    assert len(notices(core)) == 2
    assert (
        json.loads(notices(core)[0]["payload"])["body"]
        == "Dune second hasn't progressed for 30 min."
    )
    assert not any(row["fired_at"] is None for row in (await core.watches.list("dot"))["watches"])


@pytest.mark.parametrize("status", ["stalled", "warning", "FAILED"])
async def test_download_source_status_fires_immediately(core, fake_backends, watch_clock, status):
    await notifications(core)
    fake_backends[0]["/downloads/active"]["downloads"][0]["status"] = status
    await add(core, {"kind": "download_stalled", "title_contains": "Dune"})
    # An explicit watch bypasses all probe silences.
    core.storage.db.execute(
        "INSERT INTO monitor_silence VALUES ('all', ?, 'maintenance', 'owner', ?, 0)",
        (watch_clock[0] + 3600, watch_clock[0]),
    )
    core.storage.db.commit()
    await core.watches.evaluate()
    assert len(notices(core)) == 2
    assert json.loads(notices(core)[0]["payload"])["title"] == "[Download] [⚠️ Stalled]"


async def test_download_gone_after_continuous_day(core, fake_backends, watch_clock):
    await notifications(core)
    await add(core, {"kind": "download_stalled", "title_contains": "Dune"})
    await core.watches.evaluate()
    downloads = fake_backends[0]["/downloads/active"]["downloads"]
    downloads.clear()
    watch_clock[0] += 600
    await core.watches.evaluate()
    watch_clock[0] += 23 * 3600
    await core.watches.evaluate()
    assert not notices(core)
    downloads.append({"id": "new", "title": "Dune", "status": "downloading", "progress": 1})
    watch_clock[0] += 600
    await core.watches.evaluate()
    downloads.clear()
    watch_clock[0] += 600
    await core.watches.evaluate()
    watch_clock[0] += 86400
    await core.watches.evaluate()
    assert len(notices(core)) == 2
    assert "download is gone" in json.loads(notices(core)[0]["payload"])["body"]
    await core.watches.evaluate(watch_clock[0] + 86400)
    assert len(notices(core)) == 2


async def test_watch_failure_throttled_and_transactional(
    core, fake_backends, watch_clock, monkeypatch
):
    await notifications(core)
    ident = await add(core, {"kind": "media_available", "tmdb_id": 100, "media": "movie"})
    source = fake_backends[0]["/movies/list"]
    fake_backends[0]["/movies/list"] = 503
    await core.monitor.cycle()
    row = (await core.watches.list("dot"))["watches"][0]
    assert row["last_checked_at"] == watch_clock[0] and row["fired_at"] is None
    assert core.monitor.status()["last_cycle_at"] is not None and not notices(core)
    fake_backends[0]["/movies/list"] = source
    await core.watches.evaluate()
    assert not notices(core)
    original = core.alerts.queue

    def broken(*args):
        original(*args)
        raise ValueError("PRIVATE")

    monkeypatch.setattr(core.alerts, "queue", broken)
    watch_clock[0] += 600
    await core.watches.evaluate()
    assert not notices(core)
    assert (await core.watches.list("dot"))["watches"][0]["fired_at"] is None
    monkeypatch.setattr(core.alerts, "queue", original)
    watch_clock[0] += 600
    await core.watches.evaluate()
    assert len(notices(core)) == 2
    assert (await core.watches.list("dot"))["watches"][0]["watch_id"] == ident


async def test_no_channels_preserves_watch_and_failure_isolates_other_watches(
    core, fake_backends, watch_clock
):
    args = {"kind": "media_available", "tmdb_id": 100, "media": "movie"}
    await add(core, args)
    await core.watches.evaluate()
    assert (await core.watches.list("dot"))["watches"][0]["fired_at"] is None
    await notifications(core)
    await add(core, {"kind": "download_stalled", "title_contains": "Dune"})
    fake_backends[0]["/downloads/active"] = {"bad": "PRIVATE"}
    watch_clock[0] += 600
    await core.watches.evaluate()
    assert len(notices(core)) == 2
    watches = (await core.watches.list("dot"))["watches"]
    assert sum(row["fired_at"] is not None for row in watches) == 1


async def test_next_episode_baseline_restart_and_airdate(shows, watch_clock):
    core, source, _ = shows
    await notifications(core)
    await add(core, {"kind": "media_available", "tmdb_id": 901, "media": "show"})
    state = json.loads(core.storage.db.execute("SELECT state FROM alert_watches").fetchone()[0])
    assert state["baseline"] == [18, 6]
    # Reopening the database preserves the baseline (the service itself stays fake).
    reopened = Storage(core.config.state_dir)
    assert json.loads(reopened.db.execute("SELECT state FROM alert_watches").fetchone()[0]) == state
    reopened.close()
    source["/library/metadata/118/children"]["MediaContainer"]["Metadata"].append({"index": 7})
    await core.watches.evaluate()
    assert not notices(core)  # In Plex but not aired on the frozen date yet.
    source["/api/v1/tv/901/season/18"]["episodes"][6]["airDate"] = "2026-11-04"
    watch_clock[0] += 600
    await core.watches.evaluate()
    assert len(notices(core)) == 2
    assert json.loads(notices(core)[0]["payload"])["body"] == "S18E7 is now in Plex."


async def test_specific_episode_requires_identity_not_season_counts(shows, watch_clock):
    core, source, _ = shows
    await notifications(core)
    await add(
        core,
        {"kind": "media_available", "tmdb_id": 901, "media": "show", "season": 18, "episode": 7},
    )
    await core.watches.evaluate()
    assert not notices(core)
    source["/library/metadata/118/children"]["MediaContainer"]["Metadata"].append({"index": 7})
    watch_clock[0] += 600
    await core.watches.evaluate()
    assert json.loads(notices(core)[0]["payload"])["body"] == "S18E7 is now in Plex."


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"kind": "bad"},
        {"kind": "download_stalled"},
        {"kind": "download_stalled", "title_contains": " "},
        {"kind": "download_stalled", "title_contains": "Dune", "tmdb_id": 1},
        {"kind": "media_available", "tmdb_id": 1},
        {"kind": "media_available", "tmdb_id": 1, "media": "movie", "season": 1, "episode": 1},
        {"kind": "media_available", "tmdb_id": 1, "media": "show", "season": 1},
        {"kind": "download_stalled", "title_contains": "Dune", "note": "x" * 201},
        {"kind": "download_stalled", "title_contains": "Dune", "expires_days": 61},
        {"kind": "download_stalled", "title_contains": "Dune", "expires_days": 0},
        {"kind": "media_available", "tmdb_id": True, "media": "movie"},
    ],
)
async def test_watch_invalid_before_io(core, fake_backends, args):
    before = len(fake_backends[1])
    assert (await core.call("dot", "alerts_watch_add", args))["error"][
        "code"
    ] == "invalid_arguments"
    assert len(fake_backends[1]) == before


async def test_watch_denial_hidden_and_audited(core):
    tools = {"alerts_watch_add", "alerts_watch_list", "alerts_watch_remove"}
    assert tools <= {t["name"] for t in core.tools("dot")}
    assert not tools & {t["name"] for t in core.tools("agy")}
    for name in tools:
        assert (await core.call("agy", name, {}))["error"]["code"] == "scope_denied"
        assert core.audit.tail()[-1]["decision"] == "denied"
    change_policy(core.config, lambda p: p["clients"]["dot"].update(scopes=[]))
    assert (await core.call("dot", "alerts_watch_list", {}))["error"]["code"] == "scope_denied"


async def test_download_progress_change_resets_stall_clock(core, fake_backends, watch_clock):
    await notifications(core)
    await add(core, {"kind": "download_stalled", "title_contains": "Dune"})
    await core.watches.evaluate()
    watch_clock[0] += 1200
    fake_backends[0]["/downloads/active"]["downloads"][0]["progress"] = 30
    await core.watches.evaluate()
    watch_clock[0] += 1200
    await core.watches.evaluate()
    assert not notices(core)
    watch_clock[0] += 600
    await core.watches.evaluate()
    assert len(notices(core)) == 2


async def test_never_matching_download_waits_full_day(core, fake_backends, watch_clock):
    await notifications(core)
    await add(core, {"kind": "download_stalled", "title_contains": "Absent"})
    await core.watches.evaluate()
    watch_clock[0] += 86400 - 600
    await core.watches.evaluate()
    assert not notices(core)
    watch_clock[0] += 600
    await core.watches.evaluate()
    assert len(notices(core)) == 2


async def test_next_episode_crosses_season_and_ignores_older_gaps(shows, watch_clock):
    core, source, _ = shows
    await notifications(core)
    await add(core, {"kind": "media_available", "tmdb_id": 901, "media": "show"})
    # Removing an earlier episode cannot change the saved baseline.
    source["/library/metadata/118/children"]["MediaContainer"]["Metadata"].pop(0)
    source["/api/v1/tv/901/season/18"]["episodes"] = source["/api/v1/tv/901/season/18"]["episodes"][
        :6
    ]
    source["/api/v1/tv/901"]["seasons"].append(
        {"seasonNumber": 19, "episodeCount": 1, "airDate": "2026-11-04"}
    )
    source["/api/v1/tv/901/season/19"] = {
        "episodes": [{"episodeNumber": 1, "airDate": "2026-11-04"}]
    }
    source["/library/metadata/100/children"]["MediaContainer"]["Metadata"].append(
        {"index": 19, "leafCount": 1, "ratingKey": "119"}
    )
    source["/library/metadata/119/children"] = {
        "MediaContainer": {"Metadata": [{"index": 1}], "size": 1}
    }
    await core.watches.evaluate()
    assert json.loads(notices(core)[0]["payload"])["body"] == "S19E1 is now in Plex."


async def test_next_episode_creation_failure_does_not_store_watch(shows, watch_clock):
    core, source, _ = shows
    source["/api/v1/tv/901"] = 503
    result = await core.call(
        "dot", "alerts_watch_add", {"kind": "media_available", "tmdb_id": 901, "media": "show"}
    )
    assert result["outcome"] == "error"
    assert not (await core.watches.list("dot"))["watches"]
