"""Structured Slate asks use fake Gitea and temporary SQLite/policy only."""

# ruff: noqa: F811
import asyncio
import base64
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import jsonschema
import pytest
import yaml

from switchboard.ask_actions import apply_action
from switchboard.board import Board
from switchboard.config import Config, validate_board
from switchboard.domains.board import ASK_MEDIA_MAX_BYTES
from switchboard.policy import Policy
from switchboard.protocol import Protocol

from .conftest import change_policy
from .test_board import (  # noqa: F401
    FORMATTER,
    board_api,
    board_brief_data,
    call,
    grouped,
    issue,
    writes,
)

OPTIONS = [
    {"id": "fast", "label": "Quick", "detail": "Less waiting"},
    {"id": "full", "label": "Full"},
]
FIELDS = [
    {"id": "name", "label": "Name", "type": "text", "required": True, "placeholder": "A name"},
    {"id": "context", "label": "Context", "type": "multiline", "required": False},
    {"id": "count", "label": "Count", "type": "number", "required": True},
    {"id": "enabled", "label": "Enabled", "type": "toggle", "required": True},
    {
        "id": "mode",
        "label": "Mode",
        "type": "choice",
        "required": True,
        "options": [{"id": "fast", "label": "Quick"}, {"id": "full", "label": "Full"}],
    },
    {"id": "when", "label": "When", "type": "date", "required": True},
]
VALUES = {
    "name": "Example",
    "context": "More context",
    "count": 3.5,
    "enabled": True,
    "mode": "fast",
    "when": "2028-02-29",
}


@pytest.fixture
async def asks(board_api):
    change_policy(
        board_api[0].config,
        lambda p: p["clients"]["cortex"]["scopes"].extend(["board:read", "board:answer"]),
    )
    return board_api


async def create(api, kind="confirm", **args):
    return await call(
        api, "ask", card="project#1", kind=kind, prompt="Proceed with this work?", **args
    )


async def owner(api, ask_id, answer):
    result = await api[0].call("cortex", "board_answer", {"ask_id": ask_id, "answer": answer})
    jsonschema.validate(
        result, api[0].registry.capabilities["board_answer"].advertised_output_schema()
    )
    return result


@pytest.mark.parametrize(
    "kind,extra",
    [
        (
            "confirm",
            {"yes_label": "Proceed", "no_label": "Wait", "destructive": True, "recommended": "no"},
        ),
        (
            "choice",
            {"options": OPTIONS, "multiple": True, "allow_other": True, "recommended": "full"},
        ),
        ("text", {}),
        ("form", {"fields": FIELDS}),
    ],
)
async def test_create_kinds_and_comments(asks, kind, extra):
    core, state, calls = asks
    result = await create(asks, kind, detail="Here is the context.", **extra)
    ask = result["ask"]
    assert ask["id"].startswith("ask_") and len(ask["id"]) == 20
    assert ask["from"] == "codex" and ask["state"] == "open"
    assert ask["answer"] is ask["answer_text"] is ask["answered"] is ask["answered_by"] is None
    assert result["asks_open"] == 1 and result["column"] == "Needs you"
    assert result["claimed_by"] is None and result["last_note"] == ask["prompt"]
    assert core.board.ask_row(ask["id"])["return_column"] == "Ideas"
    assert len(writes(calls)) == 2
    comment = state["comments"][1][-1]["body"]
    assert comment.startswith("**codex** asks: Proceed with this work?\n\nHere is the context.")
    assert comment.endswith(f"_Answer in the Server app. Ask {ask['id']}._")
    if kind == "confirm":
        assert "- Wait (suggested)" in comment and ask["destructive"]
        assert ask["options"][0] == {
            "id": "yes",
            "label": "Proceed",
            "detail": None,
            "image": False,
        }
    elif kind == "choice":
        assert "- Full (suggested)" in comment and ask["multiple"] and ask["allow_other"]
    elif kind == "form":
        assert "- Name\n- Context" in comment and len(ask["fields"]) == 6
        assert ask["fields"][1]["placeholder"] is None and ask["fields"][0]["options"] == []
    else:
        assert ask["options"] == ask["fields"] == []
    detail = await call(asks, "card", card="project#1")
    assert detail["asks"] == [ask]


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("confirm", {"options": OPTIONS}),
        ("confirm", {"fields": FIELDS}),
        ("confirm", {"multiple": False}),
        ("confirm", {"allow_other": False}),
        ("confirm", {"recommended": "fast"}),
        ("confirm", {"yes_label": " "}),
        ("confirm", {"no_label": "x" * 41}),
        ("choice", {}),
        ("choice", {"options": OPTIONS[:1]}),
        ("choice", {"options": OPTIONS * 5}),
        ("choice", {"options": OPTIONS * 2}),
        ("choice", {"options": [{"id": "Bad", "label": "Bad"}, OPTIONS[0]]}),
        ("choice", {"options": [{"id": "bad space", "label": "Bad"}, OPTIONS[0]]}),
        ("choice", {"options": [{"id": "a" * 33, "label": "Bad"}, OPTIONS[0]]}),
        ("choice", {"options": [{"id": "bad", "label": " "}, OPTIONS[0]]}),
        ("choice", {"options": [{**OPTIONS[0], "extra": "bad"}, OPTIONS[1]]}),
        ("choice", {"options": OPTIONS, "recommended": "missing"}),
        ("choice", {"options": OPTIONS, "fields": FIELDS}),
        ("choice", {"options": OPTIONS, "yes_label": "Yes"}),
        ("choice", {"options": OPTIONS, "no_label": "No"}),
        ("choice", {"options": OPTIONS, "destructive": False}),
        *[
            ("text", {key: value})
            for key, value in {
                "options": OPTIONS,
                "fields": FIELDS,
                "recommended": "fast",
                "multiple": False,
                "allow_other": False,
                "yes_label": "Yes",
                "no_label": "No",
                "destructive": False,
            }.items()
        ],
        ("form", {}),
        ("form", {"fields": []}),
        ("form", {"fields": FIELDS * 2}),
        ("form", {"fields": [FIELDS[0], FIELDS[0]]}),
        ("form", {"fields": [{**FIELDS[0], "options": FIELDS[4]["options"]}]}),
        ("form", {"fields": [{k: v for k, v in FIELDS[4].items() if k != "options"}]}),
        ("form", {"fields": [{**FIELDS[4], "options": FIELDS[4]["options"][:1]}]}),
        ("form", {"fields": [{**FIELDS[4], "options": [FIELDS[4]["options"][0]] * 2}]}),
        ("form", {"fields": [{**FIELDS[0], "type": "secret"}]}),
        ("form", {"fields": [{**FIELDS[0], "id": "Bad"}]}),
        ("form", {"fields": [{**FIELDS[0], "placeholder": "x" * 81}]}),
        ("form", {"fields": FIELDS, "multiple": False}),
    ],
)
async def test_create_invalid_kind_arguments(asks, kind, extra):
    result = await create(asks, kind, **extra)
    assert result["error"]["code"] == "invalid_arguments"
    assert not writes(asks[2])


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt": " "},
        {"prompt": "x" * 301},
        {"detail": "x" * 2001},
        {"detail": "\x00"},
        {"asker": "cortex"},
        {"unexpected": True},
    ],
)
async def test_create_invalid_common_arguments(asks, extra):
    args = {"card": "project#1", "kind": "confirm", "prompt": "Proceed?", **extra}
    assert (await call(asks, "ask", **args))["error"]["code"] == "invalid_arguments"
    assert not writes(asks[2])


@pytest.mark.parametrize(
    "column,claimed,expected",
    [
        ("Ideas", False, "Ideas"),
        ("Up next", False, "Up next"),
        ("In progress", True, "In progress"),
        ("Needs you", False, "Up next"),
        ("Needs you", True, "In progress"),
    ],
)
async def test_saved_return_column_and_claim(asks, column, claimed, expected):
    core, state, calls = asks
    state["members"][1001] = column
    if claimed:
        core.board.record(state["issues"][1], "example/project", column, "codex", claim=True)
    first = (await create(asks))["ask"]
    second = (await create(asks))["ask"]
    assert core.board.ask_row(first["id"])["return_column"] == expected
    assert core.board.ask_row(second["id"])["return_column"] == expected
    assert len([c for c in writes(calls) if c[1].endswith("/move")]) == (column != "Needs you")
    answer = await owner(asks, first["id"], {"choice": ["yes"]})
    assert answer["column"] == "Needs you" and answer["asks_open"] == 1
    answer = await owner(asks, second["id"], {"choice": ["no"]})
    assert answer["column"] == expected and answer["asks_open"] == 0
    assert answer["claimed_by"] == ("codex" if claimed else None)
    assert not any(c[0] == "PATCH" for c in calls)


async def test_five_open_limit_concurrency_and_closed_cards(asks):
    results = await asyncio.gather(*(create(asks) for _ in range(6)))
    assert sum(r["status"] == "ok" for r in results) == 5
    assert [r["error"]["code"] for r in results if r["status"] == "error"] == ["invalid_arguments"]
    assert len(asks[1]["comments"][1]) == 5
    first = next(r["ask"]["id"] for r in results if r["status"] == "ok")
    await call(asks, "ask_withdraw", ask_id=first)
    assert (await create(asks))["status"] == "ok"


@pytest.mark.parametrize("column,state", [("Done", "open"), ("Ideas", "closed")])
async def test_create_requires_reopened_card(asks, column, state):
    asks[1]["members"][1001] = column
    asks[1]["issues"][1]["state"] = state
    result = await create(asks)
    assert result["error"]["code"] == "invalid_arguments"
    assert "reopen" in result["error"]["message"].lower() and not writes(asks[2])


@pytest.mark.parametrize(
    "kind,extra,answer,rendered",
    [
        ("confirm", {}, {"choice": ["yes"]}, "Yes"),
        ("confirm", {"yes_label": "Go"}, {"choice": ["yes"], "note": "Ready"}, "Go — Ready"),
        ("confirm", {}, {"choice": ["no"]}, "No"),
        ("choice", {"options": OPTIONS}, {"choice": ["fast"]}, "Quick"),
        (
            "choice",
            {"options": OPTIONS, "multiple": True},
            {"choice": ["full", "fast"]},
            "Full, Quick",
        ),
        ("choice", {"options": OPTIONS, "allow_other": True}, {"other": "Later"}, "Later"),
        (
            "choice",
            {"options": OPTIONS, "allow_other": True},
            {"choice": [], "other": "Later"},
            "Later",
        ),
        (
            "choice",
            {"options": OPTIONS, "allow_other": True},
            {"choice": ["fast"], "other": "Later"},
            "Quick, Later",
        ),
        ("text", {}, {"text": "A value", "note": "Context"}, "A value — Context"),
        (
            "form",
            {"fields": FIELDS},
            {"values": VALUES},
            "Name: Example; Context: More context; Count: 3.5; Enabled: Yes; "
            "Mode: Quick; When: 2028-02-29",
        ),
        (
            "form",
            {"fields": FIELDS},
            {
                "values": {
                    k: (False if k == "enabled" else v) for k, v in VALUES.items() if k != "context"
                }
            },
            "Name: Example; Count: 3.5; Enabled: No; Mode: Quick; When: 2028-02-29",
        ),
        (
            "form",
            {"fields": [{**FIELDS[0], "required": False}]},
            {"values": {}, "note": "Skip"},
            "— Skip",
        ),
    ],
)
async def test_answers_per_kind(asks, kind, extra, answer, rendered):
    created = await create(asks, kind, **extra)
    result = await owner(asks, created["ask"]["id"], answer)
    ask = result["ask"]
    assert ask["answer"] == answer and ask["answer_text"] == rendered
    assert ask["state"] == "answered" and ask["answered_by"] == "cortex" and ask["answered"]
    assert asks[1]["comments"][1][-1]["body"] == f"**cortex** answered **codex**: {rendered}"
    assert result["last_note"] == rendered[:200] and result["last_actor"] == "cortex"


BAD_ANSWERS = [
    ("confirm", {}, {}),
    ("confirm", {}, {"choice": []}),
    ("confirm", {}, {"choice": ["yes", "no"]}),
    ("confirm", {}, {"choice": ["yes", "yes"]}),
    ("confirm", {}, {"choice": ["maybe"]}),
    ("confirm", {}, {"other": "Maybe"}),
    ("confirm", {}, {"choice": "yes"}),
    ("confirm", {}, {"choice": ["yes"], "extra": 1}),
    ("confirm", {}, {"choice": ["yes"], "text": "extra"}),
    ("confirm", {}, {"choice": ["yes"], "note": " "}),
    ("confirm", {}, {"choice": ["yes"], "note": "x" * 1001}),
    ("choice", {"options": OPTIONS}, {"choice": ["fast", "full"]}),
    ("choice", {"options": OPTIONS, "multiple": True}, {"choice": ["fast", "fast"]}),
    ("choice", {"options": OPTIONS, "multiple": True}, {"choice": []}),
    ("choice", {"options": OPTIONS}, {"choice": ["missing"]}),
    ("choice", {"options": OPTIONS}, {"choice": ["fast"], "other": "Later"}),
    ("choice", {"options": OPTIONS, "allow_other": True}, {"other": " "}),
    ("choice", {"options": OPTIONS, "allow_other": True}, {"other": "x" * 501}),
    ("text", {}, {}),
    ("text", {}, {"text": " "}),
    ("text", {}, {"text": "x" * 4001}),
    ("text", {}, {"text": "Okay", "choice": ["yes"]}),
    ("form", {"fields": FIELDS}, {}),
    ("form", {"fields": FIELDS}, {"values": {}}),
    ("form", {"fields": FIELDS}, {"values": {**VALUES, "unknown": "extra"}}),
    *[
        ("form", {"fields": FIELDS}, {"values": {**VALUES, key: bad}})
        for key, bad in [
            ("name", " "),
            ("name", "x" * 2001),
            ("name", 2),
            ("context", ""),
            ("context", False),
            ("count", True),
            ("count", "3"),
            ("count", float("nan")),
            ("count", float("inf")),
            ("enabled", 1),
            ("enabled", "yes"),
            ("mode", "unknown"),
            ("mode", []),
            ("when", "2026-02-29"),
            ("when", "2028-2-29"),
            ("when", "20280229"),
            ("when", "2028-13-01"),
            ("when", 2028),
            ("when", "0000-01-01"),
        ]
    ],
]


@pytest.mark.parametrize("kind,extra,answer", BAD_ANSWERS)
async def test_invalid_answers_leave_ask_open(asks, kind, extra, answer):
    created = (await create(asks, kind, **extra))["ask"]
    asks[2].clear()
    result = await owner(asks, created["id"], answer)
    assert result["error"]["code"] == "invalid_arguments"
    assert asks[0].board.ask_row(created["id"])["state"] == "open"
    assert not writes(asks[2])


async def test_answer_scope_self_answer_and_double_answer(asks):
    core, state, calls = asks
    created = (await create(asks))["ask"]
    calls.clear()
    args = {"ask_id": created["id"], "answer": {"choice": ["yes"]}}
    assert (await core.call("codex", "board_answer", args))["error"]["code"] == "scope_denied"
    assert "board_answer" not in {t["name"] for t in core.tools("codex")}
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("board:answer"))
    assert (await call(asks, "answer", **args))["error"]["code"] == "resource_denied"
    assert not calls
    await owner(asks, created["id"], {"choice": ["yes"]})
    result = await owner(asks, created["id"], {"choice": ["no"]})
    assert result["error"]["code"] == "invalid_arguments"
    assert "already answered" in result["error"]["message"]
    assert len(state["comments"][1]) == 2
    assert core.storage.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "column,state", [("Up next", "open"), ("Done", "closed"), ("Needs you", "closed")]
)
async def test_answer_preserves_owner_moves_and_closed_state(asks, column, state):
    ask = (await create(asks))["ask"]
    asks[1]["members"][1001] = column
    asks[1]["issues"][1]["state"] = state
    asks[2].clear()
    result = await owner(asks, ask["id"], {"choice": ["yes"]})
    assert result["column"] == ("Done" if state == "closed" else column)
    assert len(writes(asks[2])) == 1 and writes(asks[2])[0][1].endswith("/comments")


async def test_withdraw_ownership_state_comment_and_return(asks):
    core, state, calls = asks
    first, second = (await create(asks))["ask"], (await create(asks))["ask"]
    change_policy(core.config, lambda p: p["clients"]["cortex"]["scopes"].append("board:write"))
    calls.clear()
    denied = await core.call("cortex", "board_ask_withdraw", {"ask_id": first["id"]})
    assert denied["error"]["code"] == "resource_denied" and not calls
    result = await call(asks, "ask_withdraw", ask_id=first["id"])
    assert result["ask"]["state"] == "withdrawn" and result["ask"]["answered"] is None
    assert result["column"] == "Needs you" and result["asks_open"] == 1
    assert state["comments"][1][-1]["body"] == "**codex** withdrew the ask: Proceed with this work?"
    assert (await owner(asks, first["id"], {"choice": ["yes"]}))["error"][
        "code"
    ] == "invalid_arguments"
    assert (await call(asks, "ask_withdraw", ask_id=first["id"]))["error"][
        "code"
    ] == "invalid_arguments"
    result = await call(asks, "ask_withdraw", ask_id=second["id"])
    assert result["column"] == "Ideas" and result["asks_open"] == 0
    third = (await create(asks))["ask"]
    await owner(asks, third["id"], {"choice": ["yes"]})
    assert (await call(asks, "ask_withdraw", ask_id=third["id"]))["error"][
        "code"
    ] == "invalid_arguments"


@pytest.mark.parametrize("name", ["ask_wait", "ask_withdraw", "answer"])
async def test_unknown_ask(asks, name):
    args = {"ask_id": "ask_" + "0" * 16}
    if name == "answer":
        result = await owner(asks, args["ask_id"], {"choice": ["yes"]})
    else:
        result = await call(asks, name, **args)
    assert result["error"]["code"] == "not_found" and not asks[2]


@pytest.mark.parametrize(
    "mode", ["immediate", "answered", "withdrawn", "timeout", "zero", "default"]
)
async def test_wait_without_sleep_or_gitea(asks, mode):
    core, _, calls = asks
    ask_id = (await create(asks))["ask"]["id"]
    if mode == "immediate":
        await owner(asks, ask_id, {"choice": ["yes"]})
    calls.clear()
    now, sleeps = [0], []

    async def sleep(seconds):
        now[0] += seconds
        sleeps.append(seconds)
        if mode in {"answered", "withdrawn"}:
            if mode == "answered":
                await owner(asks, ask_id, {"choice": ["yes"]})
            else:
                await call(asks, "ask_withdraw", ask_id=ask_id)
            calls.clear()  # Only the separate answering/withdrawing turn calls Gitea.

    core.board = Board(core, clock=lambda: now[0], sleep=sleep)
    timeout = 0 if mode == "zero" else 5
    args = {} if mode == "default" else {"timeout_s": timeout}
    result = await call(asks, "ask_wait", ask_id=ask_id, **args)
    assert result["timed_out"] == (mode in {"timeout", "zero", "default"})
    expected = {
        "immediate": 0,
        "answered": 2,
        "withdrawn": 2,
        "timeout": 5,
        "zero": 0,
        "default": 120,
    }[mode]
    assert result["waited_s"] == expected and sum(sleeps) == expected and not calls
    assert all(0 < s <= 2 for s in sleeps)
    assert core.registry.capabilities["board_ask_wait"].timeout_s == 300


async def test_card_and_list_order_limits_recent_window(asks):
    core, state, _ = asks
    # Seed durable rows with varied states, dates, cards and authors.
    spec = core.board.ask_spec({"kind": "text", "prompt": "Which value?"})
    now = datetime.now(UTC)
    with core.storage.db as db:
        for n in range(24):
            issue_id = 1002 if n == 23 else 1001
            stamp = (now - timedelta(hours=24 - n)).isoformat()
            db.execute(
                "INSERT INTO board_asks VALUES (?, ?, ?, ?, 'text', ?, ?, 'Ideas', ?, ?, ?, ?)",
                (
                    f"ask_{n:016x}",
                    issue_id,
                    "project#2" if n == 23 else "project#1",
                    "codex",
                    json.dumps(spec),
                    "open" if n < 5 else "answered",
                    stamp,
                    (now - timedelta(hours=73)).isoformat() if n == 22 else stamp,
                    None if n < 5 else "cortex",
                    None if n < 5 else json.dumps({"text": f"Value {n}"}),
                ),
            )
    state["issues"][2] = issue(2, state="closed")
    state["members"][1002] = "Done"
    result = await call(asks, "list")
    card = grouped(result)["Ideas"]["cards"][0]
    assert card["asks_open"] == 5
    assert [a["id"] for a in card["open_asks"]] == [f"ask_{n:016x}" for n in range(3)]
    assert len(result["answered"]) == 10
    assert [a["ask_id"] for a in result["answered"]] == [f"ask_{n:016x}" for n in range(21, 11, -1)]
    assert result["answered"][0]["answer_text"] == "Value 21"
    assert not (await call(asks, "list", project="workspace"))["answered"]
    detail = await call(asks, "card", card="project#1")
    assert len(detail["asks"]) == 10
    assert [a["id"] for a in detail["asks"]] == [f"ask_{n:016x}" for n in range(22, 12, -1)]
    assert (await call(asks, "add", title="New", repo="project"))["asks_open"] == 0
    listing = await call(asks, "list")
    assert grouped(listing)["Ideas"]["cards"][0]["open_asks"] == []


async def test_sanitization_and_answer_render_cap(asks):
    result = await call(
        asks,
        "ask",
        card="project#1",
        kind="text",
        prompt="Which\n\x00value?",
        detail="Some\ncontext",
    )
    assert result["ask"]["prompt"] == "Which value?" and result["ask"]["detail"] == "Some context"
    result = await owner(
        asks, result["ask"]["id"], {"text": "\x00First\n" + "x" * 3500, "note": "Why\nnow"}
    )
    assert result["ask"]["answer"]["text"].startswith("First ")
    assert len(result["ask"]["answer_text"]) == 600
    assert result["ask"]["answer"]["note"] == "Why now"


async def test_audit_hashes_all_free_text_including_nested_and_invalid_calls(asks):
    core, _, _ = asks
    prompt, detail, label, field_label, value = (
        "PRIVATE_PROMPT",
        "PRIVATE_DETAIL",
        "PRIVATE_LABEL",
        "PRIVATE_FIELD",
        "PRIVATE_VALUE",
    )
    created = await call(
        asks,
        "ask",
        card="project#1",
        kind="choice",
        prompt=prompt,
        detail=detail,
        options=[
            {"id": "a", "label": label, "detail": "PRIVATE_OPTION_CONTEXT"},
            {"id": "b", "label": "Second"},
        ],
    )
    await owner(asks, created["ask"]["id"], {"other": value})  # Invalid is hashed too.
    await owner(asks, created["ask"]["id"], {"choice": ["a"], "note": value})
    created = await call(
        asks,
        "ask",
        card="project#1",
        kind="form",
        prompt=prompt,
        fields=[{**FIELDS[0], "label": field_label}],
    )
    await owner(asks, created["ask"]["id"], {"values": {"name": value}})
    await create(asks, yes_label="PRIVATE_YES", no_label="PRIVATE_NO")
    await create(
        asks,
        "plan",
        steps=[{"id": "apply", "label": "PRIVATE_STEP", "detail": "PRIVATE_STEP_CONTEXT"}],
        evidence=[{"label": "PRIVATE_EVIDENCE", "value": "PRIVATE_EVIDENCE_VALUE"}],
    )
    private_path = str(core.config.state_dir / "PRIVATE_IMAGE_PATH")
    await create(asks, "choice", options=[{**OPTIONS[0], "image": private_path}, OPTIONS[1]])
    await create(asks, pictures=[{"image": private_path, "caption": "PRIVATE_CAPTION"}])
    log = core.audit.path.read_text()
    db_args = " ".join(r[0] for r in core.storage.db.execute("SELECT args_json FROM audit_log"))
    for private in (
        prompt,
        detail,
        label,
        field_label,
        value,
        "PRIVATE_YES",
        "PRIVATE_NO",
        "PRIVATE_OPTION_CONTEXT",
        "PRIVATE_STEP",
        "PRIVATE_STEP_CONTEXT",
        "PRIVATE_EVIDENCE",
        "PRIVATE_EVIDENCE_VALUE",
        "PRIVATE_CAPTION",
        private_path,
    ):
        assert private not in log and private not in db_args
    records = core.audit.tail(30)
    choice = next(r for r in records if r["capability"] == "board_ask" and "options" in r["args"])
    assert set(choice["args"]["options"]) == {"len", "sha256"}
    form = next(r for r in records if "fields" in (r.get("args") or {}))
    assert set(form["args"]["fields"]) == {"len", "sha256"}
    for key in ("steps", "evidence"):
        record = next(r for r in records if key in (r.get("args") or {}))
        assert set(record["args"][key]) == {"len", "sha256"}
    for record in records:
        if record["capability"] == "board_answer":
            assert set(record["args"]["answer"]) == {"len", "sha256"}


async def test_allowlist_denials_and_no_hidden_scope_bypass(asks):
    core, _, calls = asks
    ask_id = (await create(asks))["ask"]["id"]
    change_policy(core.config, lambda p: p["allowlists"]["git"].update(write_repos=[]))
    calls.clear()
    for name, args in [
        ("ask", {"card": "project#1", "kind": "text", "prompt": "Value?"}),
        ("ask_withdraw", {"ask_id": ask_id}),
    ]:
        result = await call(asks, name, **args)
        assert result["error"]["code"] == "resource_denied"
    assert (await owner(asks, ask_id, {"choice": ["yes"]}))["error"]["code"] == "resource_denied"
    assert not calls and core.board.ask_row(ask_id)["state"] == "open"
    for name in ("ask", "ask_withdraw", "ask_wait", "ask_media", "answer"):
        result = await core.call("agy", "board_" + name, {})
        assert result["error"]["code"] == "scope_denied"


async def test_gitea_partial_failure_storage_order(asks):
    core, state, _ = asks
    path = "/repos/example/project/issues/1/comments"
    state["overrides"][path] = 503
    assert (await create(asks))["error"]["code"] == "backend_error"
    assert core.storage.db.execute("SELECT COUNT(*) FROM board_asks").fetchone()[0] == 0
    assert state["members"][1001] == "Needs you"
    del state["overrides"][path]
    ask_id = (await create(asks))["ask"]["id"]
    state["overrides"][path] = 503
    assert (await owner(asks, ask_id, {"choice": ["yes"]}))["error"]["code"] == "backend_error"
    assert core.board.ask_row(ask_id)["state"] == "answered"
    assert (await owner(asks, ask_id, {"choice": ["no"]}))["error"]["code"] == "invalid_arguments"


def test_session_brief_answers_bounded_and_missing_keys():
    data = board_brief_data()
    data["answered"] = [
        {
            "card": "project#1",
            "from": "codex",
            "answered_by": "cortex",
            "prompt": "Select\nvalue?",
            "answer_text": "Use the default",
        },
        {"card": "project#2", "from": "codex", "answered_by": "codex", "prompt": "Self answer"},
        {},
        None,
    ]
    rendered = FORMATTER["format_board"](data)
    heading = "Answered by the owner, not yet acted on (board_card for the full answer)"
    assert heading in rendered and "Select value? → Use the default · asked by codex" in rendered
    assert "Self answer" not in rendered and len(rendered) <= 2400
    assert rendered.index("Needs you\n") < rendered.index(heading) < rendered.index("In progress\n")
    assert "use board_ask (yes/no, choice, text or form), not a prose note" in rendered
    data["answered"] = [{"from": "codex", "answered_by": "cortex"}]
    assert heading in FORMATTER["format_board"](data)
    data.pop("answered")
    assert heading not in FORMATTER["format_board"](data)
    data["answered"] = [
        {
            "card": "p" * 200,
            "prompt": "q" * 300,
            "answer_text": "a" * 600,
            "from": "agent",
            "answered_by": "cortex",
        }
    ] * 30
    rendered = FORMATTER["format_board"](data)
    assert "q" * 81 not in rendered and "a" * 121 not in rendered and len(rendered) <= 2400


def test_session_brief_hides_answers_an_agent_already_acted_on():
    data = board_brief_data()
    card = data["columns"][0]["cards"][0]
    card["card"] = "unique#1"
    answer = {
        "card": "unique#1",
        "from": "codex",
        "answered_by": "cortex",
        "prompt": "Go ahead?",
        "answer_text": "Yes",
        "answered": "2026-11-19T10:00:00+00:00",
    }
    data["answered"] = [answer]
    card.update(last_actor="claude", updated="2026-11-19T11:00:00Z")
    assert "Go ahead?" not in FORMATTER["format_board"](data)
    card.update(last_actor="cortex")
    assert "Go ahead?" in FORMATTER["format_board"](data)


async def test_large_forms_fit_advertised_result_budgets(asks):
    core, state, _ = asks
    fields = [
        {
            "id": f"field{n}",
            "label": "🟢" * 80,
            "type": "multiline",
            "required": True,
            "placeholder": "🟢" * 80,
        }
        for n in range(8)
    ]
    answer = {"values": {f"field{n}": "🟢" * 2000 for n in range(8)}}
    spec = core.board.ask_spec(
        {"kind": "form", "prompt": "🟢" * 300, "detail": "🟢" * 2000, "fields": fields}
    )
    stamp = datetime.now(UTC).isoformat()
    with core.storage.db as db:
        for n in range(10):
            db.execute(
                "INSERT INTO board_asks VALUES (?, 1001, 'project#1', 'codex', 'form', "
                "?, 'answered', 'Ideas', ?, ?, 'cortex', ?)",
                (f"ask_{n:016x}", json.dumps(spec), stamp, stamp, json.dumps(answer)),
            )
    result = await call(asks, "card", card="project#1")
    assert result["truncated"] and 0 < len(result["asks"]) < 10
    assert len(json.dumps(result)) <= core.registry.capabilities["board_card"].max_result_chars
    waited = await call(asks, "ask_wait", ask_id="ask_" + "0" * 16)
    assert not waited["timed_out"] and waited["ask"]["answer"] == answer
    assert len(json.dumps(waited)) <= core.registry.capabilities["board_ask_wait"].max_result_chars
    created = await call(
        asks,
        "ask",
        card="project#1",
        kind="form",
        prompt=spec["prompt"],
        detail=spec["detail"],
        fields=fields,
    )
    result = await owner(asks, created["ask"]["id"], answer)
    assert len(json.dumps(result)) <= core.registry.capabilities["board_answer"].max_result_chars
    # Even the existing comment window can be large when JSON escapes Unicode.
    state["comments"][1] = [
        {"id": n + 1, "body": "🟢" * 1500, "user": {"login": "service"}, "created_at": stamp}
        for n in range(30)
    ]
    state["issues"][1]["comments"] = 30
    result = await call(asks, "card", card="project#1")
    assert result["truncated"] and 0 < len(result["comments"]) < 30
    assert len(json.dumps(result)) <= core.registry.capabilities["board_card"].max_result_chars


async def test_large_open_forms_bound_list_with_counts_intact(asks):
    core, state, _ = asks
    fields = [
        {
            "id": f"field{n}",
            "label": "🟢" * 80,
            "type": "choice",
            "required": True,
            "placeholder": "🟢" * 80,
            "options": [{"id": f"option{m}", "label": "🟢" * 80} for m in range(8)],
        }
        for n in range(8)
    ]
    spec = core.board.ask_spec(
        {"kind": "form", "prompt": "🟢" * 300, "detail": "🟢" * 2000, "fields": fields}
    )
    stamp = datetime.now(UTC).isoformat()
    with core.storage.db as db:
        for n in range(1, 11):
            state["issues"][n] = issue(n)
            state["members"][1000 + n] = "Needs you"
            for m in range(3):
                db.execute(
                    "INSERT INTO board_asks (ask_id, issue_id, card, asker, kind, spec, "
                    "state, return_column, created_at) VALUES "
                    "(?, ?, ?, 'codex', 'form', ?, 'open', 'Ideas', ?)",
                    (f"ask_{n * 3 + m:016x}", 1000 + n, f"project#{n}", json.dumps(spec), stamp),
                )
    result = await call(asks, "list", limit=100)
    needs = grouped(result)["Needs you"]
    assert result["truncated"] and needs["count"] == result["needs_you"] == 10
    assert 0 < len(needs["cards"]) < 10
    assert all(c["asks_open"] == len(c["open_asks"]) == 3 for c in needs["cards"])
    assert len(json.dumps(result)) <= core.registry.capabilities["board_list"].max_result_chars


STEPS = [
    {"id": "apply", "label": "Apply change", "detail": "Update the files"},
    {"id": "publish", "label": "Publish", "optional": True},
    {"id": "notify", "label": "Notify", "optional": True},
]


async def test_plan_create_comment_and_roundtrip(asks):
    created = await create(asks, "plan", steps=STEPS, destructive=True)
    ask = created["ask"]
    assert ask["steps"] == [
        {**STEPS[0], "optional": False},
        {**STEPS[1], "detail": None},
        {**STEPS[2], "detail": None},
    ]
    assert ask["destructive"] and ask["options"] == ask["fields"] == ask["evidence"] == []
    assert (
        "\n1. Apply change\n2. Publish (optional)\n3. Notify (optional)\n"
        in (asks[1]["comments"][1][-1]["body"])
    )
    assert (await call(asks, "card", card="project#1"))["asks"] == [ask]
    waited = await call(asks, "ask_wait", ask_id=ask["id"], timeout_s=0)
    assert waited["ask"] == ask


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"steps": []},
        {"steps": STEPS * 4},
        {"steps": STEPS * 2},
        *[
            {"steps": [{**STEPS[0], key: value}]}
            for key, value in [
                ("id", "Bad"),
                ("id", "bad space"),
                ("id", "x" * 33),
                ("label", " "),
                ("label", "x" * 121),
                ("detail", " "),
                ("detail", "x" * 201),
                ("optional", "yes"),
                ("extra", True),
            ]
        ],
        *[
            {"steps": STEPS, key: value}
            for key, value in [
                ("options", OPTIONS),
                ("fields", FIELDS),
                ("multiple", False),
                ("allow_other", False),
                ("yes_label", "Yes"),
                ("no_label", "No"),
                ("recommended", "apply"),
            ]
        ],
    ],
)
async def test_plan_invalid_create(asks, extra):
    result = await create(asks, "plan", **extra)
    assert result["error"]["code"] == "invalid_arguments" and not writes(asks[2])
    assert asks[0].storage.db.execute("SELECT COUNT(*) FROM board_asks").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("confirm", {}),
        ("choice", {"options": OPTIONS}),
        ("text", {}),
        ("form", {"fields": FIELDS}),
    ],
)
async def test_only_plan_accepts_steps(asks, kind, extra):
    result = await create(asks, kind, steps=STEPS, **extra)
    assert result["error"]["code"] == "invalid_arguments" and not asks[2]


@pytest.mark.parametrize(
    "answer,expected,rendered",
    [
        (
            {"approve": True},
            {"approve": True, "steps": ["apply", "publish", "notify"]},
            "Approved all 3 steps",
        ),
        (
            {"approve": True, "steps": ["notify", "publish", "apply"]},
            {"approve": True, "steps": ["apply", "publish", "notify"]},
            "Approved all 3 steps",
        ),
        (
            {"approve": True, "steps": ["apply", "notify"]},
            {"approve": True, "steps": ["apply", "notify"]},
            "Approved 2 of 3 steps (left out: Publish)",
        ),
        (
            {"approve": True, "steps": ["apply"]},
            {"approve": True, "steps": ["apply"]},
            "Approved 1 of 3 steps (left out: Publish, Notify)",
        ),
        ({"approve": False}, {"approve": False}, "Not yet"),
        (
            {"approve": False, "note": "Later\nplease"},
            {"approve": False, "note": "Later please"},
            "Not yet — Later please",
        ),
        (
            {"approve": True, "note": "Checked"},
            {"approve": True, "steps": ["apply", "publish", "notify"], "note": "Checked"},
            "Approved all 3 steps — Checked",
        ),
    ],
)
async def test_plan_answers(asks, answer, expected, rendered):
    ask = (await create(asks, "plan", steps=STEPS))["ask"]
    result = await owner(asks, ask["id"], answer)
    assert result["ask"]["answer"] == expected and result["ask"]["answer_text"] == rendered
    stored = asks[0].board.ask_row(ask["id"])
    assert json.loads(stored["answer"]) == expected
    assert asks[0].board.ask_object(stored) == result["ask"]
    assert asks[1]["comments"][1][-1]["body"].endswith(rendered)
    listing = await call(asks, "list")
    assert listing["answered"][0]["answer_text"] == rendered
    data = board_brief_data()
    data["answered"] = listing["answered"]
    assert rendered in FORMATTER["format_board"](data)


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"approve": None},
        {"approve": 1},
        {"approve": "yes"},
        {"approve": False, "steps": []},
        {"approve": False, "steps": ["apply"]},
        {"approve": True, "steps": []},
        {"approve": True, "steps": ["publish"]},
        {"approve": True, "steps": ["apply", "apply"]},
        {"approve": True, "steps": ["apply", "missing"]},
        {"approve": True, "steps": "apply"},
        {"approve": True, "steps": [1]},
        {"approve": True, "choice": ["apply"]},
        {"approve": True, "values": {}},
        {"approve": True, "other": "Later"},
        {"approve": True, "text": "Yes"},
        {"approve": True, "note": " "},
        {"approve": True, "unknown": False},
    ],
)
async def test_invalid_plan_answers(asks, answer):
    ask = (await create(asks, "plan", steps=STEPS))["ask"]
    asks[2].clear()
    result = await owner(asks, ask["id"], answer)
    assert result["error"]["code"] == "invalid_arguments" and not asks[2]
    assert asks[0].board.ask_row(ask["id"])["state"] == "open"


async def test_plan_optional_only_and_render_cap(asks):
    steps = [{"id": f"s{n}", "label": "x" * 120, "optional": True} for n in range(10)]
    ask = (await create(asks, "plan", steps=steps))["ask"]
    result = await owner(asks, ask["id"], {"approve": True, "steps": [], "note": "Later"})
    assert result["ask"]["answer"]["steps"] == []
    assert result["ask"]["answer_text"].startswith("Approved 0 of 10 steps (left out: ")
    assert len(result["ask"]["answer_text"]) == 600


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("confirm", {}),
        ("choice", {"options": OPTIONS}),
        ("text", {}),
        ("form", {"fields": FIELDS}),
        ("plan", {"steps": STEPS}),
    ],
)
async def test_evidence_roundtrip_and_sanitizing(asks, kind, extra):
    evidence = [
        {"label": "Tests\npass", "value": "214\x00 of 214"},
        {"label": "QA", "state": "pending"},
        {"label": "Build", "state": "failed", "value": "Retry"},
    ]
    ask = (await create(asks, kind, evidence=evidence, **extra))["ask"]
    assert ask["evidence"] == [
        {"label": "Tests pass", "value": "214 of 214", "state": "done"},
        {"label": "QA", "value": None, "state": "pending"},
        evidence[2],
    ]
    assert "214 of 214" not in asks[1]["comments"][1][-1]["body"]
    assert (await call(asks, "card", card="project#1"))["asks"][0] == ask


@pytest.mark.parametrize(
    "evidence",
    [
        [{"label": "x"}] * 9,
        [{}],
        [{"label": " "}],
        [{"label": "x" * 101}],
        [{"label": "x", "value": " "}],
        [{"label": "x", "value": "x" * 41}],
        [{"label": "x", "state": "unknown"}],
        [{"label": "x", "unknown": True}],
        [{"label": "x", "value": None}],
        "bad",
    ],
)
async def test_invalid_evidence(asks, evidence):
    result = await create(asks, evidence=evidence)
    assert result["error"]["code"] == "invalid_arguments" and not asks[2]


async def test_evidence_empty_and_eight_lines(asks):
    assert (await create(asks, evidence=[]))["ask"]["evidence"] == []
    rows = [{"label": "x" * 100, "value": "y" * 40, "state": "done"}] * 8
    assert (await create(asks, evidence=rows))["ask"]["evidence"] == rows


@pytest.fixture
def picture(asks, tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    path = root / "preview.data"
    path.write_bytes(b"\x89PNG\r\n\x1a\nexample")
    asks[0].config.board = validate_board({"ask_media_roots": [str(root)]})
    return path


@pytest.mark.parametrize(
    "case",
    [
        "off",
        "outside",
        "sibling",
        "symlink",
        "directory",
        "large",
        "magic",
        "relative",
        "missing",
    ],
)
@pytest.mark.parametrize("source", ["option", "ask"])
async def test_invalid_pictures(asks, picture, tmp_path, case, source):
    path = picture
    options = [{**OPTIONS[0], "image": str(path)}, OPTIONS[1]]
    if case == "off":
        asks[0].config.board = validate_board({})
    elif case in {"outside", "sibling", "symlink"}:
        outside_dir = tmp_path / "pictures-other"
        outside_dir.mkdir()
        outside = outside_dir / "preview.png"
        outside.write_bytes(picture.read_bytes())
        if case == "symlink":
            link = picture.parent / "escape.png"
            link.symlink_to(outside)
            options[0]["image"] = str(link)
        else:
            options[0]["image"] = str(outside)
    elif case == "directory":
        options[0]["image"] = str(path.parent)
    elif case == "large":
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * ASK_MEDIA_MAX_BYTES)
    elif case == "magic":
        path.write_bytes(b"PRIVATE_FILE_CONTENTS")
    elif case == "relative":
        options[0]["image"] = path.name
    elif case == "missing":
        path.unlink()
    if source == "option":
        result = await create(asks, "choice", options=options)
    else:
        result = await create(asks, pictures=[{"image": options[0]["image"]}])
    assert result["error"]["code"] == "invalid_arguments" and not asks[2]
    assert "PRIVATE_FILE_CONTENTS" not in json.dumps(result)
    assert not (asks[0].config.state_dir / "ask-media").exists()


@pytest.mark.parametrize(
    "extension,magic,media_type",
    [
        ("png", b"\x89PNG\r\n\x1a\n", "image/png"),
        ("jpg", b"\xff\xd8\xff", "image/jpeg"),
    ],
)
async def test_picture_copy_and_media_roundtrip(asks, picture, extension, magic, media_type):
    # A maximum-size response must fit the registry budget, irrespective of file extension.
    payload = magic + b"x" * (ASK_MEDIA_MAX_BYTES - len(magic))
    picture.write_bytes(payload)
    link = picture.parent / "alias"
    link.symlink_to(picture)
    ask = (await create(asks, "choice", options=[{**OPTIONS[0], "image": str(link)}, OPTIONS[1]]))[
        "ask"
    ]
    assert [o["image"] for o in ask["options"]] == [True, False]
    assert str(picture) not in json.dumps(ask)
    directory = asks[0].board.media_directory(ask["id"])
    copied = directory / f"fast.{extension}"
    assert copied.read_bytes() == payload
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in (directory, directory.parent))
    picture.unlink()
    result = await call(asks, "ask_media", ask_id=ask["id"], option_id="fast")
    assert result["bytes"] == ASK_MEDIA_MAX_BYTES and result["media_type"] == media_type
    assert base64.b64decode(result["data_base64"]) == payload
    assert (
        len(json.dumps(result)) < asks[0].registry.capabilities["board_ask_media"].max_result_chars
    )
    transported = await Protocol(asks[0]).dispatch(
        "tools/call",
        {"name": "board_ask_media", "arguments": {"ask_id": ask["id"], "option_id": "fast"}},
        "codex",
        False,
    )
    assert not transported["isError"]
    assert transported["structuredContent"]["data_base64"] == result["data_base64"]
    assert len(transported["content"][0]["text"]) < 500_000
    await owner(asks, ask["id"], {"choice": ["fast"]})
    retained = await call(asks, "ask_media", ask_id=ask["id"], option_id="fast")
    assert retained["data_base64"] == result["data_base64"]
    for ask_id, option in [
        (ask["id"], "full"),
        (ask["id"], "unknown"),
        ("ask_" + "0" * 16, "fast"),
    ]:
        missing = await call(asks, "ask_media", ask_id=ask_id, option_id=option)
        assert missing["error"]["code"] == "not_found"
    copied.unlink()
    assert (await call(asks, "ask_media", ask_id=ask["id"], option_id="fast"))["error"][
        "code"
    ] == "not_found"


async def test_eight_option_pictures_and_four_ask_pictures(asks, picture):
    options = [{"id": f"p{n}", "label": "Picture", "image": str(picture)} for n in range(8)]
    pictures = [{"image": str(picture)}] * 4
    ask = (await create(asks, "choice", options=options, pictures=pictures))["ask"]
    assert all(o["image"] for o in ask["options"])
    assert len(ask["pictures"]) == 4
    assert len(list(asks[0].board.media_directory(ask["id"]).iterdir())) == 12
    for picture_id in [o["id"] for o in options] + [p["id"] for p in ask["pictures"]]:
        assert (await call(asks, "ask_media", ask_id=ask["id"], option_id=picture_id))[
            "status"
        ] == "ok"
    options.append({"id": "ninth", "label": "Picture", "image": str(picture)})
    cap = asks[0].registry.capabilities["board_ask"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"card": "project#1", "kind": "choice", "prompt": "Choose", "options": options},
            cap.input_schema,
        )
    assert (await create(asks, "choice", options=options))["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    "kind,extra,answer",
    [
        ("confirm", {}, {"choice": ["yes"]}),
        ("choice", {"options": OPTIONS}, {"choice": ["fast"]}),
        ("text", {}, {"text": "Looks good"}),
        ("form", {"fields": FIELDS}, {"values": VALUES}),
        ("plan", {"steps": [{"id": "review", "label": "Review"}]}, {"approve": True}),
    ],
)
async def test_ask_pictures_roundtrip(asks, picture, kind, extra, answer):
    payload = picture.read_bytes()
    ask = (
        await create(
            asks,
            kind,
            pictures=[{"image": str(picture), "caption": "c" * 120}, {"image": str(picture)}],
            **extra,
        )
    )["ask"]
    expected = [{"id": "pic1", "caption": "c" * 120}, {"id": "pic2", "caption": None}]
    assert ask["pictures"] == expected
    assert not any(o["image"] for o in ask["options"])
    board = asks[0].board
    assert json.loads(board.ask_row(ask["id"])["spec"])["pictures"] == expected
    assert str(picture) not in json.dumps(ask)
    assert str(picture) not in asks[0].audit.path.read_text()
    assert (await call(asks, "card", card="project#1"))["asks"] == [ask]
    assert (await call(asks, "ask_wait", ask_id=ask["id"], timeout_s=0))["ask"] == ask
    picture.unlink()  # Media reads use private copies.
    for picture_id in ("pic1", "pic2"):
        copied = board.media_directory(ask["id"]) / f"{picture_id}.png"
        assert copied.read_bytes() == payload
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        result = await call(asks, "ask_media", ask_id=ask["id"], option_id=picture_id)
        assert base64.b64decode(result["data_base64"]) == payload
    for picture_id in ("pic3", "pic4"):
        assert (await call(asks, "ask_media", ask_id=ask["id"], option_id=picture_id))["error"][
            "code"
        ] == "not_found"
    assert (await owner(asks, ask["id"], answer))["ask"]["pictures"] == expected
    assert (await call(asks, "ask_media", ask_id=ask["id"], option_id="pic1"))["status"] == "ok"


@pytest.mark.parametrize(
    "pictures",
    [
        [{}],
        [{"image": "preview.png"}] * 5,
        [{"image": "preview.png", "caption": ""}],
        [{"image": "preview.png", "caption": " "}],
        [{"image": "preview.png", "caption": "x" * 121}],
        [{"image": "preview.png", "caption": None}],
        [{"image": "preview.png", "unknown": True}],
        "bad",
    ],
)
async def test_invalid_ask_picture_arguments(asks, pictures):
    result = await create(asks, pictures=pictures)
    assert result["error"]["code"] == "invalid_arguments" and not asks[2]


async def test_overlapping_picture_ids(asks, picture):
    options = [{**OPTIONS[0], "id": "pic1", "image": str(picture)}, OPTIONS[1]]
    result = await create(asks, "choice", options=options, pictures=[{"image": str(picture)}])
    assert result["error"]["code"] == "invalid_arguments" and not asks[2]
    assert not (asks[0].config.state_dir / "ask-media").exists()
    # Existing option IDs remain valid without an ask picture using the same ID.
    ask = (await create(asks, "choice", options=options))["ask"]
    assert (await call(asks, "ask_media", ask_id=ask["id"], option_id="pic1"))["status"] == "ok"
    options[0].pop("image")
    ask = (await create(asks, "choice", options=options, pictures=[{"image": str(picture)}]))["ask"]
    assert not ask["options"][0]["image"]
    assert (await call(asks, "ask_media", ask_id=ask["id"], option_id="pic1"))["status"] == "ok"


@pytest.mark.parametrize("failure", ["copy", "comment", "move", "insert", "cancel"])
async def test_picture_failure_cleans_copies_and_creates_no_ask(
    asks, picture, monkeypatch, failure
):
    board = asks[0].board
    if failure in {"copy", "cancel"}:
        original = board.copy_pictures

        def broken(ask_id, pictures):
            original(ask_id, pictures)
            if failure == "cancel":
                raise asyncio.CancelledError
            raise OSError("Copy failed")

        monkeypatch.setattr(board, "copy_pictures", broken)
    elif failure in {"comment", "move"}:
        path = (
            "/repos/example/project/issues/1/comments"
            if failure == "comment"
            else "/user/projects/19/issues/1001/move"
        )
        asks[1]["overrides"][path] = 503
    else:
        asks[0].storage.db.execute(
            "CREATE TRIGGER fail_ask BEFORE INSERT ON board_asks "
            "BEGIN SELECT RAISE(ABORT, 'fail'); END"
        )
    options = [{**OPTIONS[0], "image": str(picture)}, OPTIONS[1]]
    pictures = [{"image": str(picture)}]
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await create(asks, "choice", options=options, pictures=pictures)
    else:
        result = await create(asks, "choice", options=options, pictures=pictures)
        assert result["status"] == "error"
    assert asks[0].storage.db.execute("SELECT COUNT(*) FROM board_asks").fetchone()[0] == 0
    assert not list((asks[0].config.state_dir / "ask-media").glob("*"))


async def test_media_prune_closed_after_thirty_days(asks, picture):
    board = asks[0].board
    now = datetime.now(UTC)
    ids = []
    for action in ("answer", "withdraw", "open", "recent", "boundary"):
        ask = (
            await create(
                asks,
                "choice",
                options=[{**OPTIONS[0], "image": str(picture)}, OPTIONS[1]],
                pictures=[{"image": str(picture), "caption": "Preview"}],
            )
        )["ask"]
        ids.append(ask["id"])
        if action == "open":
            continue
        if action == "withdraw":
            await call(asks, "ask_withdraw", ask_id=ask["id"])
        else:
            await owner(asks, ask["id"], {"choice": ["fast"]})
        assert (await call(asks, "ask_media", ask_id=ask["id"], option_id="fast"))["status"] == "ok"
        closed = now - timedelta(
            days={"answer": 31, "withdraw": 31, "recent": 29, "boundary": 30}[action]
        )
        with asks[0].storage.db as db:
            if action == "withdraw":
                spec = json.loads(board.ask_row(ask["id"])["spec"])
                spec["closed_at"] = closed.isoformat()
                db.execute(
                    "UPDATE board_asks SET spec=? WHERE ask_id=?", (json.dumps(spec), ask["id"])
                )
            else:
                db.execute(
                    "UPDATE board_asks SET answered_at=? WHERE ask_id=?",
                    (closed.isoformat(), ask["id"]),
                )
    board.prune_ask_media(now)
    board.prune_ask_media(now)  # Idempotent; history and option flags remain.
    assert [board.media_directory(i).exists() for i in ids] == [False, False, True, True, True]
    assert board.ask_object(board.ask_row(ids[0]))["options"][0]["image"]
    assert board.ask_object(board.ask_row(ids[0]))["pictures"] == [
        {"id": "pic1", "caption": "Preview"}
    ]
    assert (await call(asks, "ask_media", ask_id=ids[0], option_id="pic1"))["error"][
        "code"
    ] == "not_found"
    assert (await call(asks, "ask_media", ask_id=ids[2], option_id="pic1"))["status"] == "ok"
    assert (await call(asks, "ask_media", ask_id=ids[0], option_id="fast"))["error"][
        "code"
    ] == "not_found"


async def test_old_specs_default_new_keys(asks):
    ask = (await create(asks))["ask"]
    board = asks[0].board
    spec = json.loads(board.ask_row(ask["id"])["spec"])
    spec.pop("steps")
    spec.pop("evidence")
    spec.pop("pictures")
    assert ask["pictures"] == []
    for option in spec["options"]:
        option.pop("image")
    with asks[0].storage.db as db:
        db.execute("UPDATE board_asks SET spec=? WHERE ask_id=?", (json.dumps(spec), ask["id"]))
    assert (await call(asks, "card", card="project#1"))["asks"] == [ask]
    assert (await owner(asks, ask["id"], {"choice": ["yes"]}))["ask"]["answer_text"] == "Yes"


@pytest.mark.parametrize(
    "settings",
    [
        None,
        [],
        {"unknown": []},
        {"ask_media_roots": "bad"},
        {"ask_media_roots": ["relative"]},
        {"ask_media_roots": [3]},
        "too_many",
    ],
)
def test_invalid_board_media_config(settings, tmp_path):
    if settings == "too_many":
        settings = {"ask_media_roots": [str(tmp_path)] * 9}
    with pytest.raises(ValueError):
        validate_board(settings)


def test_board_media_config_loading(config, tmp_path):
    assert Config.load().board == {"ask_media_roots": []}
    root = tmp_path / "pictures"
    root.mkdir()
    (config.config_dir / "config.yaml").write_text(
        json.dumps({"board": {"ask_media_roots": [str(root)]}})
    )
    assert Config.load().board == {"ask_media_roots": [str(root.resolve())]}
    file = root / "file"
    file.touch()
    with pytest.raises(ValueError):
        validate_board({"ask_media_roots": [str(file)]})
    (config.config_dir / "config.yaml").write_text("board: {unknown: []}")
    with pytest.raises(ValueError):
        Config.load()


async def test_periodic_maintenance_prunes_media(asks, monkeypatch):
    core = asks[0]
    prune = Mock()
    with monkeypatch.context() as patch:
        patch.setattr(core.board, "prune_ask_media", prune)
        patch.setattr(core.audit, "maintain", Mock())
        patch.setattr(core.memory, "maintain", Mock())
        patch.setattr(core.disk_health, "maintain", AsyncMock())
        patch.setattr(
            "switchboard.core.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)
        )
        with pytest.raises(asyncio.CancelledError):
            await core.maintain()
    prune.assert_called_once()
    assert prune.call_args.args[0].tzinfo == UTC


async def test_media_state_parent_alias_and_repository_denial(asks, picture, tmp_path, monkeypatch):
    core = asks[0]
    original = core.config.state_dir
    alias = tmp_path / "state-parent"
    alias.symlink_to(original.parent, target_is_directory=True)
    core.config.state_dir = alias / original.name
    ask = (
        await create(asks, "choice", options=[{**OPTIONS[0], "image": str(picture)}, OPTIONS[1]])
    )["ask"]
    assert (await call(asks, "ask_media", ask_id=ask["id"], option_id="fast"))["status"] == "ok"
    assert str(picture) not in core.audit.path.read_text()
    change_policy(core.config, lambda p: p["allowlists"]["git"].update(write_repos=[]))
    reader = Mock(side_effect=AssertionError("Denied repositories must not read pictures"))
    monkeypatch.setattr(core.board, "picture_inputs", reader)
    result = await create(
        asks, "choice", options=[{**OPTIONS[0], "image": str(picture)}, OPTIONS[1]]
    )
    assert result["error"]["code"] == "resource_denied"
    reader.assert_not_called()


async def test_media_tool_contract_and_schema_denials(asks):
    cap = asks[0].registry.capabilities["board_ask_media"]
    assert cap.scope == "board:read" and cap.tier == "read" and cap.idempotent
    assert cap.max_result_chars == 500_000 and len(cap.description) <= 400
    assert all(term in cap.description for term in ("Use when", "Requires", "Returns"))
    for args in (
        {},
        {"ask_id": "../bad", "option_id": "fast"},
        {"ask_id": "ask_" + "0" * 16, "option_id": "../bad"},
        {"ask_id": "ask_" + "0" * 16, "option_id": "fast", "path": "ignored"},
    ):
        assert (await call(asks, "ask_media", **args))["error"]["code"] == "invalid_arguments"


GRANT = {"type": "grant_scope", "client": "agy", "scope": "ops:read"}


@pytest.mark.parametrize(
    "action",
    [
        {**GRANT, "client": "unknown"},
        {**GRANT, "scope": "unknown:read"},
        {**GRANT, "scope": "jobs:read"},
        {**GRANT, "scope": "approvals:confirm"},
        {**GRANT, "scope": "approvals:invented"},
        {**GRANT, "scope": "board:answer"},
        {**GRANT, "path": "/unrelated"},
        {**GRANT, "summary": "Trust the agent"},
        {"type": "grant_scope", "client": "agy"},
        {"type": "add_ask_media_root"},
        {"type": "run_shell", "path": "/unrelated"},
        None,
    ],
)
async def test_ask_action_invalid_scope_and_shape(asks, action):
    result = await create(asks, action=action)
    assert result["error"]["code"] == "invalid_arguments"
    assert not writes(asks[2])
    assert asks[0].storage.db.execute("SELECT COUNT(*) FROM board_asks").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["choice", "text", "form", "plan"])
async def test_ask_action_only_confirm(asks, kind):
    assert (await create(asks, kind, action=GRANT))["error"]["code"] == "invalid_arguments"
    assert not writes(asks[2])


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "missing",
        "file",
        "slash",
        "home",
        "dotconfig",
        "ssh",
        "config",
        "state",
        "tokens",
        "ancestor",
        "config_child",
        "escape",
        "control",
    ],
)
async def test_ask_action_bad_root(asks, tmp_path, monkeypatch, case):
    core = asks[0]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for directory in (home / ".config", home / ".ssh", core.config.config_dir / "child"):
        directory.mkdir(exist_ok=True)
    file = tmp_path / "file"
    file.touch()
    escape = tmp_path / "escape"
    escape.symlink_to(core.config.config_dir, target_is_directory=True)
    values = {
        "relative": "relative",
        "missing": str(tmp_path / "missing"),
        "file": str(file),
        "slash": "/",
        "home": str(home),
        "dotconfig": str(home / ".config"),
        "ssh": str(home / ".ssh"),
        "config": str(core.config.config_dir),
        "state": str(core.config.state_dir),
        "tokens": str(core.config.token_dir),
        "ancestor": str(tmp_path),
        "config_child": str(core.config.config_dir / "child"),
        "escape": str(escape),
        "control": str(tmp_path / "bad\x00root"),
    }
    result = await create(asks, action={"type": "add_ask_media_root", "path": values[case]})
    assert result["error"]["code"] == "invalid_arguments"
    assert not writes(asks[2])


async def test_ask_action_yes_grants_and_reloads_policy(asks):
    core, state, _ = asks
    file = core.policy.path
    original = file.read_bytes()
    cap = core.registry.capabilities["ops_health"]
    assert not core.policy.allowed("agy", cap)
    ask = (await create(asks, action=GRANT, destructive=False))["ask"]
    assert ask["destructive"] is True
    summary = "Gives agy the ops:read permission"
    assert ask["action"] == {"type": "grant_scope", "summary": summary}
    stored = json.loads(core.board.ask_row(ask["id"])["spec"])["action"]
    assert stored == {**GRANT, "summary": summary}
    assert summary in state["comments"][1][0]["body"]
    answered = (await owner(asks, ask["id"], {"choice": ["yes"]}))["ask"]
    assert answered["action"]["result"] == "applied"
    assert answered["state"] == "answered"
    backup = file.with_name(f"policy.yaml.before-ask-{ask['id']}")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(file.stat().st_mode) == 0o600
    assert Policy(file).require("agy", cap)
    assert core.policy.require("agy", cap)  # Same instance, cached before replacement.
    assert not list(file.parent.glob(".*.ask-*"))
    assert summary in state["comments"][1][-1]["body"]
    assert "Applied" in state["comments"][1][-1]["body"]
    receipt = next(r for r in core.audit.tail() if r["outcome"] == "applied")
    assert receipt["client"] == "cortex" and receipt["args"]["ask_id"] == ask["id"]
    assert (await call(asks, "card", card="project#1"))["asks"][0]["action"] == answered["action"]
    assert (await call(asks, "list"))["answered"][0]["action"] == answered["action"]


@pytest.mark.parametrize("action_type", ["grant_scope", "add_ask_media_root"])
@pytest.mark.parametrize("withdraw", [False, True])
async def test_ask_action_no_or_withdraw_changes_nothing(asks, tmp_path, action_type, withdraw):
    core = asks[0]
    root = tmp_path / "media"
    root.mkdir()
    action = GRANT if action_type == "grant_scope" else {"type": action_type, "path": str(root)}
    originals = {p: p.read_bytes() for p in core.config.config_dir.glob("*.yaml")}
    live = dict(core.config.board)
    ask = (await create(asks, action=action))["ask"]
    result = (
        await call(asks, "ask_withdraw", ask_id=ask["id"])
        if withdraw
        else await owner(asks, ask["id"], {"choice": ["no"]})
    )
    assert "result" not in result["ask"]["action"]
    assert all(p.read_bytes() == contents for p, contents in originals.items())
    assert core.config.board == live
    assert not list(core.config.config_dir.glob("*.before-ask-*"))


@pytest.mark.parametrize("action_type", ["grant_scope", "add_ask_media_root"])
async def test_ask_action_second_apply_is_already(asks, tmp_path, action_type):
    core = asks[0]
    root = tmp_path / "media"
    root.mkdir()
    action = GRANT if action_type == "grant_scope" else {"type": action_type, "path": str(root)}
    first = (await create(asks, action=action))["ask"]
    second = (await create(asks, action=action))["ask"]
    assert (await owner(asks, first["id"], {"choice": ["yes"]}))["ask"]["action"][
        "result"
    ] == "applied"
    file = (
        core.policy.path if action_type == "grant_scope" else core.config.config_dir / "config.yaml"
    )
    identity = file.stat()
    result = await owner(asks, second["id"], {"choice": ["yes"]})
    assert result["ask"]["action"]["result"] == "already"
    assert file.stat() == identity
    stored = json.loads(core.board.ask_row(first["id"])["spec"])["action"]
    assert apply_action(core, first["id"], stored)["result"] == "already"
    assert file.stat() == identity
    assert len(list(file.parent.glob(file.name + ".before-ask-*"))) == 1


async def test_ask_action_media_root_enables_picture_immediately(asks, tmp_path):
    core = asks[0]
    root = tmp_path / "media"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    image = root / "preview.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nexample")
    file = core.config.config_dir / "config.yaml"
    file.write_text("port: 18801\nboard:\n  ask_media_roots: []\n")
    original = file.read_bytes()
    ask = (await create(asks, action={"type": "add_ask_media_root", "path": str(alias)}))["ask"]
    assert str(root.resolve()) in ask["action"]["summary"]
    result = await owner(asks, ask["id"], {"choice": ["yes"]})
    assert result["ask"]["action"]["result"] == "applied"
    assert core.config.board == {"ask_media_roots": [str(root.resolve())]}
    assert yaml.safe_load(file.read_text()) == {"port": 18801, "board": core.config.board}
    assert file.with_name(f"config.yaml.before-ask-{ask['id']}").read_bytes() == original
    assert Config.load().board == core.config.board
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert (await create(asks, pictures=[{"image": str(image)}]))["ask"]["pictures"][0][
        "id"
    ] == "pic1"
    assert str(root) not in core.audit.path.read_text()


async def test_ask_action_root_limit_and_revalidate_at_answer(asks, tmp_path):
    core = asks[0]
    roots = [tmp_path / f"root-{i}" for i in range(9)]
    for root in roots:
        root.mkdir()
    ask = (await create(asks, action={"type": "add_ask_media_root", "path": str(roots[8])}))["ask"]
    file = core.config.config_dir / "config.yaml"
    file.write_text(yaml.safe_dump({"board": {"ask_media_roots": [str(r) for r in roots[:8]]}}))
    original = file.read_bytes()
    result = await create(asks, action={"type": "add_ask_media_root", "path": str(roots[8])})
    assert result["error"]["code"] == "invalid_arguments"
    result = await owner(asks, ask["id"], {"choice": ["yes"]})
    assert result["ask"]["action"]["result"] == "failed"
    assert file.read_bytes() == original


@pytest.mark.parametrize("failure", ["invalid_policy", "replace", "backup_link"])
async def test_ask_action_failed_write_is_saved_safely(asks, tmp_path, monkeypatch, failure):
    from switchboard import ask_actions

    core = asks[0]
    ask = (await create(asks, action=GRANT))["ask"]
    file = core.policy.path
    original = file.read_bytes()
    if failure == "invalid_policy":
        loader = Policy.load

        def invalid_stage(self):
            if self.path != file:
                raise ValueError("PRIVATE exception path and contents")
            return loader(self)

        monkeypatch.setattr(Policy, "load", invalid_stage)
    elif failure == "replace":
        monkeypatch.setattr(ask_actions.os, "replace", Mock(side_effect=OSError("PRIVATE failure")))
    else:
        target = tmp_path / "unrelated"
        target.write_text("untouched")
        file.with_name(f"policy.yaml.before-ask-{ask['id']}").symlink_to(target)
    result = await owner(asks, ask["id"], {"choice": ["yes"]})
    assert result["ask"]["state"] == "answered"
    assert result["ask"]["action"]["result"] == "failed"
    assert file.read_bytes() == original
    assert not core.policy.allowed("agy", core.registry.capabilities["ops_health"])
    assert not list(file.parent.glob(".*.ask-*"))
    assert "PRIVATE" not in json.dumps(result) + core.audit.path.read_text()
    assert any(r["outcome"] == "failed" for r in core.audit.tail())
    if failure == "backup_link":
        assert target.read_text() == "untouched"


async def test_ask_action_path_cannot_be_redirected_after_approval(asks, tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    ask = (await create(asks, action={"type": "add_ask_media_root", "path": str(root)}))["ask"]
    root.rmdir()
    root.symlink_to(asks[0].config.config_dir, target_is_directory=True)
    result = await owner(asks, ask["id"], {"choice": ["yes"]})
    assert result["ask"]["action"]["result"] == "failed"
    assert asks[0].config.board["ask_media_roots"] == []


async def test_ask_action_authorization_and_post_answer_comment_failure(asks):
    core, state, _ = asks
    ask = (await create(asks, action=GRANT))["ask"]
    denied = await call(asks, "answer", ask_id=ask["id"], answer={"choice": ["yes"]})
    assert denied["error"]["code"] == "scope_denied"
    state["overrides"]["/repos/example/project/issues/1/comments"] = 503
    assert "error" in await owner(asks, ask["id"], {"choice": ["yes"]})
    stored = core.board.ask_object(core.board.ask_row(ask["id"]))
    assert stored["state"] == "answered" and stored["action"]["result"] == "applied"
    assert core.policy.require("agy", core.registry.capabilities["ops_health"])
    assert (await owner(asks, ask["id"], {"choice": ["yes"]}))["error"][
        "code"
    ] == "invalid_arguments"


def test_ask_action_edits_keep_comments_and_every_other_byte():
    from switchboard.ask_actions import insert_root, insert_scope

    policy = (
        "# who may do what\n"
        "clients:\n"
        "  claude:\n"
        "    scopes:\n"
        "    - board:read\n"
        "  cortex:  # the Server app\n"
        "    scopes:\n"
        "    - board:read\n"
        "    - ops:read  # health\n"
        "    resources:\n"
        "      repos: []\n"
    )
    edited = insert_scope(policy, "cortex", "activity:read")
    assert edited.count("\n") == policy.count("\n") + 1
    assert "# who may do what" in edited and "# the Server app" in edited and "# health" in edited
    import yaml

    data = yaml.safe_load(edited)
    assert data["clients"]["cortex"]["scopes"] == ["activity:read", "board:read", "ops:read"]
    assert data["clients"]["claude"]["scopes"] == ["board:read"]
    import pytest

    with pytest.raises(ValueError):
        insert_scope(policy, "nobody", "activity:read")

    config = "# private\nmonitor:\n  enabled: true\n"
    added = insert_root(config, "/tmp/pics")
    assert added.startswith(config)
    assert yaml.safe_load(added)["board"] == {"ask_media_roots": ["/tmp/pics"]}
    again = insert_root(added, "/tmp/more")
    assert yaml.safe_load(again)["board"]["ask_media_roots"] == ["/tmp/more", "/tmp/pics"]
    assert "# private" in again
    inline = insert_root("board:\n  ask_media_roots: []\n", "/tmp/pics")
    assert yaml.safe_load(inline) == {"board": {"ask_media_roots": ["/tmp/pics"]}}
    empty = insert_scope("clients:\n  cortex:\n    scopes: []\n", "cortex", "activity:read")
    assert yaml.safe_load(empty)["clients"]["cortex"]["scopes"] == ["activity:read"]


GRANTS = {
    "type": "grant_scopes",
    "grants": [{"client": "agy", "scope": "ops:read"}, {"client": "cortex", "scope": "jobs:read"}],
}


async def test_ask_action_grants_several_scopes_in_one_yes(asks):
    core, _, _ = asks
    file = core.policy.path
    original = file.read_bytes()
    ask = (await create(asks, action=GRANTS))["ask"]
    summary = "Gives agy ops:read; cortex jobs:read"
    assert ask["action"] == {"type": "grant_scopes", "summary": summary}
    answered = (await owner(asks, ask["id"], {"choice": ["yes"]}))["ask"]
    assert answered["action"]["result"] == "applied"
    clients = Policy(file).load()["clients"]
    assert "ops:read" in clients["agy"]["scopes"] and "jobs:read" in clients["cortex"]["scopes"]
    assert file.with_name(f"policy.yaml.before-ask-{ask['id']}").read_bytes() == original


@pytest.mark.parametrize(
    "grants",
    [
        [{"client": "agy", "scope": "ops:read"}],
        [{"client": "agy", "scope": "ops:read"}, {"client": "agy", "scope": "ops:read"}],
        [{"client": "agy", "scope": "ops:read"}, {"client": "agy", "scope": "board:answer"}],
        [{"client": "agy", "scope": "ops:read"}, {"client": "cortex", "scope": "ops:read"}],
        [{"client": "agy", "scope": "ops:read"}, {"client": "agy"}],
        [{"client": "agy", "scope": "ops:read"}] * 9,
    ],
)
async def test_ask_action_several_grants_refuses_bad_sets(asks, grants):
    result = await create(asks, action={"type": "grant_scopes", "grants": grants})
    assert result["error"]["code"] == "invalid_arguments"


def test_grant_summary_groups_clients_with_the_same_scopes():
    from switchboard.ask_actions import grant_summary

    grants = [{"client": "cortex", "scope": s} for s in ("elevate:read", "elevate:pair")] + [
        {"client": c, "scope": s}
        for c in ("claude", "codex", "agy")
        for s in ("elevate:read", "elevate:request")
    ]
    assert grant_summary(grants) == (
        "Gives cortex elevate:read and elevate:pair; "
        "claude, codex and agy elevate:read and elevate:request"
    )
