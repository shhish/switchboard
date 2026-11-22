"""Structured Slate asks use fake Gitea and temporary SQLite/policy only."""

# ruff: noqa: F811
import asyncio
import json
from datetime import UTC, datetime, timedelta

import jsonschema
import pytest

from switchboard.board import Board

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
        assert ask["options"][0] == {"id": "yes", "label": "Proceed", "detail": None}
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
    ):
        assert private not in log and private not in db_args
    records = core.audit.tail(30)
    choice = next(r for r in records if r["capability"] == "board_ask" and "options" in r["args"])
    assert set(choice["args"]["options"]) == {"len", "sha256"}
    form = next(r for r in records if "fields" in (r.get("args") or {}))
    assert set(form["args"]["fields"]) == {"len", "sha256"}
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
    for name in ("ask", "ask_withdraw", "ask_wait", "answer"):
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
    heading = "Answered by the owner (act on these; board_card for the full answer)"
    assert heading in rendered and "Select value? → Use the default · asked by codex" in rendered
    assert "Self answer" not in rendered and len(rendered) <= 1500
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
    assert "q" * 81 not in rendered and "a" * 121 not in rendered and len(rendered) <= 1500


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
