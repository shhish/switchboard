"""Owner feed, pulse and history use only local fakes and a frozen clock."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml

from switchboard import activity
from switchboard.audit import Audit, activity_refs
from switchboard.config import validate_probes
from switchboard.core import Core
from switchboard.errors import CapabilityError
from switchboard.monitor import STATE_RANK, SYSTEM_PROBE
from switchboard.storage import Storage

from .conftest import change_policy
from .test_board import board_api  # noqa: F401 - fake Gitea fixture

NOW = datetime(2026, 11, 20, 20, 30, tzinfo=UTC).timestamp()
HOUR = NOW // 3600 * 3600


@pytest.fixture
async def history_core(config, monkeypatch):
    config.probes = validate_probes(
        [
            {"name": "plex", "url": "http://127.0.0.1/", "label": "Plex"},
            {"name": "nzbget", "url": "http://127.0.0.1/", "label": "NZBGet"},
        ]
    )
    change_policy(config, lambda p: p["clients"]["cortex"]["scopes"].append("activity:read"))
    core = Core(config)
    monkeypatch.setattr(activity.time, "time", lambda: NOW)
    try:
        yield core
    finally:
        await core.close()


@pytest.fixture
def host_zone(monkeypatch):
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    if old is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", old)
    time.tzset()


def transition(core, state, at, *, name="plex", latency=5):
    with core.storage.db:
        core.monitor.transition({"name": name}, state, at, latency_ms=latency)


def sample(core, at, state="up", name="plex", checks=1, failures=0):
    with core.storage.db:
        core.storage.db.execute(
            "INSERT INTO monitor_hours VALUES (?, ?, ?, ?, ?)", (at, name, state, checks, failures)
        )


def audit(core, tool, *, client="claude", args=None, result=None, **kw):
    return core.audit.append(
        client=client,
        capability=tool,
        args=args or {},
        result=result,
        tier=kw.pop("tier", "write"),
        decision=kw.pop("decision", "allowed"),
        outcome=kw.pop("outcome", "ok"),
        **kw,
    )


def cache(core, title="Ship the app", card="project#1"):
    with core.storage.db:
        core.storage.db.execute(
            "INSERT INTO board_cards(issue_id, card, title, column_name) VALUES (1, ?, ?, 'Ideas')",
            (card, title),
        )


def ask(core, asker="claude"):
    spec = core.board.ask_spec({"kind": "text", "prompt": "When should it ship?"})
    with core.storage.db:
        core.storage.db.execute(
            "INSERT INTO board_asks(ask_id, issue_id, card, asker, kind, spec, state, "
            "return_column, created_at, answered_at, answered_by, answer) "
            "VALUES (?, 1, 'project#1', ?, 'text', ?, 'answered', 'In progress', "
            "?, ?, 'cortex', ?)",
            (
                "ask_" + "a" * 16,
                asker,
                json.dumps(spec),
                activity.iso(NOW - 200),
                activity.iso(NOW),
                json.dumps({"text": "after a TestFlight pass"}),
            ),
        )
    return "ask_" + "a" * 16


@pytest.mark.parametrize("previous", STATE_RANK)
@pytest.mark.parametrize("current", STATE_RANK)
def test_records_each_transition_and_hourly_upsert(history_core, previous, current):
    core = history_core
    transition(core, previous, HOUR + 10)
    transition(core, current, HOUR + 20, latency=11)
    rows = core.storage.db.execute("SELECT * FROM monitor_events ORDER BY id").fetchall()
    assert len(rows) == (1 if previous == current else 2)
    if previous != current:
        assert rows[-1]["previous"] == previous and rows[-1]["status"] == current
        assert rows[-1]["latency_ms"] == 11
    hourly = core.storage.db.execute("SELECT * FROM monitor_hours").fetchone()
    assert hourly["checks"] == 2
    assert hourly["failures"] == sum(s in {"down", "degraded"} for s in [previous, current])
    assert hourly["worst"] == max([previous, current], key=STATE_RANK.__getitem__)
    state = core.storage.db.execute("SELECT * FROM monitor_state WHERE name='plex'").fetchone()
    assert state["last_change_at"] == HOUR + (10 if previous == current else 20)
    assert state["latency_ms"] == 11


def test_atomic_history_and_incident_id(history_core):
    core = history_core
    transition(core, "down", HOUR)
    transition(core, "up", HOUR + 240)
    rows = core.storage.db.execute("SELECT * FROM monitor_events ORDER BY id").fetchall()
    assert rows[0]["incident_id"] == rows[1]["incident_id"]
    assert rows[0]["incident_id"]
    with pytest.raises(RuntimeError), core.storage.db:
        core.monitor.transition({"name": "plex"}, "degraded", NOW)
        raise RuntimeError
    assert core.storage.db.execute("SELECT COUNT(*) FROM monitor_events").fetchone()[0] == 2
    assert core.storage.db.execute("SELECT status FROM monitor_state").fetchone()[0] == "up"
    assert core.storage.db.execute("SELECT checks FROM monitor_hours").fetchone()[0] == 2


def test_process_samples_opt_out_and_system_outage(history_core):
    core = history_core
    core.config.probes[0]["alert"] = False
    services = [{"name": p["name"], "status": "down", "latency_ms": 9} for p in core.config.probes]
    core.monitor.process(services, HOUR)
    core.monitor.process(services, HOUR + 60)
    rows = list(core.storage.db.execute("SELECT * FROM monitor_hours"))
    assert {r["name"] for r in rows} == {"plex", "nzbget", SYSTEM_PROBE}
    assert all(r["checks"] == 2 for r in rows)
    assert (
        core.storage.db.execute(
            "SELECT latency_ms FROM monitor_state WHERE name='plex'"
        ).fetchone()[0]
        == 9
    )
    assert not core.storage.db.execute("SELECT 1 FROM alert_outbox").fetchone()


def test_prune_35_days(history_core):
    core = history_core
    cutoff = NOW - 35 * activity.DAY
    transition(core, "up", cutoff - 3600)
    transition(core, "down", cutoff)
    transition(core, "up", NOW)
    core.monitor.maintain()
    assert [r[0] for r in core.storage.db.execute("SELECT at FROM monitor_events ORDER BY at")] == [
        cutoff,
        NOW,
    ]
    assert (
        min(r[0] for r in core.storage.db.execute("SELECT hour FROM monitor_hours"))
        == cutoff // 3600 * 3600
    )


async def test_maintenance_loop_prunes_when_monitor_disabled(history_core, monkeypatch):
    core = history_core
    transition(core, "up", NOW - 36 * activity.DAY)

    async def no_disk():
        pass

    async def end(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(core.disk_health, "maintain", no_disk)
    monkeypatch.setattr("switchboard.core.asyncio.sleep", end)
    with pytest.raises(asyncio.CancelledError):
        await core.maintain()
    assert not core.storage.db.execute("SELECT 1 FROM monitor_events").fetchone()
    assert not core.storage.db.execute("SELECT 1 FROM monitor_hours").fetchone()


def test_quiet_day_pulse(history_core):
    for offset in range(24):
        sample(history_core, HOUR - offset * 3600)
    result = activity.pulse(history_core, {})
    assert result["hours"] == 24 and len(result["buckets"]) == 24
    assert result["summary"] == "Healthy all day"
    assert all(b["state"] == "up" and b["note"] is None for b in result["buckets"])
    assert result["buckets"][0]["start"] == activity.iso(HOUR - 23 * 3600)
    assert result["buckets"][-1]["start"] == activity.iso(HOUR)


def test_one_slow_hour_pulse(history_core, host_zone):
    # 08:00 UTC is 3 AM locally; earlier timestamps exercise DST independently of host defaults.
    slow_at = HOUR - 12 * 3600
    for offset in range(24):
        hour = HOUR - offset * 3600
        sample(history_core, hour, "degraded" if hour == slow_at else "up", name="nzbget")
        sample(history_core, hour)
    result = activity.pulse(history_core, {})
    assert result["summary"] == "One slow hour at 3 AM"
    assert result["buckets"][11] == {
        "start": activity.iso(slow_at),
        "state": "slow",
        "note": "NZBGet slow",
    }
    assert all(
        b["state"] == "up" for b in activity.pulse(history_core, {"name": "plex"})["buckets"]
    )


def test_outage_spans_two_hours(history_core, host_zone):
    # 1:58 PM to 2:02 PM local, with samples in both hours.
    down_at = HOUR - 3600 - 120
    transition(history_core, "up", down_at - 3600)
    transition(history_core, "down", down_at)
    transition(history_core, "down", HOUR - 3600 + 30)
    transition(history_core, "up", HOUR - 3600 + 120)
    result = activity.pulse(history_core, {"hours": 3, "name": "plex"})
    assert [b["state"] for b in result["buckets"]] == ["down", "down", "unknown"]
    assert [b["note"] for b in result["buckets"]] == ["Plex down 2 min", "Plex down 2 min", None]
    assert result["summary"] == "Plex was down for 4 minutes at 1 PM"


def test_no_data_unknown_partial_and_note_bound(history_core):
    result = activity.pulse(history_core, {})
    assert result["summary"] == "No health data yet"
    assert len(result["buckets"]) == 24 and all(b["state"] == "unknown" for b in result["buckets"])
    sample(history_core, HOUR)
    assert activity.pulse(history_core, {})["summary"] == "Healthy in 1 observed hour"
    history_core.config.probes[0]["label"] = "A" * 60
    sample(history_core, HOUR, "degraded", name="nzbget")
    sample(history_core, HOUR - 3600, "down")
    assert all(len(b["note"] or "") <= 80 for b in activity.pulse(history_core, {})["buckets"])
    with pytest.raises(CapabilityError, match="Unknown configured probe"):
        activity.pulse(history_core, {"name": "absent"})


MAPPINGS = [
    ("board_add", {}, "added", "added Ship the app", "info"),
    ("board_move", {"column": "In progress"}, "started", "started Ship the app", "info"),
    ("board_move", {"column": "Done"}, "finished", "finished Ship the app", "good"),
    ("board_move", {"column": "Needs you"}, "needs_you", "needs you: Ship the app", "warning"),
    ("board_move", {"column": "Up next"}, "moved", "moved Ship the app", "info"),
    ("board_ask", {}, "asked", "asked: When should it ship?", "warning"),
    (
        "git_pr_merge",
        {"repo": "example/project"},
        "merged",
        "merged a change into example/project",
        "good",
    ),
    (
        "git_tag_create",
        {"repo": "example/project", "tag": "v1.2.3"},
        "released",
        "released example/project v1.2.3",
        "good",
    ),
    ("ops_service_restart", {"service": "plex"}, "restarted", "restarted Plex", "info"),
    ("approvals_confirm", {}, "allowed", "allowed an action", "info"),
    ("approvals_approve", {}, "allowed", "allowed an action", "info"),
]


@pytest.mark.parametrize("actor", ["claude", "codex", "cortex"])
@pytest.mark.parametrize("tool,args,verb,wording,severity", MAPPINGS)
def test_audit_mapping(history_core, actor, tool, args, verb, wording, severity):
    core = history_core
    cache(core)
    ask_id = ask(core)
    audit(
        core,
        tool,
        client=actor,
        args={"card": "project#1", "ask_id": ask_id, **args},
        tier="approval" if tool.startswith("approvals_") else "write",
        result={"status": "ok", "card": "project#1", "ask": {"id": ask_id}},
    )
    items = activity.feed(core, {})["events"]
    assert len(items) == 1
    item = items[0]
    assert item["title"] == ("You" if actor == "cortex" else actor.capitalize()) + " " + wording
    assert item["verb"] == verb and item["severity"] == severity
    assert item["kind"] == ("owner" if actor == "cortex" else "agent")
    assert item["actor"] == actor and item["id"].startswith("audit:")


@pytest.mark.parametrize("actor", ["claude", "cortex"])
def test_answers_and_comments(history_core, actor):
    core = history_core
    cache(core)
    ask_id = ask(core, asker=actor)
    audit(
        core,
        "board_answer",
        client="cortex",
        args={"ask_id": ask_id, "answer": {"text": "NEVER FROM AUDIT"}},
    )
    audit(
        core, "board_comment", client=actor, args={"card": "project#1", "body": "NEVER FROM AUDIT"}
    )
    events = activity.feed(core, {"kinds": ["owner"]})["events"]
    answered = next(e for e in events if e["verb"] == "answered")
    assert answered["title"] == "You answered: after a TestFlight pass"
    assert (
        answered["detail"]
        == ("You" if actor == "cortex" else "Claude") + " asked: When should it ship?"
    )
    assert answered["card"] == "project#1"
    assert sum(e["verb"] == "replied" for e in events) == int(actor == "cortex")
    assert "NEVER FROM AUDIT" not in json.dumps(events)


def test_feed_privacy_and_unknown_title(history_core):
    core = history_core
    credential = "sk-" + "aB9" * 20
    cache(core, title=f"**Ship** [the app](https://example.test) {credential}\n`today`")
    audit(
        core,
        "board_add",
        args={"title": "PRIVATE ARGUMENT", "prompt": "PRIVATE PROMPT"},
        result={"status": "ok", "card": "project#1"},
        hash_fields={"title", "prompt"},
    )
    events = activity.feed(core, {})["events"]
    encoded = json.dumps(events)
    assert events[0]["title"] == "Claude added Ship the app [redacted] today"
    for forbidden in [
        credential,
        "PRIVATE ARGUMENT",
        "PRIVATE PROMPT",
        "sha256",
        "https://",
        "**",
        "`",
    ]:
        assert forbidden not in encoded
    assert core.audit.verify()["ok"]
    audit(core, "board_move", args={"card": "missing#7", "column": "Done"})
    assert any(e["title"] == "Claude finished missing#7" for e in activity.feed(core, {})["events"])


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/private/file",
        "~/private/file",
        "./private/file",
        "C:\\private\\file",
        "token=" + "a" * 20,
    ],
)
def test_redacts_paths_tokens_and_controls(value):
    result = activity.text("**Check** " + value + "\x00\nsoon")
    assert "private" not in result and "a" * 20 not in result
    assert "\n" not in result and "\x00" not in result and "**" not in result


@pytest.mark.parametrize(
    "extra",
    [
        {"decision": "denied"},
        {"outcome": "error"},
        {"outcome": "approval_required"},
        {"outcome": "failed"},
        {"tier": "read"},
        {"error_code": "backend_error"},
    ],
)
def test_feed_ignores_failed_and_read_calls(history_core, extra):
    audit(history_core, "git_pr_merge", args={"repo": "project"}, **extra)
    assert not activity.feed(history_core, {})["events"]


def test_monitor_pair_and_window_boundary(history_core, host_zone):
    core = history_core
    transition(core, "down", HOUR - 28 * 60)
    transition(core, "up", HOUR - 24 * 60)
    result = activity.feed(core, {"kinds": ["server"]})
    assert len(result["events"]) == 1
    item = result["events"][0]
    assert item["title"] == "Plex was down for 4 minutes"
    assert item["detail"] == "Back at 2:36 PM"
    assert item["verb"] == "recovered" and item["severity"] == "good"
    assert item["actor"] is None and item["service"] == "plex"
    assert item["at"] == activity.iso(HOUR - 24 * 60)
    # A down outside the requested window still gives the recovery its true duration.
    result = activity.feed(core, {"since": activity.iso(HOUR - 26 * 60)})
    assert len(result["events"]) == 1 and "4 minutes" in result["events"][0]["title"]


def test_monitor_flap_collapse(history_core):
    core = history_core
    for i, state in enumerate(["down", "up"] * 3):
        transition(core, state, HOUR + i * 60)
    events = activity.feed(core, {})["events"]
    assert len(events) == 1
    assert events[0]["detail"] == "Flapped 6 times" and events[0]["severity"] == "good"
    assert events[0]["at"] == activity.iso(HOUR + 5 * 60)
    assert events[0]["id"] == "monitor:6"


@pytest.mark.parametrize(
    "state,title,severity",
    [
        ("down", "Plex went down", "critical"),
        ("degraded", "Plex is slow", "warning"),
        ("unknown", "Plex state is unknown", "warning"),
    ],
)
def test_server_wording(history_core, state, title, severity):
    transition(history_core, state, HOUR)
    item = activity.feed(history_core, {})["events"][0]
    assert item["title"] == title and item["severity"] == severity


def test_since_kinds_order_limit_and_stable_ids(history_core, monkeypatch):
    core = history_core
    audit(core, "git_pr_merge", args={"repo": "project"})
    transition(core, "down", NOW - 1)
    monkeypatch.setattr(activity.time, "time", lambda: NOW + 1)
    audit(core, "git_tag_create", client="cortex", args={"repo": "project", "tag": "v1.0.0"})
    first = activity.feed(core, {})
    assert [e["kind"] for e in first["events"]] == ["owner", "agent", "server"]
    assert activity.feed(core, {}) == first
    assert first["since"] == activity.iso(NOW + 1 - activity.DAY)
    capped = activity.feed(core, {"limit": 2})
    assert len(capped["events"]) == 2 and capped["truncated"]
    assert not activity.feed(core, {"limit": 3})["truncated"]
    assert [e["kind"] for e in activity.feed(core, {"kinds": ["server"]})["events"]] == ["server"]
    assert activity.feed(core, {"kinds": []})["events"] == []
    assert len(activity.feed(core, {"since": activity.iso(NOW + 0.5)})["events"]) == 1
    activity.feed(core, {"since": activity.iso(NOW + 1 - 14 * activity.DAY)})


@pytest.mark.parametrize(
    "since",
    [
        "bad",
        "2026-11-20",
        "2026-11-20T10:00:00",
        activity.iso(NOW + 1),
        activity.iso(NOW - 14 * activity.DAY - 1),
    ],
)
async def test_since_bounds_through_core(history_core, since):
    result = await history_core.call("cortex", "activity_feed", {"since": since})
    assert result["error"]["code"] == "invalid_arguments"


async def test_scopes_schemas_and_unknown_probe(history_core):
    core = history_core
    for tool, args in [
        ("activity_feed", {}),
        ("ops_pulse", {}),
        ("ops_service_history", {"name": "plex"}),
    ]:
        cap = core.registry.capabilities[tool]
        jsonschema.Draft202012Validator.check_schema(cap.input_schema)
        jsonschema.Draft202012Validator.check_schema(cap.output_schema)
        result = await core.call("cortex", tool, args)
        assert result["status"] == "ok", (tool, result)
        jsonschema.validate(result, cap.output_schema)
        unknown = await core.call("cortex", tool, {**args, "extra": True})
        assert unknown["error"]["code"] == "invalid_arguments"
    assert (await core.call("cortex", "audit_query", {}))["error"]["code"] == "scope_denied"
    assert (await core.call("claude", "activity_feed", {}))["error"]["code"] == "scope_denied"
    assert (await core.call("agy", "ops_pulse", {}))["error"]["code"] == "scope_denied"
    assert (await core.call("cortex", "ops_service_history", {"name": "missing"}))["error"][
        "code"
    ] == "not_found"
    for tool, args in [
        ("ops_pulse", {"hours": 0}),
        ("ops_pulse", {"hours": 169}),
        ("activity_feed", {"limit": 201}),
        ("activity_feed", {"limit": 0}),
        ("activity_feed", {"kinds": ["invalid"]}),
        ("ops_service_history", {"name": "plex", "days": 15}),
    ]:
        assert (await core.call("cortex", tool, args))["error"]["code"] == "invalid_arguments"
    policy = yaml.safe_load(await asyncio.to_thread(Path("config/policy.example.yaml").read_text))
    assert [c for c, p in policy["clients"].items() if "activity:read" in p["scopes"]] == ["cortex"]


async def test_service_history(history_core):
    core = history_core
    # Restartable service ID can differ from the health probe ID.
    core.config.services = {"media-app": {"probe": "plex"}}
    transition(core, "down", HOUR)
    transition(core, "up", HOUR + 240, latency=12)
    audit(core, "ops_service_restart", args={"service": "media-app"}, outcome="queued")
    audit(core, "ops_service_restart", args={"service": "nzbget"})
    sample(core, HOUR - 3600, "up", checks=2)
    result = await core.call("cortex", "ops_service_history", {"name": "plex"})
    assert result["state"] == "up" and result["since"] == activity.iso(HOUR + 240)
    assert result["latency_ms"] == 12
    assert {e["verb"] for e in result["events"]} == {"restarted", "recovered"}
    assert all(e["service"] == "plex" for e in result["events"])
    assert result["uptime_percent"] == 75
    for _ in range(40):
        audit(core, "ops_service_restart", args={"service": "media-app"})
    assert len(activity.service_history(core, {"name": "plex"})["events"]) == 30
    empty = activity.service_history(core, {"name": "nzbget", "days": 1})
    assert empty["state"] == "unknown" and empty["since"] is None
    assert empty["latency_ms"] is None and empty["uptime_percent"] == 0


@pytest.mark.usefixtures("board_api")
async def test_actual_board_calls_cache_and_audit_refs(core):
    added = await core.call("codex", "board_add", {"repo": "project", "title": "Fresh title"})
    assert added["status"] == "ok"
    ref = added["card"]
    stored = core.audit.tail(1)[0]["args"]
    assert stored["_activity"] == {"card": ref}
    assert isinstance(stored["title"], dict) and "sha256" in stored["title"]
    row = core.storage.db.execute("SELECT * FROM board_cards WHERE card=?", (ref,)).fetchone()
    assert row["title"] == "Fresh title" and row["column_name"] == "Ideas"
    moved = await core.call("codex", "board_move", {"card": ref, "column": "In progress"})
    assert moved["status"] == "ok"
    assert core.audit.tail(1)[0]["args"]["_activity"] == {"card": ref, "column": "In progress"}
    asked = await core.call(
        "codex", "board_ask", {"card": ref, "kind": "text", "prompt": "Ship when?"}
    )
    assert asked["status"] == "ok"
    assert core.audit.tail(1)[0]["args"]["_activity"]["ask_id"] == asked["ask"]["id"]
    assert "Ship when?" not in await asyncio.to_thread(core.audit.path.read_text)
    assert core.audit.verify()["ok"]


def test_refs_never_record_free_text_or_secrets():
    secret = "sk-" + "aB9" * 20
    result = {
        "status": "ok",
        "card": secret,
        "title": "private",
        "ask": {"id": "ask_" + "a" * 16, "prompt": "private"},
    }
    assert activity_refs("board_ask", {}, result) == {"ask_id": "ask_" + "a" * 16}
    assert activity_refs("board_add", {}, {"status": "error", "card": "project#1"}) == {}


def test_old_database_migration_preserves_chain_and_receipts(tmp_path):
    root = tmp_path / "state"
    storage = Storage(root)
    audit_log = Audit(storage, tmp_path / "logs")
    audit_log.append(
        client="claude",
        capability="board_move",
        tier="write",
        decision="allowed",
        outcome="ok",
        args={"card": "project#1", "column": "Done"},
    )
    with storage.db:
        storage.db.execute(
            "INSERT INTO board_cards(issue_id, card, last_actor) VALUES (1, 'project#1', 'codex')"
        )
        # Recreate the pre-feature shape without new history/cache columns.
        storage.db.execute("ALTER TABLE board_cards DROP COLUMN title")
        storage.db.execute("ALTER TABLE board_cards DROP COLUMN column_name")
        storage.db.execute("ALTER TABLE monitor_state DROP COLUMN latency_ms")
        storage.db.execute("DROP TABLE monitor_events")
        storage.db.execute("DROP TABLE monitor_hours")
    head = storage.db.execute("SELECT entry_hash FROM audit_log").fetchone()[0]
    storage.close()
    reopened = Storage(root)
    try:
        assert {r[1] for r in reopened.db.execute("PRAGMA table_info(board_cards)")} >= {
            "title",
            "column_name",
        }
        assert "latency_ms" in {
            r[1] for r in reopened.db.execute("PRAGMA table_info(monitor_state)")
        }
        assert reopened.db.execute("SELECT last_actor FROM board_cards").fetchone()[0] == "codex"
        assert reopened.db.execute("SELECT entry_hash FROM audit_log").fetchone()[0] == head
        assert Audit(reopened, tmp_path / "logs").verify()["ok"]
        assert reopened.db.execute("SELECT COUNT(*) FROM monitor_hours").fetchone()[0] == 0
        assert reopened.db.execute("SELECT COUNT(*) FROM monitor_events").fetchone()[0] == 0
    finally:
        reopened.close()


def test_monitor_ids_survive_empty_pruning(history_core):
    core = history_core
    transition(core, "down", NOW - 36 * activity.DAY)
    first = core.storage.db.execute("SELECT id FROM monitor_events").fetchone()[0]
    core.monitor.maintain()
    transition(core, "up", NOW)
    assert core.storage.db.execute("SELECT id FROM monitor_events").fetchone()[0] > first


def test_degraded_recovery_and_slow_flaps(history_core):
    core = history_core
    transition(core, "down", HOUR)
    transition(core, "degraded", HOUR + 240)
    events = activity.feed(core, {})["events"]
    assert len(events) == 1
    assert events[0]["title"] == "Plex was down for 4 minutes"
    assert events[0]["severity"] == "warning" and events[0]["verb"] == "recovered"
    for i, state in enumerate(["degraded", "up"] * 3):
        transition(core, state, HOUR + 900 + i * 60, name="nzbget")
    event = next(e for e in activity.feed(core, {})["events"] if e["service"] == "nzbget")
    assert event["detail"] == "Flapped 6 times"


def test_opt_out_retires_alert_incident_but_retains_samples(history_core):
    core = history_core
    transition(core, "down", HOUR)
    assert core.storage.db.execute("SELECT down_since FROM monitor_state").fetchone()[0] == HOUR
    core.config.probes[0]["alert"] = False
    core.monitor.process([{"name": "plex", "status": "up"}], HOUR + 60)
    row = core.storage.db.execute("SELECT * FROM monitor_state WHERE name='plex'").fetchone()
    assert row["status"] == "up" and row["down_since"] is None and row["incident_id"] is None
    assert (
        core.storage.db.execute("SELECT COUNT(*) FROM monitor_events WHERE name='plex'").fetchone()[
            0
        ]
        == 2
    )


def test_identifiers_never_disclose_paths():
    item = activity.event(
        "test:1",
        NOW,
        "agent",
        "claude",
        "added",
        "Safe title",
        card="/private/card#1",
        service="/private/service",
    )
    assert item["card"] is None and item["service"] is None
    item = activity.event(
        "test:2", NOW, "server", None, "down", "A service went down", service="some_service"
    )
    assert item["service"] == "some_service"
