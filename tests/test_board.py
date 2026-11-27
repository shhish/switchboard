"""Board contracts against a fake HTTP Gitea only; no owner service or config."""

import asyncio
import copy
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
from aiohttp import web

from switchboard import gitea
from switchboard.board import Board
from switchboard.config import Config
from switchboard.domains.board import COLUMNS
from switchboard.errors import CapabilityError

from .conftest import change_policy, running_app


def issue(number=1, repo="project", **extra):
    return {
        "id": 1000 + number,
        "number": number,
        "title": f"Work {number}",
        "body": "Card body",
        "state": "open",
        "updated_at": f"2026-11-18T10:{number % 60:02}:00Z",
        "created_at": "2026-11-01T00:00:00Z",
        "html_url": f"https://gitea.example/example/{repo}/issues/{number}",
        "repository": {"name": repo, "full_name": "example/" + repo, "owner": {"login": "example"}},
        "pull_request": None,
        "labels": [{"name": "feature"}],
        "comments": 0,
        **extra,
    }


@pytest.fixture
async def board_api(core, config):
    credential = "fake" + "-board-credential"
    token = config.config_dir / "gitea.secret"
    token.write_text(credential)
    token.chmod(0o600)
    change_policy(
        config,
        lambda p: (
            p["clients"]["codex"]["scopes"].extend(["board:read", "board:write"]),
            p["allowlists"].update(git={"write_repos": ["example/project", "example/workspace"]}),
        ),
    )
    state = {
        "projects": [{"id": 19, "title": "Slate", "html_url": "https://gitea.example/projects/19"}],
        "columns": [
            {"id": 31 + i, "title": name, "sorting": i, "default": i == 0}
            for i, name in enumerate(COLUMNS)
        ],
        "issues": {1: issue()},
        "members": {1001: "Ideas"},
        "comments": {},
        "overrides": {},
    }
    calls = []

    def paged(request, rows):
        page, limit = int(request.query.get("page", 1)), int(request.query.get("limit", 50))
        assert page >= 1 and 1 <= limit <= 50
        return rows[(page - 1) * limit : page * limit]

    async def handle(request):
        assert request.headers["Authorization"] == "token " + credential
        path = request.path.removeprefix("/api/v1")
        payload = await request.json() if request.can_read_body else None
        calls.append((request.method, path, payload, dict(request.query)))
        if path in state["overrides"]:
            value = state["overrides"][path]
            if isinstance(value, int):
                return web.Response(status=value, text="private backend text")
            return web.json_response(value)
        if path == "/user/projects":
            assert request.query["state"] == "open"
            return web.json_response(paged(request, state["projects"]))
        if path == "/user/projects/19/columns":
            return web.json_response(paged(request, state["columns"]))
        match = re.fullmatch(r"/user/projects/19/columns/(\d+)/issues(?:/(\d+))?", path)
        if match:
            column_id = int(match[1])
            name = next(c["title"] for c in state["columns"] if c["id"] == column_id)
            if request.method == "GET":
                rows = [
                    i for i in state["issues"].values() if state["members"].get(i["id"]) == name
                ]
                return web.json_response(paged(request, rows))
            assert request.method == "POST" and payload is None
            state["members"][int(match[2])] = name
            return web.Response(status=201)
        match = re.fullmatch(r"/user/projects/19/issues/(\d+)/move", path)
        if match:
            assert request.method == "POST" and set(payload) == {"column_id"}
            state["members"][int(match[1])] = next(
                c["title"] for c in state["columns"] if c["id"] == payload["column_id"]
            )
            return web.Response(status=204)
        match = re.fullmatch(r"/repos/example/([^/]+)/issues(?:/(\d+))?(/comments)?", path)
        if not match:
            return web.Response(status=404)
        repo, raw_number, comments_path = match.groups()
        if raw_number is None:
            assert request.method == "POST" and set(payload) == {"title", "body"}
            number = max(state["issues"], default=0) + 1
            created = issue(number, repo, **payload)
            state["issues"][number] = created
            return web.json_response(created, status=201)
        number = int(raw_number)
        row = state["issues"].get(number)
        if row is None or row["repository"]["name"] != repo:
            return web.Response(status=404)
        if comments_path:
            comments = state["comments"].setdefault(number, [])
            if request.method == "GET":
                return web.json_response(paged(request, comments))
            assert request.method == "POST" and set(payload) == {"body"}
            comment = {
                "id": len(comments) + 1,
                "body": payload["body"],
                "user": {"login": "service"},
                "created_at": "2026-11-18T10:00:00Z",
            }
            comments.append(comment)
            row["comments"] += 1
            row["updated_at"] = "2026-11-19T12:00:00Z"
            return web.json_response(comment, status=201)
        if request.method == "PATCH":
            assert set(payload) == {"state"} and payload["state"] in ("open", "closed")
            row.update(payload)
        else:
            assert request.method == "GET"
        return web.json_response(row)

    app = web.Application()
    app.router.add_route("*", "/api/v1/{path:.*}", handle)
    async with running_app(app) as url:
        config.backends["gitea"].update(
            url=url + "/api/v1", enabled=True, owner="example", token_file=str(token)
        )
        yield core, state, calls


async def call(api, name, **args):
    result = await api[0].call("codex", "board_" + name, args)
    jsonschema.validate(
        result, api[0].registry.capabilities["board_" + name].advertised_output_schema()
    )
    return result


def grouped(result):
    return {c["title"]: c for c in result["columns"]}


def writes(calls):
    return [c for c in calls if c[0] != "GET"]


async def test_list_grouping_done_and_no_per_card_calls(board_api):
    core, state, calls = board_api
    state["issues"].update({2: issue(2, state="closed"), 3: issue(3), 4: issue(4), 5: issue(5)})
    state["members"].update({1002: "In progress", 1003: "Needs you", 1004: "Up next", 1005: "Done"})
    result = await call(board_api, "list")
    groups = grouped(result)
    assert list(groups) == list(COLUMNS[:-1])
    assert groups["In progress"]["count"] == 0
    assert result["needs_you"] == 1 and not result["truncated"]
    assert groups["Ideas"]["cards"][0]["claimed_by"] is None
    assert len(calls) == 7  # discovery + five column requests
    calls.clear()
    result = await call(board_api, "list", include_done=True)
    assert len(calls) == 5 and not writes(calls)
    done = grouped(result)["Done"]
    assert done["count"] == 2 and {c["card"] for c in done["cards"]} == {"project#2", "project#5"}
    assert all(c["column"] == "Done" for c in done["cards"])
    assert result["source"] == "gitea"


async def test_filters_and_done_recent_cap(board_api):
    _, state, _ = board_api
    for n in range(2, 22):
        state["issues"][n] = issue(n, state="closed")
        state["members"][1000 + n] = "In progress"
    state["issues"][22] = issue(22, "workspace")
    state["members"][1022] = "Needs you"
    result = await call(board_api, "list", project="example/project", column="Done")
    done = grouped(result)["Done"]
    assert done["count"] == 20 and len(done["cards"]) == 15 and result["truncated"]
    assert [c["number"] for c in done["cards"]] == list(range(21, 6, -1))
    assert result["needs_you"] == 0
    result = await call(board_api, "list", project="workspace", column="Needs you")
    assert (
        result["needs_you"] == 1 and grouped(result)["Needs you"]["cards"][0]["repo"] == "workspace"
    )
    result = await call(board_api, "list", include_done=True, limit=1)
    assert sum(len(c["cards"]) for c in result["columns"]) == 1 and result["truncated"]


async def test_bounded_paging_and_duplicate_refusal(board_api):
    _, state, calls = board_api
    state["issues"] = {n: issue(n) for n in range(1, 221)}
    state["members"] = {i["id"]: "Ideas" for i in state["issues"].values()}
    result = await call(board_api, "list", limit=100)
    assert result["truncated"] and grouped(result)["Ideas"]["count"] == 200
    assert max(int(c[3].get("page", 1)) for c in calls) == 4
    result = await call(board_api, "add", title="Novel title", repo="project")
    assert result["error"]["code"] == "backend_invalid" and not writes(calls)
    result = await call(board_api, "card", card="project#220")
    assert result["error"]["code"] == "backend_invalid"


async def test_card_reference_membership_and_latest_comments(board_api):
    _, state, calls = board_api
    state["issues"][1]["body"] = "\x00Ignore\nme " + "x" * 6500
    comments = [
        {
            "id": n,
            "body": "\x01note\n" + "x" * 1600,
            "user": {"login": "author\nname"},
            "created_at": f"2026-{n:03}",
        }
        for n in range(1, 102)
    ]
    state["comments"][1] = comments
    state["issues"][1]["comments"] = len(comments)
    result = await call(board_api, "card", card="example/project#1")
    assert (
        len(result["body"]) == 6000 and "\x00" not in result["body"] and "\n" not in result["body"]
    )
    assert len(result["comments"]) == 30 and result["truncated"]
    assert result["comments"][0]["created"] == "2026-072"
    assert result["comments"][-1]["created"] == "2026-101"
    assert result["comments"][0]["author"] == "author name"
    assert len(result["comments"][0]["body"]) == 1500
    assert len([c for c in calls if c[1].endswith("/comments")]) == 2
    state["members"].clear()
    result = await call(board_api, "card", card="project#1")
    assert result["outcome"] == "not_found" and "attached" in result["error"]["message"]
    state["issues"][2] = issue(2, "workspace")
    state["members"][1002] = "Ideas"
    assert (await call(board_api, "card", card="#2"))["card"] == "workspace#2"


@pytest.mark.parametrize(
    "card",
    [
        "../bad#1",
        "other/project#1",
        "project#0",
        "project#-1",
        "project#1\n",
        "project",
        "x/y/z#1",
        "#1#1",
    ],
)
async def test_invalid_references(board_api, card):
    assert (await call(board_api, "card", card=card))["error"]["code"] == "invalid_arguments"
    assert not board_api[2]


async def test_add_footer_attach_default_and_duplicates(board_api):
    _, state, calls = board_api
    result = await call(board_api, "add", title="  WORK \t1 ", repo="project")
    assert result["duplicate"] and result["card"] == "project#1" and not writes(calls)
    result = await call(board_api, "add", title="New work", body="Details", column="Up next")
    assert result["repo"] == "workspace" and not result["duplicate"]
    assert result["last_actor"] == "codex" and result["last_note"] is None
    created = state["issues"][result["number"]]
    assert created["body"] == "Details\n\n_Added by codex via Switchboard._"
    assert state["members"][created["id"]] == "Up next"
    assert writes(calls)[-1][1] == f"/user/projects/19/columns/32/issues/{created['id']}"
    assert writes(calls)[-1][2] is None
    result = await call(board_api, "add", title="New work", repo="project")
    assert result["repo"] == "project" and not result["duplicate"]  # same title, different repo
    state["issues"][1]["state"] = "closed"
    assert not (await call(board_api, "add", title="Work 1", repo="project"))["duplicate"]
    result = await call(board_api, "add", title="Default")
    assert result["column"] == "Ideas"


async def test_concurrent_adds_share_duplicate_guard(board_api):
    results = await asyncio.gather(
        *(call(board_api, "add", title="One shared item") for _ in range(2))
    )
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert len(board_api[1]["issues"]) == 2


@pytest.mark.parametrize(
    "args",
    [
        {"title": "Done", "column": "Done"},
        {"title": " "},
        {"title": "x" * 201},
        {"title": "No", "repo": "other/project"},
    ],
)
async def test_add_invalid_no_writes(board_api, args):
    assert (await call(board_api, "add", **args))["error"]["code"] == "invalid_arguments"
    assert not writes(board_api[2])


async def test_move_claim_close_reopen_and_attributed_note(board_api):
    core, state, calls = board_api
    result = await call(
        board_api, "move", card="project#1", column="In progress", note="Building it"
    )
    assert result["claimed_by"] == "codex" and result["last_actor"] == "codex"
    assert result["updated"] == state["issues"][1]["updated_at"] == "2026-11-19T12:00:00Z"
    assert result["last_note"] == "Building it"
    assert state["comments"][1][0]["body"] == "**codex** moved this to In progress: Building it"
    result = await call(board_api, "move", card="project#1", column="Done")
    assert result["state"] == "closed" and state["issues"][1]["state"] == "closed"
    assert state["members"][1001] == "Done"
    assert result["last_note"] == "Building it"
    result = await call(
        board_api,
        "move",
        card="project#1",
        column="Needs you",
        note="Accept the agreement at example.com",
    )
    assert result["state"] == "open" and result["column"] == "Needs you"
    assert (
        state["comments"][1][-1]["body"]
        == "**codex** moved this to Needs you: Accept the agreement at example.com"
    )
    assert [c[2] for c in calls if c[0] == "PATCH"] == [{"state": "closed"}, {"state": "open"}]
    # A fresh workflow object sees durable receipts; no comment fetch during list.
    core.board = Board(core)
    result = await call(board_api, "list", column="Needs you")
    # Done ended the claim; reopening does not bring it back.
    assert grouped(result)["Needs you"]["cards"][0]["claimed_by"] is None
    row = core.storage.db.execute("SELECT * FROM board_cards").fetchone()
    assert row["issue_id"] == 1001 and row["last_at"]


@pytest.mark.parametrize("note", [None, "", " \n\t", "\x00"])
async def test_needs_you_requires_exact_ask_note(board_api, note):
    args = {} if note is None else {"note": note}
    result = await call(board_api, "move", card="project#1", column="Needs you", **args)
    assert result["error"]["code"] == "invalid_arguments" and not board_api[2]


async def test_comment_attribution_and_last_note(board_api):
    core, state, calls = board_api
    result = await call(board_api, "comment", card="project#1", body="Progress\n" + "x" * 300)
    assert result["comment_id"] == 1 and result["last_actor"] == "codex"
    assert result["updated"] == state["issues"][1]["updated_at"] == "2026-11-19T12:00:00Z"
    assert len(result["last_note"]) == 200 and "\n" not in result["last_note"]
    assert state["comments"][1][0]["body"].startswith("**codex**: Progress\n")
    detail = await call(board_api, "card", card="project#1")
    assert detail["last_note"] == result["last_note"]
    # Both client spoofing and free-text audit leakage are rejected/prevented.
    result = await call(board_api, "comment", card="project#1", body="Hi", client="owner")
    assert result["error"]["code"] == "invalid_arguments"
    log = (core.config.log_dir / "audit.jsonl").read_text()
    assert "x" * 100 not in log


@pytest.mark.parametrize(
    "name,args",
    [
        ("add", {"title": "No", "repo": "project"}),
        ("move", {"card": "project#1", "column": "Done"}),
        ("comment", {"card": "project#1", "body": "No"}),
    ],
)
async def test_allowlist_denial_before_io(board_api, name, args):
    core, _, calls = board_api
    change_policy(core.config, lambda p: p["allowlists"]["git"].update(write_repos=[]))
    result = await call(board_api, name, **args)
    assert result["error"]["code"] == "resource_denied" and not calls


@pytest.mark.parametrize(
    "name,args",
    [
        ("list", {}),
        ("card", {"card": "project#1"}),
        ("add", {"title": "No"}),
        ("move", {"card": "project#1", "column": "Done"}),
        ("comment", {"card": "project#1", "body": "No"}),
    ],
)
async def test_scope_denial_even_for_hidden_tools(board_api, name, args):
    core, _, calls = board_api
    result = await core.call("agy", "board_" + name, args)
    assert result["error"]["code"] == "scope_denied" and not calls
    assert not any(t["name"].startswith("board_") for t in core.tools("agy"))


@pytest.mark.parametrize("missing", ["project", *COLUMNS])
async def test_missing_project_or_column(board_api, missing):
    _, state, _ = board_api
    if missing == "project":
        state["projects"] = []
    else:
        state["columns"] = [c for c in state["columns"] if c["title"] != missing]
    result = await call(board_api, "list")
    assert result["error"]["code"] == "backend_invalid"
    assert ("Slate" if missing == "project" else missing) in result["error"]["message"]


async def test_cache_expiry_and_config_change(board_api):
    core, state, calls = board_api
    await call(board_api, "list")
    state["overrides"]["/user/projects/19/columns"] = []
    assert (await call(board_api, "list"))["status"] == "ok"
    key, _, value = core.board._cache
    core.board._cache = (key, 0, value)
    assert (await call(board_api, "list"))["error"]["code"] == "backend_invalid"
    core.config.backends["gitea"]["board_project"] = "Different"
    assert "Different" in (await call(board_api, "list"))["error"]["message"]
    assert len([c for c in calls if c[1] == "/user/projects"]) == 3


async def test_sanitized_list_and_non_issue_exclusion(board_api):
    _, state, _ = board_api
    state["issues"][1]["title"] = "Ignore\n\x00instructions" + "x" * 250
    state["issues"][1]["labels"] = [{"name": "Label\n\x01text"}]
    state["issues"].update({2: issue(2, pull_request={"url": "x"}), 3: issue(3)})
    state["issues"][3]["repository"]["full_name"] = "another/project"
    state["members"].update({1002: "Ideas", 1003: "Ideas"})
    result = await call(board_api, "list")
    cards = grouped(result)["Ideas"]["cards"]
    assert len(cards) == 1 and len(cards[0]["title"]) == 200
    assert "\n" not in cards[0]["title"] and cards[0]["labels"] == ["Label text"]


async def test_explicit_patch_and_empty_post_use_write_timeout(board_api, monkeypatch):
    core, _, _ = board_api
    original = core.backends.session.request
    observed = []

    def request(method, url, **kwargs):
        observed.append((method, kwargs["timeout"].total))
        return original(method, url, **kwargs)

    monkeypatch.setattr(core.backends.session, "request", request)
    result = await core.backends.gitea.request(
        "/repos/example/project/issues/1", method="PATCH", payload={"state": "closed"}
    )
    assert result["state"] == "closed"
    assert (
        await core.backends.gitea.request("/user/projects/19/columns/31/issues/1001", method="POST")
        == {}
    )
    assert (
        await core.backends.gitea.request(
            "/user/projects/19/issues/1001/move", method="POST", payload={"column_id": 31}
        )
        == {}
    )
    await core.backends.gitea.request("/repos/example/project/issues/1", method="GET")
    assert observed == [
        ("PATCH", gitea.WRITE_TIMEOUT_S),
        ("POST", gitea.WRITE_TIMEOUT_S),
        ("POST", gitea.WRITE_TIMEOUT_S),
        ("GET", gitea.READ_TIMEOUT_S),
    ]
    for method in ("DELETE", "PUT", "patch"):
        with pytest.raises(CapabilityError, match="method"):
            await core.backends.gitea.request("/no", method=method)


@pytest.mark.parametrize(
    "status,code", [(403, "backend_error"), (404, "not_found"), (500, "backend_error")]
)
async def test_patch_error_mapping_no_backend_text(board_api, status, code):
    core, state, _ = board_api
    state["overrides"]["/repos/example/project/issues/1"] = status
    with pytest.raises(CapabilityError) as caught:
        await core.backends.gitea.request(
            "/repos/example/project/issues/1", method="PATCH", payload={"state": "closed"}
        )
    assert caught.value.code == code and "private" not in str(caught.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("board_project", ""),
        ("board_project", "\nBad"),
        ("board_project", 1),
        ("board_general_repo", "owner/repo"),
        ("board_general_repo", "../repo"),
        ("board_general_repo", "repo\n"),
    ],
)
def test_board_settings_validation(key, value):
    with pytest.raises(ValueError):
        gitea.validate_settings({"url": "http://127.0.0.1:1/api/v1", key: value})


def test_config_load_board_settings(config):
    settings = gitea.validate_settings({"url": "http://127.0.0.1:1/api/v1"})
    assert settings["board_project"] == "Slate" and settings["board_general_repo"] == "workspace"
    (config.config_dir / "config.yaml").write_text(
        "backends:\n  gitea:\n    board_project: Work\n    board_general_repo: general\n"
    )
    loaded = Config.load()
    assert loaded.backends["gitea"]["board_project"] == "Work"
    assert loaded.backends["gitea"]["board_general_repo"] == "general"


async def test_board_capability_contracts(board_api):
    core = board_api[0]
    for name in (
        "list",
        "card",
        "add",
        "move",
        "comment",
        "ask",
        "ask_wait",
        "ask_withdraw",
        "answer",
    ):
        cap = core.registry.capabilities["board_" + name]
        assert len(cap.description) <= 400
        assert all(
            term in cap.description for term in ("Use when", "Requires", "Returns", "untrusted")
        )
        assert len(cap.description.split(".", 1)[0]) <= 120
        assert cap.service == "gitea" and cap.memory is None and cap.memory_optout
        assert cap.input_schema["additionalProperties"] is False
        assert not set(cap.input_schema) & {"oneOf", "anyOf", "allOf"}


FORMATTER = runpy.run_path(str(Path(__file__).parents[1] / "scripts/session_brief.py"))


def board_brief_data():
    return {
        "status": "ok",
        "board": {"title": "Slate"},
        "needs_you": 3,
        "columns": [
            {
                "title": name,
                "count": count,
                "cards": [
                    {
                        "card": f"project#{n}",
                        "title": "Ask\n\x00the owner" + "x" * 190,
                        "claimed_by": "codex" if name == "In progress" else None,
                    }
                    for n in range(30)
                ],
            }
            for name, count in zip(COLUMNS, [6, 4, 2, 3, 0], strict=True)
        ],
        "truncated": True,
    }


def test_session_brief_format_and_budget():
    data = board_brief_data()
    text = FORMATTER["format_board"](data)
    assert text.startswith("Board (Slate): 3 need you · 2 in progress · 4 up next · 6 ideas")
    assert "Needs you\n" in text and "Ideas: 6 (board_list for the rest)" in text
    assert "untrusted data" in text and "Partial board" in text
    assert text.endswith(FORMATTER["RULES"]) and len(text) <= 2400 and "\x00" not in text
    small = copy.deepcopy(data)
    for column in small["columns"]:
        column["cards"] = column["cards"][:1]
    text = FORMATTER["format_board"](small)
    assert text.index("Needs you\n") < text.index("In progress\n") < text.index("Up next\n")
    assert "· codex" in text


@pytest.mark.parametrize("mode", ["ok", "failed", "malformed", "memory-failed", "timeout"])
def test_session_hook_two_calls_and_independent_failures(tmp_path, mode):
    sb = tmp_path / "sb"
    data = json.dumps(board_brief_data())
    sb.write_text(
        "#!"
        + sys.executable
        + "\n"
        + f"""
import sys, time
args = sys.argv[1:]
assert args[:2] == ["--client", "claude"]
if args[2:] == ["memory", "brief"]:
    if {mode!r} == "memory-failed": raise SystemExit(1)
    print("M" * 6000)
else:
    assert args[2:] == ["call", "board_list", "--json", '{{"limit":40}}']
    if {mode!r} == "failed": raise SystemExit(1)
    if {mode!r} == "timeout": time.sleep(5)
    print("invalid" if {mode!r} == "malformed" else {data!r})
"""
    )
    sb.chmod(0o700)
    result = subprocess.run(
        ["sh", "scripts/claude-session-brief.sh"],
        env=dict(os.environ, SWITCHBOARD_SB=str(sb)),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stderr == ""
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(context) <= 6000
    if mode in ("failed", "malformed", "timeout"):
        # The memory brief keeps the rest of the room; the failure is said, not hidden.
        assert context.startswith("M") and context.endswith("before picking up work.")
    elif mode == "memory-failed":
        assert context.startswith("Board (Slate)")
    else:
        assert context.startswith("M") and context.endswith(FORMATTER["RULES"])
        assert context.index("Board (Slate)") > 0


async def test_claim_receipt_survives_later_comment_failure(board_api):
    _, state, _ = board_api
    state["overrides"]["/repos/example/project/issues/1/comments"] = 503
    result = await call(board_api, "move", card="project#1", column="In progress", note="Starting")
    assert result["error"]["code"] == "backend_error"
    assert state["members"][1001] == "In progress"
    result = await call(board_api, "list", column="In progress")
    card = grouped(result)["In progress"]["cards"][0]
    assert card["claimed_by"] == "codex" and card["last_actor"] == "codex"
    assert card["last_note"] is None  # The note was not posted.


async def test_patch_keeps_response_size_limit(board_api, monkeypatch):
    core, _, _ = board_api
    monkeypatch.setattr(gitea, "MAX_BODY", 32)
    with pytest.raises(CapabilityError) as caught:
        await core.backends.gitea.request(
            "/repos/example/project/issues/1", method="PATCH", payload={"state": "closed"}
        )
    assert caught.value.code == "backend_invalid" and "limit" in str(caught.value)


async def test_leaving_in_progress_releases_the_claim(board_api):
    result = await call(board_api, "move", card="project#1", column="In progress")
    assert result["claimed_by"] == "codex"
    result = await call(board_api, "move", card="project#1", column="Up next", note="Paused")
    assert result["claimed_by"] is None
