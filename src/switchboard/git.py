"""Typed Git workflows: bounded reads and allowlisted, commit-pinned Gitea writes."""

import asyncio
import base64
import re
import time
from urllib.parse import quote

import yaml

from .domains.common import sanitize_text
from .errors import CapabilityError
from .gitea import SLUG
from .localgit import LocalGit
from .services import redact_line

SHA = re.compile(r"[0-9a-fA-F]{40,64}")
REPO_BUDGET = 25
WORKFLOW_BUDGET = 30
WORKFLOW_BYTES = 64 * 1024
TAG = r"v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def invalid():
    raise CapabilityError("backend_invalid", "gitea: incomplete or invalid response")


def mapping(value):
    if not isinstance(value, dict):
        invalid()
    return value


def text(value, limit=300):
    return sanitize_text(value if isinstance(value, str) else "", limit)


def subject(value):
    return text(value.partition("\n")[0] if isinstance(value, str) else "", 300)


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        invalid()
    return value.lower()  # Gitea reports lowercase hex.


async def bounded_map(function, rows):
    semaphore = asyncio.Semaphore(6)

    async def one(row):
        async with semaphore:
            return await function(row)

    return await asyncio.gather(*(one(row) for row in rows))


class Git:
    sleep = staticmethod(asyncio.sleep)
    clock = staticmethod(time.monotonic)

    def __init__(self, core, *, sleep=None, clock=None):
        self.core = core
        self.api = core.backends.gitea
        self.sleep = sleep or getattr(core, "git_sleep", None) or self.sleep
        self.clock = clock or getattr(core, "git_clock", None) or self.clock

    def repo(self, value):
        owner = self.core.config.backends["gitea"]["owner"]
        if not isinstance(value, str):
            raise CapabilityError("invalid_arguments", "Invalid repository name")
        full = value if "/" in value else owner + "/" + value
        parts = full.split("/")
        if len(parts) != 2 or parts[0] != owner or not all(re.fullmatch(SLUG, p) for p in parts):
            raise CapabilityError(
                "invalid_arguments", "Repository must belong to the configured Gitea owner"
            )
        return full

    def path(self, repo):
        return "/repos/" + "/".join(quote(p, safe="") for p in repo.split("/"))

    async def info(self, repo):
        return mapping(await self.api.request(self.path(repo)))

    def require_write(self, repo):
        self.core.policy.require_git_repo(repo)

    async def repositories(self, query=None):
        rows, truncated = await self.api.pages(
            "/repos/search",
            key="data",
            params={"q": query or "", "private": "true", "sort": "updated", "order": "desc"},
        )
        owner = self.core.config.backends["gitea"]["owner"]
        result = []
        for row in rows:
            full = row.get("full_name", "")
            if isinstance(full, str) and full.startswith(owner + "/"):
                self.repo(full)
                result.append(row)
        return result, truncated

    async def branch_sha(self, repo, branch):
        data = mapping(
            await self.api.request(self.path(repo) + "/branches/" + quote(branch, safe=""))
        )
        return sha(mapping(data.get("commit")).get("id"))

    async def runs(self, repo, params=None, cap=200):
        return await self.api.pages(
            self.path(repo) + "/actions/runs", params=params, key="workflow_runs", cap=cap
        )

    def run_row(self, row):
        if type(row.get("id")) is not int or row["id"] < 1:
            invalid()
        return {
            "id": row["id"],
            "name": text(row.get("name")),
            "status": text(row.get("status"), 40),
            "conclusion": text(row.get("conclusion"), 40),
            "head_branch": text(row.get("head_branch"), 256),
            "head_sha": sha(row.get("head_sha")),
            "event": text(row.get("event"), 80),
            "path": text(row.get("path"), 300),
        }

    async def ci(self, repo, commit):
        commit = sha(commit)
        data = mapping(
            await self.api.request(
                self.path(repo) + "/commits/" + commit + "/status", params={"page": 1, "limit": 50}
            )
        )
        if data.get("sha") != commit:
            invalid()
        statuses = data.get("statuses")
        count = data.get("total_count")
        if statuses is None and count == 0:
            statuses = []  # Gitea sends null, not [], for a commit with no statuses.
        if not isinstance(statuses, list) or type(count) is not int or count < 0:
            invalid()
        checks = []
        for item in statuses[:50]:
            item = mapping(item)
            checks.append(
                {
                    "name": text(item.get("context")),
                    "state": text(item.get("status"), 40),
                    "description": text(item.get("description")),
                }
            )
        runs, truncated = await self.runs(repo, {"head_sha": commit})
        # Keep the latest attempt for each workflow/event on this exact commit.
        current = [self.run_row(row) for row in runs if row.get("head_sha") == commit]
        latest = {}
        for run in sorted(current, key=lambda r: r["id"], reverse=True):
            key = (run["path"] or str(run["id"]), run["event"])
            latest.setdefault(key, run)
        current = list(latest.values())
        incomplete = truncated or count != len(statuses) or count > len(checks)
        states = [check["state"] for check in checks]
        states += [
            "success"
            if r["status"] == "completed" and r["conclusion"] == "success"
            else "skipped"
            if r["status"] == "completed" and r["conclusion"] == "skipped"
            else "pending"
            if r["status"] != "completed"
            else "failure"
            for r in current
        ]
        # A skipped duplicate (e.g. the push run beside a pull_request run) says
        # nothing about the commit. It cannot pass on its own, though: a commit
        # whose only checks were skipped stays unknown.
        all_skipped = bool(states) and all(s == "skipped" for s in states)
        states = [s for s in states if s != "skipped"]
        # Both the combined status and each visible check must agree. Unknown or
        # incomplete data can never authorize a write or the no-checks exception.
        combined_state = data.get("state")
        if not isinstance(combined_state, str):
            invalid()
        if count or combined_state not in {"pending", "success"}:
            states.append(combined_state)
        if incomplete or all_skipped:
            state = "unknown"
        elif not states:
            state = "no_checks"
        elif any(s in {"failure", "error"} for s in states):
            state = "failure"
        elif all(s == "success" for s in states):
            state = "success"
        elif all(s in {"success", "pending", "running", "queued"} for s in states):
            state = "pending"
        else:
            state = "unknown"
        return {
            "sha": commit,
            "state": state,
            "checks": checks,
            "runs": current,
            "truncated": incomplete,
        }

    def gate(self, ci, allow_no_checks=False):
        if ci["state"] != "success" and not (allow_no_checks and ci["state"] == "no_checks"):
            raise CapabilityError(
                "git_ci_denied",
                "CI must succeed for the exact commit; absent, incomplete or pending checks "
                "do not pass",
            )

    async def has_pr_workflows(self, repo, base):
        """Inspect base-branch workflows; incomplete or invalid files fail closed."""
        if not isinstance(base, str) or not base:
            invalid()
        remaining = WORKFLOW_BUDGET
        for directory in (".gitea/workflows", ".github/workflows"):
            path = self.path(repo) + "/contents/" + directory
            rows = await self.api.request(path, params={"ref": base}, missing=True)
            if rows is None:
                continue
            if not isinstance(rows, list):
                invalid()
            for row in rows:
                row = mapping(row)
                name = row.get("name")
                if not isinstance(name, str) or "/" in name or "\\" in name:
                    invalid()
                if not name.lower().endswith((".yml", ".yaml")):
                    continue
                if row.get("type") != "file" or remaining == 0:
                    return True
                remaining -= 1
                size = row.get("size")
                if type(size) is not int or not 0 <= size <= WORKFLOW_BYTES:
                    return True
                file = mapping(
                    await self.api.request(path + "/" + quote(name, safe=""), params={"ref": base})
                )
                content = file.get("content")
                if (
                    file.get("encoding") != "base64"
                    or not isinstance(content, str)
                    or len(content) > 2 * WORKFLOW_BYTES
                ):
                    return True
                try:
                    raw = base64.b64decode("".join(content.split()), validate=True)
                    if len(raw) > WORKFLOW_BYTES:
                        return True
                    workflow = yaml.safe_load(raw)
                except (ValueError, yaml.YAMLError, UnicodeError, RecursionError):
                    return True
                if not isinstance(workflow, dict):
                    return True
                # PyYAML's YAML 1.1 resolver treats an unquoted `on` as True.
                for key in ("on", True):
                    if key not in workflow:
                        continue
                    events = workflow[key]
                    if not isinstance(events, (str, list, dict)):
                        return True
                    triggers = [events] if isinstance(events, str) else events
                    if "pull_request" in triggers or "pull_request_target" in triggers:
                        return True
        return False

    def pr_row(self, repo, data):
        data = mapping(data)
        head, base = mapping(data.get("head")), mapping(data.get("base"))
        if type(data.get("number")) is not int or data["number"] < 1:
            invalid()
        return {
            "repo": repo,
            "number": data["number"],
            "title": text(data.get("title"), 200),
            "author": text(mapping(data.get("user", {})).get("login"), 100),
            "head": text(head.get("ref"), 256),
            "base": text(base.get("ref"), 256),
            "head_sha": sha(head.get("sha")),
            "draft": data.get("draft") is True,
            "mergeable": data.get("mergeable") is True,
            "merged": data.get("merged") is True,
            "state": text(data.get("state"), 20),
            "updated": text(data.get("updated_at"), 80),
        }

    async def repos(self, args):
        rows, truncated = await self.repositories(args.get("query"))
        limit = args.get("limit", 25)
        if len(rows) > limit:  # CI lookups cost three requests per repo
            rows, truncated = rows[:limit], True

        async def one(data):
            repo = self.repo(data["full_name"])
            branch = data.get("default_branch")
            if not isinstance(branch, str) or not branch:
                invalid()
            try:
                ci = await self.ci(repo, await self.branch_sha(repo, branch))
                state = ci["state"]
            except CapabilityError as exc:
                if exc.code != "not_found":
                    raise
                state = "unknown"  # An empty repository has no default-branch commit.
            count = data.get("open_pr_counter", 0)
            if type(count) is not int or count < 0:
                invalid()
            return {
                "repo": repo,
                "default_branch": text(branch, 256),
                "open_prs": count,
                "updated": text(data.get("updated_at"), 80),
                "ci_state": state,
            }

        return {
            "status": "ok",
            "repos": await bounded_map(one, rows),
            "limit": limit,
            "truncated": truncated,
        }

    async def prs(self, args):
        limit = args.get("limit", 20)
        if "repo" in args:
            repos, truncated = [{"full_name": self.repo(args["repo"])}], False
        else:
            repos, truncated = await self.repositories()
            if args.get("state", "open") == "open":
                # Repo search reports open PR counts, so skip repos with none.
                repos = [r for r in repos if r.get("open_pr_counter")]
            if len(repos) > REPO_BUDGET:  # newest-updated first; bound backend fan-out
                repos, truncated = repos[:REPO_BUDGET], True

        async def one(data):
            repo = self.repo(data["full_name"])
            rows, cut = await self.api.pages(
                self.path(repo) + "/pulls",
                params={"state": args.get("state", "open"), "sort": "recentupdate"},
                cap=limit + 1,
            )
            return [self.pr_row(repo, row) for row in rows], cut

        batches = await bounded_map(one, repos)
        rows = [row for batch, _ in batches for row in batch]
        rows.sort(key=lambda row: row["updated"], reverse=True)
        truncated = truncated or any(cut for _, cut in batches) or len(rows) > limit

        async def with_ci(row):
            row["ci_state"] = (await self.ci(row["repo"], row["head_sha"]))["state"]
            return row

        return {
            "status": "ok",
            "prs": await bounded_map(with_ci, rows[:limit]),
            "limit": limit,
            "truncated": truncated,
        }

    async def pr(self, args):
        repo = self.repo(args["repo"])
        path = self.path(repo) + f"/pulls/{args['number']}"
        data = mapping(await self.api.request(path))
        row = self.pr_row(repo, data)
        files, fcut = await self.api.pages(path + "/files", cap=100)
        commits, ccut = await self.api.pages(path + "/commits", cap=100)
        reviews, rcut = await self.api.pages(path + "/reviews", cap=100)
        row.update(
            body=text(data.get("body"), 6000),
            files=[
                {
                    "path": text(f.get("filename"), 1024),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                }
                for f in files
            ],
            commits=[
                {
                    "sha": sha(c.get("sha"))[:12],
                    "subject": subject(mapping(c.get("commit")).get("message")),
                }
                for c in commits
            ],
            reviews=[
                {
                    "author": text(mapping(r.get("user", {})).get("login"), 100),
                    "state": text(r.get("state"), 40),
                }
                for r in reviews
            ],
            ci=await self.ci(repo, row["head_sha"]),
        )
        return {
            "status": "ok",
            "pr": row,
            "truncated": fcut
            or ccut
            or rcut
            or row["ci"]["truncated"]
            or len(data.get("body") or "") > 6000,
        }

    async def ci_runs(self, args):
        repo = self.repo(args["repo"])
        limit = args.get("limit", 20)
        params = {
            key: args[key] for key in ("branch", "status", "event", "head_sha") if key in args
        }
        rows, truncated = await self.runs(repo, params, cap=limit + 1)

        async def one(data):
            run = self.run_row(data)
            jobs, cut = await self.api.pages(
                self.path(repo) + f"/actions/runs/{run['id']}/jobs", key="jobs", cap=100
            )
            run["jobs"] = [
                {
                    "id": j.get("id"),
                    "name": text(j.get("name")),
                    "status": text(j.get("status"), 40),
                    "conclusion": text(j.get("conclusion"), 40),
                }
                for j in jobs
            ]
            run["truncated"] = cut
            return run

        runs = await bounded_map(one, rows[:limit])
        return {
            "status": "ok",
            "runs": runs,
            "limit": limit,
            "truncated": truncated or len(rows) > limit or any(run["truncated"] for run in runs),
        }

    async def ci_log(self, args):
        repo = self.repo(args["repo"])
        data = await self.api.request(
            self.path(repo) + f"/actions/jobs/{args['job_id']}/logs", text=True
        )
        # Match only redacted, control-stripped data: filters cannot expose whether
        # a secret occurred, and terminal escapes cannot conceal credentials.
        raw = data.splitlines()
        tail = raw[-1600:]
        lines = [redact_line(ANSI.sub("", line)) for line in tail]
        needle = args.get("contains", "").casefold()
        matches = [line for line in lines if needle in line.casefold()]
        limit = args.get("lines", 100)
        return {
            "status": "ok",
            "repo": repo,
            "job_id": args["job_id"],
            "lines": matches[-limit:],
            "matched": len(matches),
            "truncated": len(raw) > 1600
            or len(matches) > limit
            or any(len(line) > 500 for line in tail),
        }

    async def ci_wait(self, args):
        from .domains.common import require_one

        require_one(args, "number", "ref")
        repo = self.repo(args["repo"])
        timeout_s = args.get("timeout_s", 300)
        start_time = self.clock()
        deadline = start_time + timeout_s

        terminal_states = {"success", "failure", "no_checks"}

        async def resolve_head():
            if "number" in args:
                path = self.path(repo) + f"/pulls/{args['number']}"
                data = mapping(await self.api.request(path))
                row = self.pr_row(repo, data)
                return row["head_sha"]
            ref = args["ref"]
            if SHA.fullmatch(ref):
                return ref.lower()
            return await self.branch_sha(repo, ref)

        commit = await resolve_head()
        ci_info = await self.ci(repo, commit)

        while ci_info["state"] not in terminal_states:
            now = self.clock()
            remaining = deadline - now
            if remaining <= 0:
                break
            await self.sleep(min(15, remaining))
            commit = await resolve_head()
            ci_info = await self.ci(repo, commit)

        waited_s = int(round(self.clock() - start_time))
        timed_out = ci_info["state"] not in terminal_states

        return {
            "status": "ok",
            "repo": repo,
            "sha": ci_info["sha"],
            "state": ci_info["state"],
            "checks": ci_info["checks"],
            "runs": ci_info["runs"],
            "truncated": ci_info["truncated"],
            "waited_s": waited_s,
            "timed_out": timed_out,
        }

    async def local_status(self, args):
        repo = args.get("repo")
        if repo is not None:
            if "/" in repo:
                repo = self.repo(repo).split("/")[1]
            if not re.fullmatch(SLUG, repo):
                raise CapabilityError("invalid_arguments", "Invalid local repository name")
        return await LocalGit(self.core.config).status(repo)

    async def pr_create(self, args):
        repo = self.repo(args["repo"])
        self.require_write(repo)
        base = args.get("base") or mapping(await self.info(repo)).get("default_branch")
        if not isinstance(base, str) or not base:
            invalid()
        data = await self.api.request(
            self.path(repo) + "/pulls",
            payload={
                "head": args["head"],
                "base": base,
                "title": args["title"],
                "body": args.get("body", ""),
            },
        )
        return {"status": "done", "pr": self.pr_row(repo, data)}

    async def pr_comment(self, args):
        repo = self.repo(args["repo"])
        self.require_write(repo)
        self.pr_row(repo, await self.api.request(self.path(repo) + f"/pulls/{args['number']}"))
        data = mapping(
            await self.api.request(
                self.path(repo) + f"/issues/{args['number']}/comments",
                payload={"body": args["body"]},
            )
        )
        if type(data.get("id")) is not int:
            invalid()
        return {"status": "done", "repo": repo, "number": args["number"], "comment_id": data["id"]}

    async def pr_merge(self, args):
        repo = self.repo(args["repo"])
        self.require_write(repo)
        path = self.path(repo) + f"/pulls/{args['number']}"
        data = mapping(await self.api.request(path))
        row = self.pr_row(repo, data)
        if (
            row["state"] != "open"
            or data.get("draft") is not False
            or row["merged"]
            or not row["mergeable"]
        ):
            raise CapabilityError("git_merge_denied", "PR must be open, not draft, and mergeable")
        ci = await self.ci(repo, row["head_sha"])
        allow_no_checks = args.get("allow_no_checks", False)
        self.gate(ci, allow_no_checks)
        if (
            ci["state"] == "no_checks"
            and allow_no_checks
            and await self.has_pr_workflows(repo, data["base"].get("ref"))
        ):
            raise CapabilityError(
                "git_ci_denied",
                "This repository runs CI on pull requests; wait for its checks instead of "
                "allow_no_checks",
            )
        await self.api.request(
            path + "/merge",
            payload={
                "Do": args.get("method", "merge"),
                "delete_branch_after_merge": args.get("delete_branch", True),
                "head_commit_id": row["head_sha"],
            },
        )
        merged = mapping(await self.api.request(path))
        merged_sha = merged.get("merge_commit_sha")
        if (
            merged.get("merged") is not True
            or not isinstance(merged_sha, str)
            or not SHA.fullmatch(merged_sha)
        ):
            raise CapabilityError(
                "backend_ambiguous",
                "Merge accepted but merged commit not verified; inspect git_pr before retrying",
            )
        return {"status": "done", "repo": repo, "number": args["number"], "merged_sha": merged_sha}

    async def ci_rerun(self, args):
        repo = self.repo(args["repo"])
        self.require_write(repo)
        path = self.path(repo) + f"/actions/runs/{args['run_id']}"
        run = mapping(await self.api.request(path))
        conclusion = run.get("conclusion")
        # Gitea refuses rerun-failed-jobs on a run with no failed jobs: re-run the
        # whole run then, unless the caller explicitly asked for failed jobs only.
        failed_only = args.get("failed_only", conclusion in ("failure", "cancelled"))
        await self.api.request(
            path + ("/rerun-failed-jobs" if failed_only else "/rerun"), payload={}
        )
        result = {
            "status": "requested",
            "repo": repo,
            "run_id": args["run_id"],
            "failed_only": failed_only,
            "stale": False,
        }
        # A re-run replays the run's original commit; say so when the branch moved on.
        branch, head = run.get("head_branch"), run.get("head_sha")
        if run.get("event") in ("push", "schedule") and isinstance(branch, str) and branch:
            try:
                current = await self.branch_sha(repo, branch)
            except CapabilityError:
                current = None
            if current and isinstance(head, str) and current != head.lower():
                result["stale"] = True
        return result

    async def pr_land(self, args):
        """Validate before accepting the job; the job itself never raises."""
        repo = self.repo(args["repo"])
        self.require_write(repo)
        row = self.pr_row(
            repo, mapping(await self.api.request(self.path(repo) + f"/pulls/{args['number']}"))
        )
        if row["state"] != "open" or row["draft"] or row["merged"]:
            raise CapabilityError("git_merge_denied", "PR must be open and not a draft")
        return repo

    async def land(self, args, *, pressure=None, progress=None):
        """Rebase, wait for CI, re-run a failure when the host was overloaded, merge."""
        repo = self.repo(args["repo"])
        number = args["number"]
        path = self.path(repo) + f"/pulls/{number}"
        retries = args.get("flake_retries", 1)
        deadline = self.clock() + args.get("timeout_s", 1800)
        steps = []

        def step(kind, detail=""):
            steps.append(
                {"at_s": int(round(self.clock() - started)), "step": kind, "detail": detail}
            )
            del steps[:-40]
            if progress:
                progress(kind)

        def done(status, **extra):
            return {"status": status, "repo": repo, "number": number, "steps": steps, **extra}

        async def load_level():
            if pressure is None:
                return None
            try:
                return (await pressure()).get("level")
            except Exception:
                return None

        started = self.clock()
        rebases = 0
        while True:
            data = mapping(await self.api.request(path))
            row = self.pr_row(repo, data)
            if row["merged"]:
                return done("merged", merged_sha=text(data.get("merge_commit_sha"), 64))
            if row["state"] != "open":
                return done("blocked", reason="PR is closed")
            if rebases == 0 or not row["mergeable"]:
                if rebases >= 3:
                    return done("blocked", reason="still not mergeable after 3 rebases")
                rebases += 1
                try:
                    await self.api.request(
                        path + "/update", method="POST", params={"style": "rebase"}
                    )
                    step("rebase")
                except CapabilityError as exc:
                    # Up to date already, or a real conflict: the merge check below decides.
                    step("rebase_skipped", exc.code)
                await self.sleep(5)
                continue
            ci = await self.ci(repo, row["head_sha"])
            while ci["state"] in ("pending", "unknown"):
                if self.clock() >= deadline:
                    return done("timeout", reason="CI did not finish", sha=row["head_sha"])
                await self.sleep(20)
                ci = await self.ci(repo, row["head_sha"])
            step("ci", ci["state"])
            if ci["state"] == "failure":
                failed = [r["id"] for r in ci["runs"] if r.get("conclusion") == "failure"]
                if retries <= 0 or not failed:
                    return done("ci_failed", sha=row["head_sha"], failed_runs=failed[:10])
                # Only spend a retry once the Mac is no longer critical: load flakes repeat.
                while await load_level() == "critical":
                    if self.clock() >= deadline:
                        return done("timeout", reason="host pressure stayed critical")
                    step("wait_pressure")
                    await self.sleep(30)
                retries -= 1
                for run_id in failed[:5]:
                    await self.api.request(
                        self.path(repo) + f"/actions/runs/{run_id}/rerun-failed-jobs", payload={}
                    )
                step("rerun", ",".join(map(str, failed[:5])))
                await self.sleep(20)
                continue
            try:
                merged = await self.pr_merge(
                    {
                        "repo": repo,
                        "number": number,
                        "method": args.get("method", "merge"),
                        "delete_branch": args.get("delete_branch", True),
                        "allow_no_checks": args.get("allow_no_checks", False),
                    }
                )
            except CapabilityError as exc:
                if exc.code in ("git_merge_denied", "backend_error") and rebases < 3:
                    step("merge_refused", exc.code)
                    rebases += 1
                    try:
                        await self.api.request(
                            path + "/update", method="POST", params={"style": "rebase"}
                        )
                        step("rebase")
                    except CapabilityError as inner:
                        step("rebase_skipped", inner.code)
                    await self.sleep(5)
                    continue
                return done("blocked", reason=exc.code)
            step("merged", merged["merged_sha"])
            return done("merged", merged_sha=merged["merged_sha"])

    async def tag_create(self, args):
        repo = self.repo(args["repo"])
        self.require_write(repo)
        if not re.fullmatch(TAG, args["tag"]) or not args["message"].strip():
            raise CapabilityError("invalid_arguments", "Tag must be a semantic v-version")
        path = self.path(repo)
        if (
            await self.api.request(path + "/tags/" + quote(args["tag"], safe=""), missing=True)
            is not None
        ):
            raise CapabilityError("git_tag_denied", "Release tag already exists")
        target = args.get("target") or (await self.info(repo)).get("default_branch")
        if not isinstance(target, str) or not target:
            invalid()
        commit = target.lower() if SHA.fullmatch(target) else await self.branch_sha(repo, target)
        self.gate(await self.ci(repo, commit))
        # Send the resolved SHA, never a movable branch or tag name.
        await self.api.request(
            path + "/tags",
            payload={"tag_name": args["tag"], "target": commit, "message": args["message"]},
        )
        return {"status": "done", "repo": repo, "tag": args["tag"], "sha": commit}
