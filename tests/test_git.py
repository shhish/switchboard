"""Git tools use only fake Gitea HTTP and temporary real Git repositories."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from aiohttp import web

from switchboard import gitea, hostcmd
from switchboard.config import Config, validate_git
from switchboard.git import Git

from .conftest import change_policy, running_app

A, B, C = (c * 40 for c in "abc")


def pull(number=1):
    return {
        "number": number,
        "title": "A change",
        "body": "Review this",
        "user": {"login": "developer"},
        "head": {"ref": "feature", "sha": A},
        "base": {"ref": "main", "sha": B},
        "draft": False,
        "mergeable": True,
        "merged": False,
        "state": "open",
        "updated_at": f"2026-11-06T10:00:{number % 60:02}Z",
    }


def run(run_id=10, commit=A, status="completed", conclusion="success"):
    return {
        "id": run_id,
        "name": "CI",
        "status": status,
        "conclusion": conclusion,
        "head_branch": "feature",
        "head_sha": commit,
        "event": "push",
        "path": ".gitea/workflows/ci.yaml",
    }


@pytest.fixture
async def git_api(core, config):
    token = config.config_dir / "gitea.secret"
    credential = "fake" + "-gitea-credential"
    token.write_text(credential)
    token.chmod(0o600)
    change_policy(
        config,
        lambda p: (
            p["clients"]["codex"]["scopes"].extend(["git:read", "git:write"]),
            p["allowlists"].update(git={"write_repos": ["example/project"]}),
        ),
    )
    state = {
        "repos": [
            {
                "full_name": "example/project",
                "default_branch": "main",
                "open_pr_counter": 1,
                "updated_at": "2026-11-06",
            }
        ],
        "pr": pull(),
        "prs": [pull()],
        "runs": [run()],
        "combined": {
            "state": "success",
            "total_count": 1,
            "statuses": [{"context": "tests", "status": "success", "description": "green"}],
        },
        "files": [{"filename": "src/code.py", "additions": 3, "deletions": 1}],
        "commits": [{"sha": A, "commit": {"message": "Fix code"}}],
        "reviews": [{"state": "APPROVED", "user": {"login": "reviewer"}}],
        "jobs": [{"id": 7, "name": "Test", "status": "completed", "conclusion": "success"}],
        "log": "test passed\n",
        "branch_sha": A,
        "tags": {},
        "overrides": {},
    }
    calls = []

    def paged(request, rows):
        page, limit = int(request.query.get("page", 1)), int(request.query.get("limit", 50))
        assert 1 <= page <= 4 and 1 <= limit <= 50
        return rows[(page - 1) * limit : page * limit]

    async def handle(request):
        assert request.headers.get("Authorization") == "token " + credential
        path = request.path.removeprefix("/api/v1")
        payload = await request.json() if request.method == "POST" else None
        calls.append(
            {
                "method": request.method,
                "path": path,
                "query": dict(request.query),
                "payload": payload,
            }
        )
        if path in state["overrides"]:
            response = state["overrides"][path]
            if isinstance(response, int):
                return web.Response(
                    status=response,
                    text="private backend error",
                    headers={"Location": request.path + "?redirected=yes"}
                    if response == 302
                    else None,
                )
            if isinstance(response, str):
                return web.Response(text=response)
            return web.json_response(response)
        if path == "/repos/search":
            return web.json_response({"data": paged(request, state["repos"])})
        prefix = "/repos/example/project"
        if not path.startswith(prefix):
            return web.Response(status=404)
        suffix = path[len(prefix) :]
        if request.method == "POST":
            if suffix == "/pulls":
                assert set(payload) == {"head", "base", "title", "body"}
                return web.json_response(state["pr"], status=201)
            if suffix == "/issues/1/comments":
                assert set(payload) == {"body"}
                return web.json_response({"id": 51, "body": payload["body"]}, status=201)
            if suffix == "/pulls/1/merge":
                assert set(payload) == {"Do", "delete_branch_after_merge", "head_commit_id"}
                if payload["head_commit_id"] != state["pr"]["head"]["sha"]:
                    return web.Response(status=409)
                state["pr"].update(merged=True, state="closed", merge_commit_sha=B)
                return web.Response(status=200)
            if suffix in ("/actions/runs/10/rerun", "/actions/runs/10/rerun-failed-jobs"):
                assert payload == {}
                return web.Response(status=204)
            if suffix == "/tags":
                assert set(payload) == {"tag_name", "target", "message"}
                if payload["tag_name"] in state["tags"]:
                    return web.Response(status=409)
                state["tags"][payload["tag_name"]] = payload
                return web.json_response(payload, status=201)
            pytest.fail("Unexpected Gitea mutation: " + suffix)
        assert request.method == "GET"
        if not suffix:
            return web.json_response(state["repos"][0])
        if suffix.startswith("/branches/"):
            return web.json_response({"name": suffix[10:], "commit": {"id": state["branch_sha"]}})
        if suffix == "/pulls":
            return web.json_response(paged(request, state["prs"]))
        if suffix == "/pulls/1":
            return web.json_response(state["pr"])
        for name in ("files", "commits", "reviews"):
            if suffix == "/pulls/1/" + name:
                return web.json_response(paged(request, state[name]))
        if suffix.startswith("/commits/") and suffix.endswith("/status"):
            commit = suffix.split("/")[2]
            return web.json_response({"sha": state.get("status_sha", commit), **state["combined"]})
        if suffix == "/actions/runs":
            rows = state["runs"]
            if "head_sha" in request.query:
                rows = [r for r in rows if r["head_sha"] == request.query["head_sha"]]
            if "branch" in request.query:
                rows = [r for r in rows if r["head_branch"] == request.query["branch"]]
            if "status" in request.query:
                rows = [
                    r for r in rows if request.query["status"] in (r["status"], r["conclusion"])
                ]
            result = {"workflow_runs": paged(request, rows)}
            if state.get("race"):
                state["pr"]["head"]["sha"] = C
            return web.json_response(result)
        if suffix == "/actions/runs/10":
            return web.json_response(run())
        if suffix.endswith("/jobs"):
            return web.json_response({"jobs": paged(request, state["jobs"])})
        if suffix == "/actions/jobs/7/logs":
            return web.Response(text=state["log"])
        if suffix.startswith("/tags/"):
            tag = suffix.removeprefix("/tags/")
            return (
                web.json_response(state["tags"][tag])
                if tag in state["tags"]
                else web.Response(status=404)
            )
        pytest.fail("Unexpected Gitea read: " + suffix)

    app = web.Application()
    app.router.add_route("*", "/api/v1/{path:.*}", handle)
    async with running_app(app) as url:
        config.backends["gitea"].update(
            url=url + "/api/v1", enabled=True, owner="example", token_file=str(token)
        )
        yield core, state, calls


async def call(api, name, **args):
    return await api[0].call("codex", "git_" + name, args)


def writes(api):
    return [c for c in api[2] if c["method"] == "POST"]


async def test_read_tools(git_api):
    core, state, calls = git_api
    repos = await call(git_api, "repos", query="project")
    assert repos["repos"][0] == {
        "repo": "example/project",
        "default_branch": "main",
        "open_prs": 1,
        "updated": "2026-11-06",
        "ci_state": "success",
    }
    assert repos["source"] == "gitea"
    assert calls[0]["query"]["q"] == "project"
    for args in ({}, {"repo": "project", "state": "all", "limit": 1}):
        result = await call(git_api, "prs", **args)
        assert result["prs"][0]["ci_state"] == "success"
        assert result["prs"][0]["head_sha"] == A
    detail = await call(git_api, "pr", repo="example/project", number=1)
    assert detail["pr"]["files"][0]["additions"] == 3
    assert detail["pr"]["commits"][0]["sha"] == A[:12]
    assert detail["pr"]["reviews"][0]["state"] == "APPROVED"
    assert detail["pr"]["ci"]["runs"][0]["id"] == 10
    runs = await call(git_api, "ci_runs", repo="project", branch="feature", status="completed")
    assert runs["runs"][0]["jobs"][0]["id"] == 7
    assert not writes(git_api)
    assert core.audit.tail()[-1]["capability"] == "git_ci_runs"


async def test_writes_routes_defaults_and_receipts(git_api):
    result = await call(
        git_api, "pr_create", repo="project", head="feature", title="A change", body="details"
    )
    assert result["pr"]["number"] == 1
    assert writes(git_api)[-1]["payload"]["base"] == "main"
    assert (await call(git_api, "pr_comment", repo="project", number=1, body="Reviewed"))[
        "comment_id"
    ] == 51
    for failed in (True, False):
        result = await call(git_api, "ci_rerun", repo="project", run_id=10, failed_only=failed)
        assert result["outcome"] == "accepted"
        assert writes(git_api)[-1]["path"].endswith("rerun-failed-jobs" if failed else "rerun")
    result = await call(git_api, "pr_merge", repo="project", number=1)
    assert result["merged_sha"] == B
    assert writes(git_api)[-1]["payload"] == {
        "Do": "merge",
        "delete_branch_after_merge": True,
        "head_commit_id": A,
    }
    result = await call(git_api, "tag_create", repo="project", tag="v1.2.3", message="Release")
    assert result["sha"] == A
    assert writes(git_api)[-1]["payload"] == {
        "tag_name": "v1.2.3",
        "target": A,
        "message": "Release",
    }
    result = await call(git_api, "tag_create", repo="project", tag="v1.2.3", message="Release")
    assert result["error"]["code"] == "git_tag_denied"


@pytest.mark.parametrize("state", ["pending", "failure", "success", "no_checks", "unknown"])
@pytest.mark.parametrize("tool", ["pr_merge", "tag_create"])
async def test_ci_gates(git_api, state, tool):
    _, data, _ = git_api
    data["runs"] = []
    data["combined"] = {
        "state": "pending" if state == "no_checks" else state,
        "total_count": 0 if state == "no_checks" else 1,
        "statuses": [] if state == "no_checks" else [{"context": "ci", "status": state}],
    }
    args = {
        "repo": "project",
        **({"number": 1} if tool == "pr_merge" else {"tag": "v2.0.0", "message": "Release"}),
    }
    result = await call(git_api, tool, **args)
    if state == "success":
        assert result["status"] == "done"
        assert len(writes(git_api)) == 1
    else:
        assert result["error"]["code"] == "git_ci_denied"
        assert not writes(git_api)


@pytest.mark.parametrize("state", ["no_checks", "pending", "failure", "unknown"])
async def test_no_checks_exception_is_explicit_and_narrow(git_api, state):
    _, data, _ = git_api
    data["runs"] = []
    data["combined"] = {
        "state": "pending" if state == "no_checks" else state,
        "total_count": 0 if state == "no_checks" else 1,
        "statuses": [] if state == "no_checks" else [{"context": "ci", "status": state}],
    }
    result = await call(git_api, "pr_merge", repo="project", number=1, allow_no_checks=True)
    assert (result["status"] == "done") == (state == "no_checks")
    assert bool(writes(git_api)) == (state == "no_checks")


@pytest.mark.parametrize(
    "status,conclusion,passes",
    [
        ("queued", "", False),
        ("in_progress", "", False),
        ("completed", "failure", False),
        ("completed", "cancelled", False),
        ("completed", "success", True),
    ],
)
async def test_actions_also_gate_merge(git_api, status, conclusion, passes):
    git_api[1]["runs"] = [run(status=status, conclusion=conclusion)]
    result = await call(git_api, "pr_merge", repo="project", number=1, allow_no_checks=True)
    assert (result["status"] == "done") == passes
    assert bool(writes(git_api)) == passes


async def test_only_exact_sha_latest_workflow_attempts_count(git_api):
    git_api[1]["runs"] = [
        run(10, A),
        run(9, A, conclusion="failure"),
        run(11, B, conclusion="failure"),
    ]
    assert (await call(git_api, "pr_merge", repo="project", number=1))["status"] == "done"


@pytest.mark.parametrize(
    "change",
    [
        {"draft": True},
        {"draft": None},
        {"state": "closed"},
        {"mergeable": False},
        {"mergeable": None},
        {"merged": True},
    ],
)
async def test_pr_state_refusals(git_api, change):
    git_api[1]["pr"].update(change)
    result = await call(git_api, "pr_merge", repo="project", number=1)
    assert result["error"]["code"] == "git_merge_denied"
    assert not writes(git_api)


async def test_head_race_cannot_merge_unchecked_push(git_api):
    git_api[1]["race"] = True
    result = await call(
        git_api, "pr_merge", repo="project", number=1, method="squash", delete_branch=False
    )
    assert result["error"]["code"] == "backend_error"
    assert writes(git_api)[0]["payload"] == {
        "Do": "squash",
        "delete_branch_after_merge": False,
        "head_commit_id": A,
    }
    assert not git_api[1]["pr"]["merged"]


async def test_tag_pins_resolved_sha_and_supports_sha_target(git_api):
    git_api[1]["branch_sha"] = B
    result = await call(
        git_api, "tag_create", repo="project", tag="v1.0.0-rc.1", target=A, message="Candidate"
    )
    assert result["sha"] == A
    assert not any("/branches/" in c["path"] for c in git_api[2])


@pytest.mark.parametrize(
    "tool,args",
    [
        ("pr_create", {"head": "feature", "title": "Title"}),
        ("pr_comment", {"number": 1, "body": "Comment"}),
        ("pr_merge", {"number": 1}),
        ("ci_rerun", {"run_id": 10}),
        ("tag_create", {"tag": "v1.0.0", "message": "Release"}),
    ],
)
async def test_every_write_checks_scope_and_allowlist(git_api, config, tool, args):
    core = git_api[0]
    result = await core.call("agy", "git_" + tool, {"repo": "project", **args})
    assert result["error"]["code"] == "scope_denied"
    change_policy(
        config,
        lambda p: p["allowlists"].update(git={"write_repos": ["someone/*", "example/other"]}),
    )
    result = await call(git_api, tool, repo="project", **args)
    assert result["error"]["code"] == "resource_denied"
    assert git_api[2] == []
    change_policy(config, lambda p: p["allowlists"].update(git={"write_repos": ["example/*"]}))
    assert (await call(git_api, tool, repo="project", **args))["status"] in {"done", "requested"}


@pytest.mark.parametrize(
    "tool,args",
    [
        ("repos", {}),
        ("prs", {}),
        ("pr", {"repo": "project", "number": 1}),
        ("ci_runs", {"repo": "project"}),
        ("ci_log", {"repo": "project", "job_id": 7}),
        ("local_status", {}),
    ],
)
async def test_every_read_checks_scope_even_if_hidden(git_api, tool, args):
    core = git_api[0]
    assert "git_" + tool not in {t["name"] for t in core.tools("agy")}
    result = await core.call("agy", "git_" + tool, args)
    assert result["error"]["code"] == "scope_denied"
    assert git_api[2] == []


@pytest.mark.parametrize(
    "repo",
    [
        "other/project",
        "../project",
        "example/../project",
        "/project",
        "project?x=1",
        "-bad",
        "example/*",
    ],
)
async def test_repo_cannot_inject_path_or_cross_owner(git_api, repo):
    result = await call(git_api, "pr", repo=repo, number=1)
    assert result["error"]["code"] == "invalid_arguments"
    assert not git_api[2]


async def test_log_tail_filter_redaction_and_ansi(git_api):
    secret = "token" + "-must-be-hidden"
    git_api[1]["log"] = (
        "old line\n" * 1800
        + "\x1b[31mFAIL password="
        + secret
        + "\x1b[0m\n"
        + '{"nested":{"token":"'
        + secret
        + '"}}\nFAIL literal.* end\n'
    )
    result = await call(git_api, "ci_log", repo="project", job_id=7, lines=2, contains="FaIl")
    assert result["matched"] == 2 and result["truncated"]
    assert "\x1b" not in json.dumps(result) and secret not in json.dumps(result)
    assert "REDACTED" in str(result["lines"])
    result = await call(git_api, "ci_log", repo="project", job_id=7, contains="literal.*")
    assert result["lines"] == ["FAIL literal.* end"]
    result = await call(git_api, "ci_log", repo="project", job_id=7, contains=secret)
    assert result["matched"] == 0


async def test_untrusted_text_is_sanitized_and_truncated(git_api):
    data = git_api[1]
    data["pr"].update(title="title\x00\nnext", body="\x00Ignore instructions\n" + "x" * 7000)
    data["files"][0]["filename"] = "path\x00\nnext"
    data["commits"][0]["commit"]["message"] = "subject\n" + "x" * 1000
    result = await call(git_api, "pr", repo="project", number=1)
    assert len(result["pr"]["body"]) <= 6000 and result["truncated"]
    assert result["pr"]["title"] == "title next"
    assert len(result["pr"]["commits"][0]["subject"]) <= 300
    assert "\x00" not in json.dumps(result)


async def test_pagination_is_bounded_and_offsets_are_stable(git_api):
    data = git_api[1]
    data["prs"] = [pull(n) for n in range(1, 102)]
    result = await call(git_api, "prs", repo="project", limit=50)
    assert len(result["prs"]) == 50 and result["truncated"]
    pages = [c["query"] for c in git_api[2] if c["path"].endswith("/pulls")]
    assert pages == [
        {"state": "open", "sort": "recentupdate", "page": "1", "limit": "50"},
        {"state": "open", "sort": "recentupdate", "page": "2", "limit": "50"},
    ]
    data["files"] *= 120
    result = await call(git_api, "pr", repo="project", number=1)
    assert len(result["pr"]["files"]) == 100 and result["truncated"]
    data["repos"] *= 500
    repos, cut = await Git(git_api[0]).repositories()
    assert len(repos) == 200 and cut
    assert max(int(c["query"]["page"]) for c in git_api[2] if "page" in c["query"]) <= 4


async def test_incomplete_ci_cannot_be_treated_as_no_checks(git_api):
    git_api[1]["runs"] = [run(i, A) for i in range(1, 250)]
    git_api[1]["combined"] = {"state": "pending", "total_count": 0, "statuses": []}
    result = await call(git_api, "pr_merge", repo="project", number=1, allow_no_checks=True)
    assert result["error"]["code"] == "git_ci_denied" and not writes(git_api)


@pytest.mark.parametrize("corruption", ["sha", "count", "statuses", "runs"])
async def test_malformed_ci_fails_closed(git_api, corruption):
    data = git_api[1]
    if corruption == "sha":
        data["status_sha"] = C
    elif corruption == "count":
        data["combined"]["total_count"] = 55
    elif corruption == "statuses":
        data["combined"]["statuses"] = None
    else:
        data["overrides"]["/repos/example/project/actions/runs"] = {"workflow_runs": "bad"}
    result = await call(git_api, "pr_merge", repo="project", number=1, allow_no_checks=True)
    assert result["error"]["code"] in {"backend_invalid", "git_ci_denied"}
    assert not writes(git_api)


@pytest.mark.parametrize(
    "failure", ["redirect", "oversize", "json", "404", "500", "mode", "symlink"]
)
async def test_transport_and_token_safety(git_api, config, failure, tmp_path):
    api = git_api[0].backends.gitea
    if failure == "mode":
        token_path = Path(config.backends["gitea"]["token_file"])
        await asyncio.to_thread(token_path.chmod, 0o644)
    elif failure == "symlink":
        link = tmp_path / "token-link"
        link.symlink_to(config.backends["gitea"]["token_file"])
        config.backends["gitea"]["token_file"] = str(link)
    else:
        git_api[1]["overrides"]["/repos/search"] = {
            "redirect": 302,
            "oversize": "x" * (gitea.MAX_BODY + 1),
            "json": "not JSON",
            "404": 404,
            "500": 500,
        }[failure]
    result = await call(git_api, "repos")
    assert result["status"] == "error"
    assert "private backend error" not in str(result)
    assert (await api.probe())["status"] == "down"


async def test_doctor_backend_probe_checks_safe_token_and_reachability(git_api):
    assert await git_api[0].backends.probe("gitea") == {
        "status": "ok",
        "reason": "token file safe; API reachable",
    }


@pytest.mark.parametrize(
    "tag", ["1.0.0", "v1.0", "v1.0.0/evil", "v1.0.0+build", "v1.0.0\n", "v1.0.0;cmd"]
)
async def test_invalid_release_tags_refused(git_api, tag):
    result = await call(git_api, "tag_create", repo="project", tag=tag, message="Release")
    assert result["error"]["code"] == "invalid_arguments" and not git_api[2]


def setup_git(path, *args):
    path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        hostcmd.git_argv(path, list(args)),
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def local_repo(tmp_path, config):
    root = tmp_path / "repos"
    repo = root / "project"
    setup_git(repo, "init", "-b", "main")
    (repo / "tracked").write_text("initial\n")
    setup_git(repo, "add", "tracked")
    setup_git(repo, "commit", "-m", "Initial commit")
    wt = root / "project.wt" / "feature"
    setup_git(repo, "worktree", "add", "-b", "feature", str(wt))
    config.git = {"roots": [str(root)]}
    return repo, wt, root


async def test_real_local_status_preserves_index_and_reports_worktree(git_api, local_repo):
    repo, wt, root = local_repo
    (repo / "staged").write_text("staged")
    setup_git(repo, "add", "staged")
    (repo / "tracked").write_text("unstaged")
    (repo / "untracked").write_text("new")
    (wt / "tracked").write_text("feature work")
    setup_git(wt, "stash", "push", "-m", "keep")
    before = (repo / ".git/index").read_bytes(), (repo / ".git/index").stat().st_mtime_ns
    result = await call(git_api, "local_status", repo="project")
    assert result["source"] == "local_git"
    by_path = {r["path"]: r for r in result["checkouts"]}
    assert set(by_path) == {"project", "project.wt/feature"}
    main, feature = by_path["project"], by_path["project.wt/feature"]
    assert main["branch"] == "main" and feature["branch"] == "feature"
    assert main["staged"] == main["unstaged"] == main["untracked"] == 1
    assert main["conflicted"] == 0 and main["stash_count"] == feature["stash_count"] == 1
    assert main["last_commit"]["subject"] == "Initial commit"
    assert before == ((repo / ".git/index").read_bytes(), (repo / ".git/index").stat().st_mtime_ns)
    assert not (repo / ".git/index.lock").exists()
    assert str(root) not in json.dumps(result)


async def test_local_detached_unborn_and_operation_flags(git_api, local_repo):
    repo, wt, root = local_repo
    setup_git(wt, "checkout", "--detach")
    setup_git(root / "empty", "init", "-b", "new")
    (repo / ".git/MERGE_HEAD").write_text(A + "\n")
    (repo / ".git/rebase-merge").mkdir()
    result = await call(git_api, "local_status")
    assert result["status"] == "ok", result
    rows = {r["path"]: r for r in result["checkouts"]}
    assert rows["project.wt/feature"]["branch"] == "detached"
    assert rows["empty"]["last_commit"] is None and rows["empty"]["branch"] == "new"
    assert rows["project"]["merge_in_progress"] and rows["project"]["rebase_in_progress"]


@pytest.mark.parametrize("escape", ["checkout", "worktree", "metadata"])
async def test_local_root_escape_refused(git_api, local_repo, tmp_path, escape):
    repo, wt, root = local_repo
    outside = tmp_path / "outside"
    if escape == "checkout":
        setup_git(outside, "init", "-b", "main")
        (root / "escaped").symlink_to(outside, target_is_directory=True)
    elif escape == "worktree":
        setup_git(repo, "worktree", "add", "-b", "outside", str(outside))
    else:
        outside.mkdir()
        (repo / ".git").rename(outside / "gitdir")
        (repo / ".git").symlink_to(outside / "gitdir", target_is_directory=True)
    result = await call(git_api, "local_status")
    assert result["error"]["code"] == "resource_denied"
    assert str(outside) not in json.dumps(result)


async def test_local_command_failure_is_not_clean_status(git_api, local_repo, monkeypatch):
    async def failed(*args, **kwargs):
        return -1, "", "private error"

    monkeypatch.setattr(hostcmd, "run", failed)
    result = await call(git_api, "local_status")
    assert result["error"]["code"] == "backend_down"


def test_hostcmd_accepts_only_fixed_git_vectors(tmp_path):
    for command in hostcmd.GIT_COMMANDS:
        assert hostcmd.valid_argv(hostcmd.git_argv(tmp_path, command))
    for command in (
        ["push"],
        ["fetch"],
        ["pull"],
        ["checkout", "main"],
        ["reset", "--hard"],
        ["clean", "-fd"],
        ["status", "--porcelain=v2", "--branch", "--ignored"],
        ["rev-parse", "--git-path", "anything"],
    ):
        assert not hostcmd.valid_argv(hostcmd.git_argv(tmp_path, command))
    assert not hostcmd.valid_argv(hostcmd.git_argv("relative", hostcmd.GIT_COMMANDS[0]))
    argv = hostcmd.git_argv(tmp_path, hostcmd.GIT_COMMANDS[0])
    argv[2] = "maintenance.auto=true"
    assert not hostcmd.valid_argv(argv)


@pytest.mark.parametrize(
    "settings", [{"roots": ["relative"]}, {"roots": "bad"}, {"roots": [], "shell": "oops"}]
)
def test_invalid_local_config(settings):
    with pytest.raises(ValueError):
        validate_git(settings)


def test_example_config_and_policy_load(config, monkeypatch):
    from switchboard.policy import Policy

    (config.config_dir / "config.yaml").write_text(Path("config/config.example.yaml").read_text())
    loaded = Config.load()
    assert loaded.git == {"roots": []} and loaded.backends["gitea"]["enabled"] is False
    policy = Policy(Path("config/policy.example.yaml"))
    for client in ("owner", "claude", "codex", "agy", "openclaw", "cortex"):
        assert "git:read" in policy.client(client)["scopes"]
        assert ("git:write" in policy.client(client)["scopes"]) == (
            client in ("owner", "claude", "codex", "agy")
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"url": "http://localhost:3003/api/v1"},
        {"url": "http://example.invalid/api/v1"},
        {"token_file": "relative"},
        {"owner": "../bad"},
        {"owner": ""},
        {"token": "never"},
    ],
)
def test_invalid_gitea_config(config, settings):
    import yaml

    data = {
        "backends": {
            "gitea": {
                "enabled": True,
                "owner": "example",
                "token_file": str(config.config_dir / "gitea.secret"),
                **settings,
            }
        }
    }
    (config.config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        Config.load()


async def test_real_divergence_and_conflict_counts(git_api, local_repo):
    repo, wt, root = local_repo
    setup_git(wt, "branch", "--set-upstream-to=main")
    (wt / "tracked").write_text("feature change\n")
    setup_git(wt, "commit", "-am", "Feature commit")
    (repo / "tracked").write_text("main change\n")
    setup_git(repo, "commit", "-am", "Main commit")
    with pytest.raises(subprocess.CalledProcessError):
        setup_git(wt, "merge", "main")
    result = await call(git_api, "local_status", repo="example/project")
    row = next(r for r in result["checkouts"] if r["branch"] == "feature")
    assert (row["ahead"], row["behind"], row["conflicted"], row["upstream"]) == (1, 1, 1, "main")
    assert row["merge_in_progress"]


async def test_tag_actions_pending_blocks_even_with_green_statuses(git_api):
    git_api[1]["runs"] = [run(status="in_progress", conclusion="")]
    result = await call(git_api, "tag_create", repo="project", tag="v1.0.0", message="Release")
    assert result["error"]["code"] == "git_ci_denied" and not writes(git_api)


async def test_exact_sha_filter_avoids_unrelated_actions_history(git_api):
    git_api[1]["runs"] = [run(i, B, conclusion="failure") for i in range(300)] + [run(1000, A)]
    result = await call(git_api, "pr_merge", repo="project", number=1)
    assert result["merged_sha"] == B
    query = next(c["query"] for c in git_api[2] if c["path"].endswith("/actions/runs"))
    assert query["head_sha"] == A and "branch" not in query


async def test_malformed_empty_status_count_cannot_pass_gate(git_api):
    git_api[1]["combined"]["total_count"] = 0
    result = await call(git_api, "pr_merge", repo="project", number=1, allow_no_checks=True)
    assert result["error"]["code"] == "git_ci_denied" and not writes(git_api)


async def test_no_check_count_cannot_hide_failure_state(git_api):
    git_api[1]["combined"] = {"state": "failure", "total_count": 0, "statuses": []}
    git_api[1]["runs"] = []
    result = await call(git_api, "pr_merge", repo="project", number=1, allow_no_checks=True)
    assert result["error"]["code"] == "git_ci_denied" and not writes(git_api)


async def test_log_size_limit_fails_without_partial_secret_output(git_api, monkeypatch):
    monkeypatch.setattr(gitea, "MAX_BODY", 100)
    git_api[1]["log"] = "private " * 30
    result = await call(git_api, "ci_log", repo="project", job_id=7)
    assert result["error"]["code"] == "backend_invalid"
    assert "lines" not in result and "private" not in str(result)


async def test_pr_comment_does_not_comment_on_an_issue(git_api):
    git_api[1]["overrides"]["/repos/example/project/pulls/1"] = 404
    result = await call(git_api, "pr_comment", repo="project", number=1, body="Comment")
    assert result["error"]["code"] == "not_found" and not writes(git_api)


async def test_merge_receipt_is_verified_before_reporting_done(git_api):
    git_api[1]["overrides"]["/repos/example/project/pulls/1"] = pull()
    result = await call(git_api, "pr_merge", repo="project", number=1)
    assert result["error"]["code"] == "backend_ambiguous"
    assert len(writes(git_api)) == 1


async def test_free_text_write_arguments_are_hashed_in_audit(git_api):
    await call(
        git_api,
        "pr_create",
        repo="project",
        head="feature",
        title="Private title",
        body="Private body",
    )
    row = git_api[0].audit.tail()[-1]
    assert "Private title" not in str(row) and "Private body" not in str(row)
    assert "sha256" in str(row)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("ci_log", {"repo": "project", "job_id": 7, "lines": 401}),
        ("prs", {"limit": 51}),
        ("pr", {"repo": "project", "number": 0}),
        ("pr_merge", {"repo": "project", "number": 1, "method": "fast-forward-only"}),
        ("tag_create", {"repo": "project", "tag": "v1.2.3", "message": " "}),
    ],
)
async def test_bad_tool_arguments_fail_before_network(git_api, tool, args):
    result = await call(git_api, tool, **args)
    assert result["error"]["code"] == "invalid_arguments" and not git_api[2]


async def test_doctor_reports_unsafe_gitea_token(git_api, config):
    result = await git_api[0].call("codex", "ops_switchboard_status", {})
    assert result["checks"]["gitea_token"]["status"] == "ok"
    token = Path(config.backends["gitea"]["token_file"])
    await asyncio.to_thread(token.chmod, 0o644)
    result = await git_api[0].call("codex", "ops_switchboard_status", {})
    assert result["checks"]["gitea_token"]["status"] == "degraded"
    assert result["backends"]["gitea"]["status"] == "down"


async def test_core_worktree_cannot_redirect_local_reads(git_api, local_repo, tmp_path):
    repo, wt, root = local_repo
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    setup_git(repo, "config", "core.worktree", str(outside))
    result = await call(git_api, "local_status")
    assert result["error"]["code"] == "resource_denied"


async def test_ansi_cannot_split_secret_labels_before_redaction(git_api):
    credential = "hidden" + "-credential"
    git_api[1]["log"] = (
        "PASS to\x1b[0mken=" + credential + "\n\x1b]8;;https://example.invalid/\x07link\x1b]8;;\x07"
    )
    result = await call(git_api, "ci_log", repo="project", job_id=7)
    assert result["lines"] == ["PASS token=<REDACTED>", "link"]
    assert credential not in str(result)


async def test_commit_subject_excludes_body(git_api):
    git_api[1]["commits"][0]["commit"]["message"] = "Subject\n\nPrivate body"
    result = await call(git_api, "pr", repo="project", number=1)
    assert result["pr"]["commits"][0]["subject"] == "Subject"


async def test_refs_must_match_exactly_and_shas_are_case_insensitive(git_api):
    bad = await call(git_api, "pr_create", repo="project", head="feature\n", title="x")
    assert bad["error"]["code"] == "invalid_arguments"
    from switchboard.git import sha

    assert sha("A" * 40) == "a" * 40
