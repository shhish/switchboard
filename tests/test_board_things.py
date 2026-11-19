"""Phone captures use fake Things reads/completions and the fake Gitea board."""

import asyncio
import copy
import sqlite3
from unittest.mock import AsyncMock

import pytest

from switchboard.board import Board
from switchboard.config import Config
from switchboard.errors import CapabilityError
from switchboard.gitea import validate_settings

from .conftest import change_policy
from .test_board import board_api, call, writes  # noqa: F401


def todo(ident="t1", **fields):
    return {
        "id": ident,
        "name": "Watch together",
        "notes": "Phone notes\nSecond line",
        "status": "open",
        "project_id": "capture",
        "project_name": "Board",
        "area_id": None,
        **fields,
    }


@pytest.fixture
async def capture(board_api, monkeypatch):  # noqa: F811
    core, board, calls = board_api
    core.config.backends["gitea"]["board_things_project"] = "Board"
    core.config.backends["things"]["enabled"] = True
    change_policy(
        core.config,
        lambda p: p["allowlists"].update(
            tasks={
                "allow_all": False,
                "projects": ["capture"],
                "areas": [],
                "deny_projects": [],
                "deny_areas": [],
            }
        ),
    )
    state = {
        "todos": [todo()],
        "projects": [todo("capture", name="Board", project_id=None)],
        "calls": [],
        "fail_completions": set(),
        "read_error": None,
        "views": {},
        "truncated": False,
    }

    async def things_call(method, args=None):
        args = args or {}
        state["calls"].append((method, copy.deepcopy(args)))
        if method != "things.complete" and state["read_error"]:
            raise CapabilityError(state["read_error"], "Private backend text")
        if method == "things.add_project":
            core.policy.require_task(args)
            if state.get("create_error"):
                raise CapabilityError(state["create_error"], "Private creation text")
            project = todo("capture", name=args["title"], project_id=None)
            state["projects"].append(project)
            if state.get("create_race"):
                raise CapabilityError("things_conflict", "Private duplicate text")
            return {"project": project}
        if method == "things.projects":
            return {
                "projects": state["projects"],
                "truncated": state.get("projects_truncated", False),
            }
        if method == "things.lists":
            assert args["list"] in ("Anytime", "Today", "Upcoming", "Someday")
            assert args["limit"] == 200
            selected = state["views"].get(args["list"])
            rows = [
                t
                for t in state["todos"]
                if t["status"] == "open"
                and (t["id"] in selected if selected is not None else args["list"] == "Anytime")
            ]
            return {"todos": rows[:200], "truncated": len(rows) > 200 or state["truncated"]}
        assert method == "things.complete"
        current = next(t for t in state["todos"] if t["id"] == args["todo_id"])
        core.policy.require_task(args, current["project_id"], current["area_id"])
        # A second connection proves the receipt was committed before completion.
        with sqlite3.connect(core.config.state_dir / "state.sqlite3") as db:
            receipt = db.execute(
                "SELECT card FROM board_things_imports WHERE things_id=?", (current["id"],)
            ).fetchone()
        assert receipt and receipt[0].startswith("workspace#")
        if current["id"] in state["fail_completions"]:
            raise CapabilityError("things_backend_error", "Private completion text")
        current["status"] = "completed"
        return {"todo": current}

    monkeypatch.setattr(core.backends.things, "call", things_call)
    return core, board, calls, state


async def test_import_creates_ideas_card_then_completes(capture):
    core, board, _, state = capture
    result = await call(capture, "import_things")
    assert result["imported"] == [
        {"things_id": "t1", "card": "workspace#2", "title": "Watch together"}
    ]
    assert result["completed"] == 1 and result["skipped"] == 0 and not result["errors"]
    assert state["todos"][0]["status"] == "completed"
    created = board["issues"][2]
    assert created["title"] == "Watch together" and board["members"][created["id"]] == "Ideas"
    assert (
        created["body"]
        == "Phone notes\nSecond line\n\n_Added from Things._\n\n_Added by things via Switchboard._"
    )
    receipt = core.storage.db.execute(
        "SELECT * FROM board_cards WHERE issue_id=?", (created["id"],)
    ).fetchone()
    assert receipt["last_actor"] == "things"
    assert state["calls"][-1][1] == {"todo_id": "t1", **core.policy.task_rules()}


async def test_completion_failure_reuses_durable_mapping_after_restart(capture):
    core, board, calls, state = capture
    state["fail_completions"].add("t1")
    first = await call(capture, "import_things")
    assert first["completed"] == 0 and len(first["imported"]) == 1
    assert first["errors"] == [{"things_id": "t1", "code": "things_backend_error"}]
    assert state["todos"][0]["status"] == "open"
    core.board = Board(core)
    board["issues"][2]["state"] = "closed"  # Even a closed card must not be recreated.
    calls.clear()
    state["fail_completions"].clear()
    second = await call(capture, "import_things")
    assert second["completed"] == 1 and second["skipped"] == 1 and second["imported"] == []
    assert not calls and len(board["issues"]) == 2
    assert core.storage.db.execute("SELECT COUNT(*) FROM board_things_imports").fetchone()[0] == 1


async def test_distinct_things_ids_can_share_duplicate_card(capture):
    core, board, _, state = capture
    state["todos"].append(todo("t2"))
    result = await call(capture, "import_things")
    assert result["completed"] == 2
    assert len({i["card"] for i in result["imported"]}) == 1 and len(board["issues"]) == 2
    assert core.storage.db.execute("SELECT COUNT(*) FROM board_things_imports").fetchone()[0] == 2


@pytest.mark.parametrize("disabled", ["unset", "things", "gitea"])
async def test_disabled_capture_is_noop(capture, disabled):
    core, _, calls, state = capture
    if disabled == "unset":
        core.config.backends["gitea"]["board_things_project"] = ""
    else:
        core.config.backends[disabled]["enabled"] = False
    result = await call(capture, "import_things")
    assert result["imported"] == [] and result["completed"] == result["skipped"] == 0
    assert not result["errors"] and not calls and not state["calls"]
    before = core.storage.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    await core.board.evaluate_things(now=1000)
    assert core.storage.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


@pytest.mark.parametrize(
    "code",
    [
        "backend_disabled",
        "backend_down",
        "things_timeout",
        "things_unauthorized",
        "things_backend_error",
    ],
)
async def test_things_unavailable_is_quiet_noop(capture, code):
    _, _, calls, state = capture
    state["read_error"] = code
    result = await call(capture, "import_things")
    assert not result["errors"] and result["completed"] == 0 and not result["imported"]
    assert not calls


async def test_project_selection_open_status_views_and_dedup(capture):
    _, board, _, state = capture
    state["todos"] = [
        todo("t1"),
        todo("t2", name="Different", project_id="other"),
        todo("t3", status="completed"),
        todo("t4", status="canceled"),
        todo("t5", name="Tomorrow"),
        todo("t6", name="Someday"),
    ]
    state["views"] = {
        "Anytime": ["t2"],
        "Today": ["t1"],
        "Upcoming": ["t1", "t5"],
        "Someday": ["t6"],
    }
    result = await call(capture, "import_things")
    assert {i["things_id"] for i in result["imported"]} == {"t1", "t5", "t6"}
    assert result["completed"] == 3 and len(board["issues"]) == 4
    assert len([c for c in state["calls"] if c[0] == "things.complete"]) == 3


async def test_import_cap_and_truncation(capture):
    core, board, _, state = capture
    state["todos"] = [todo(f"t{n}", name=f"Idea {n}") for n in range(25)]
    result = await call(capture, "import_things")
    assert len(result["imported"]) == result["completed"] == 20
    assert result["skipped"] == 5 and result["truncated"]
    assert len(board["issues"]) == 21
    assert core.storage.db.execute("SELECT COUNT(*) FROM board_things_imports").fetchone()[0] == 20
    second = await call(capture, "import_things")
    assert second["completed"] == 5 and len(second["imported"]) == 5 and not second["truncated"]


async def test_import_surfaces_bounded_read_truncation(capture):
    _, _, _, state = capture
    state["truncated"] = True
    result = await call(capture, "import_things")
    assert result["completed"] == 1 and result["truncated"]


@pytest.mark.parametrize("scope", ["board:write", "tasks:write"])
async def test_both_scopes_required_before_io(capture, scope):
    core, _, calls, state = capture
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].remove(scope))
    result = await call(capture, "import_things")
    assert result["error"]["code"] == "scope_denied" and not calls and not state["calls"]


@pytest.mark.parametrize("domain", ["git", "tasks"])
async def test_resource_allowlists_before_creation_or_completion(capture, domain):
    core, _, calls, state = capture
    if domain == "git":
        change_policy(core.config, lambda p: p["allowlists"]["git"].update(write_repos=[]))
    else:
        change_policy(
            core.config, lambda p: p["allowlists"]["tasks"].update(deny_projects=["capture"])
        )
    result = await call(capture, "import_things")
    assert result["error"]["code"] == "resource_denied" and not writes(calls)
    assert not any(c[0] == "things.complete" for c in state["calls"])


async def test_card_failure_does_not_complete_or_map(capture):
    core, board, _, state = capture
    board["overrides"]["/repos/example/workspace/issues"] = 500
    result = await call(capture, "import_things")
    assert result["completed"] == 0 and result["errors"] == [
        {"things_id": "t1", "code": "backend_error"}
    ]
    assert not any(c[0] == "things.complete" for c in state["calls"])
    assert not core.storage.db.execute("SELECT * FROM board_things_imports").fetchall()


async def test_bad_title_skipped_and_other_capture_continues(capture):
    _, board, _, state = capture
    state["todos"] = [todo("t1", name=" "), todo("t2", name="x" * 201), todo("t3")]
    result = await call(capture, "import_things")
    assert result["skipped"] == 2 and len(result["errors"]) == 2 and result["completed"] == 1
    assert len(board["issues"]) == 2


async def test_ambiguous_project_reports_error_without_writes(capture):
    _, _, calls, state = capture
    state["projects"] = [todo("p1", name="Board"), todo("p2", name="Board")]
    result = await call(capture, "import_things")
    assert result["errors"] == [{"things_id": None, "code": "things_project_ambiguous"}]
    assert not calls and not any(m == "things.add_project" for m, _ in state["calls"])


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("race", [False, True])
async def test_missing_project_created_once_and_audited_as_system(capture, automatic, race):
    core, _, _, state = capture
    state.update(projects=[], create_race=race)
    if automatic:
        await core.board.evaluate_things(now=1000)
    else:
        result = await call(capture, "import_things")
        assert not result["errors"] and result["completed"] == 1
    await call(capture, "import_things")
    assert state["todos"][0]["status"] == "completed"
    creates = [args for method, args in state["calls"] if method == "things.add_project"]
    assert creates == [{"title": "Board", **core.policy.task_rules()}]
    row = core.storage.db.execute(
        "SELECT * FROM audit_log WHERE capability='tasks_add_project'"
    ).fetchone()
    assert row["client"] is None and row["origin"] == "system" and row["tier"] == "write"
    assert row["outcome"] == ("error" if race else "ok")
    assert row["error_code"] == ("things_conflict" if race else None)


@pytest.mark.parametrize("phase", ["read", "create"])
@pytest.mark.parametrize(
    "code", ["backend_down", "things_unauthorized", "things_timeout", "things_backend_error"]
)
async def test_unavailable_missing_project_is_quiet(capture, phase, code):
    core, _, calls, state = capture
    state["projects"] = []
    state["read_error" if phase == "read" else "create_error"] = code
    result = await call(capture, "import_things")
    assert not result["errors"] and result["completed"] == 0 and not calls
    assert state["todos"][0]["status"] == "open"
    if phase == "create":
        row = core.storage.db.execute(
            "SELECT * FROM audit_log WHERE capability='tasks_add_project'"
        ).fetchone()
        assert row["origin"] == "system" and row["error_code"] == code


@pytest.mark.parametrize("interval", [120, 300, 3600])
async def test_system_interval_gate_persists_and_audits(capture, interval):
    core, _, _, state = capture
    core.config.backends["gitea"]["board_things_interval_s"] = interval
    await core.board.evaluate_things(now=1000)
    assert state["todos"][0]["status"] == "completed"
    count = len(state["calls"])
    await core.board.evaluate_things(now=1000 + interval - 1)
    core.board = Board(core)
    await core.board.evaluate_things(now=1000 + interval - 1)
    assert len(state["calls"]) == count
    await core.board.evaluate_things(now=1000 + interval)
    assert len(state["calls"]) > count
    rows = core.storage.db.execute(
        "SELECT * FROM audit_log WHERE capability='board_import_things'"
    ).fetchall()
    assert len(rows) == 2 and all(r["origin"] == "system" and r["client"] is None for r in rows)
    assert all(r["outcome"] == "ok" and r["tier"] == "write" for r in rows)
    assert "Phone notes" not in (core.config.log_dir / "audit.jsonl").read_text()


async def test_failed_attempts_also_respect_interval(capture):
    core, _, _, state = capture
    state["read_error"] = "backend_down"
    await core.board.evaluate_things(now=1000)
    await core.board.evaluate_things(now=1100)
    assert len(state["calls"]) == 1
    state["read_error"] = None
    await core.board.evaluate_things(now=1300)
    assert state["todos"][0]["status"] == "completed"


async def test_concurrent_manual_and_system_import_share_lock(capture):
    core, board, _, _ = capture
    await asyncio.gather(core.board.evaluate_things(now=1000), call(capture, "import_things"))
    assert len(board["issues"]) == 2
    assert core.storage.db.execute("SELECT COUNT(*) FROM board_things_imports").fetchone()[0] == 1


async def test_monitor_runs_import_and_isolates_failures(capture, monkeypatch):
    core, _, _, state = capture
    monkeypatch.setattr(core.monitor.probes, "run", AsyncMock(return_value={"services": []}))
    await core.monitor.cycle()
    assert state["todos"][0]["status"] == "completed"
    broken = AsyncMock(side_effect=RuntimeError("Private error"))
    monkeypatch.setattr(core.board, "evaluate_things", broken)
    await core.monitor.cycle()
    await core.monitor.cycle()
    assert broken.await_count == 2


async def test_system_failure_is_safe_and_audited(capture, monkeypatch):
    core, _, _, _ = capture
    monkeypatch.setattr(
        core.board, "_import_things", AsyncMock(side_effect=RuntimeError("Private error"))
    )
    await core.board.evaluate_things(now=1000)
    row = core.storage.db.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    assert (
        row["origin"] == "system"
        and row["error_code"] == "internal_error"
        and row["outcome"] == "error"
    )
    assert "Private error" not in (core.config.log_dir / "audit.jsonl").read_text()


@pytest.mark.parametrize(
    "key,value",
    [
        ("board_things_project", None),
        ("board_things_project", 1),
        ("board_things_project", " "),
        ("board_things_project", "Bad\n"),
        ("board_things_project", "x" * 201),
        ("board_things_interval_s", True),
        ("board_things_interval_s", 120.0),
        ("board_things_interval_s", 119),
        ("board_things_interval_s", 3601),
    ],
)
def test_capture_settings_invalid(key, value):
    with pytest.raises(ValueError):
        validate_settings({"url": "http://127.0.0.1:1/api/v1", key: value})


def test_capture_config_defaults_and_roundtrip(config):
    defaults = validate_settings({"url": "http://127.0.0.1:1/api/v1"})
    assert defaults["board_things_project"] == "" and defaults["board_things_interval_s"] == 300
    (config.config_dir / "config.yaml").write_text(
        "backends:\n  gitea:\n    board_things_project: Board\n    board_things_interval_s: 120\n"
    )
    loaded = Config.load()
    assert loaded.backends["gitea"]["board_things_project"] == "Board"
    assert loaded.backends["gitea"]["board_things_interval_s"] == 120


async def test_project_discovery_truncation_refuses_ambiguous_selection(capture):
    _, _, calls, state = capture
    state["projects_truncated"] = True
    result = await call(capture, "import_things")
    assert result["errors"] == [{"things_id": None, "code": "backend_invalid"}]
    assert not calls and len(state["calls"]) == 1


async def test_import_tool_contract_and_sanitized_untrusted_titles(capture):
    core, board, _, state = capture
    cap = core.registry.capabilities["board_import_things"]
    assert cap.scope == "board:write" and cap.tier == "write" and not cap.idempotent
    assert cap.input_schema["properties"] == {} and not cap.input_schema["additionalProperties"]
    assert cap.memory is None and cap.memory_optout
    assert (
        len(cap.description) <= 400
        and "tasks:write" in cap.description
        and "untrusted" in cap.description
    )
    state["todos"][0]["name"] = "Untrusted\n\x00title"
    result = await call(capture, "import_things")
    assert result["imported"][0]["title"] == "Untrusted title"
    assert board["issues"][2]["title"] == "Untrusted\n\x00title"  # Source is forwarded as data.
