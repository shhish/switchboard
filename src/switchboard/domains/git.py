"""Gitea workflows and read-only local checkout inventory."""

import re

from ..errors import CapabilityError
from ..git import TAG, Git
from ..schema import obj, string
from .common import array, output

S = {"type": "string"}
B = {"type": "boolean"}
N = {"type": "integer", "minimum": 0}
ID = {"type": "integer", "minimum": 1}
REPO = string(201)
REF = {**string(256), "pattern": r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$"}
REF_EXACT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
LIMIT = {"type": "integer", "minimum": 1, "maximum": 50}
STATE = {"type": "string", "enum": ["success", "failure", "pending", "no_checks", "unknown"]}
RUN_FIELDS = {
    "id": ID,
    "name": S,
    "status": S,
    "conclusion": S,
    "head_branch": S,
    "head_sha": S,
    "event": S,
    "path": S,
}
RUN = obj(RUN_FIELDS, RUN_FIELDS)
CHECK = obj({"name": S, "state": S, "description": S}, ("name", "state", "description"))
CI = obj(
    {
        "sha": S,
        "state": STATE,
        "checks": {**array(CHECK), "maxItems": 50},
        "runs": {**array(RUN), "maxItems": 200},
        "truncated": B,
    },
    ("sha", "state", "checks", "runs", "truncated"),
)
PR_FIELDS = {
    "repo": S,
    "number": ID,
    "title": S,
    "author": S,
    "head": S,
    "base": S,
    "head_sha": S,
    "draft": B,
    "mergeable": B,
    "merged": B,
    "state": S,
    "updated": S,
}
PR = obj(PR_FIELDS, PR_FIELDS)
PR_LIST = obj({**PR_FIELDS, "ci_state": STATE}, (*PR_FIELDS, "ci_state"))
DETAIL = obj(
    {
        **PR_FIELDS,
        "body": {"type": "string", "maxLength": 6000},
        "files": {
            **array(
                obj({"path": S, "additions": N, "deletions": N}, ("path", "additions", "deletions"))
            ),
            "maxItems": 100,
        },
        "commits": {**array(obj({"sha": S, "subject": S}, ("sha", "subject"))), "maxItems": 100},
        "reviews": {**array(obj({"author": S, "state": S}, ("author", "state"))), "maxItems": 100},
        "ci": CI,
    },
    (*PR_FIELDS, "body", "files", "commits", "reviews", "ci"),
)
JOB = obj(
    {"id": ID, "name": S, "status": S, "conclusion": S}, ("id", "name", "status", "conclusion")
)
RUN_JOBS = obj(
    {**RUN_FIELDS, "jobs": {**array(JOB), "maxItems": 100}, "truncated": B},
    (*RUN_FIELDS, "jobs", "truncated"),
)
COMMIT = obj({"sha": S, "age_s": N, "subject": S}, ("sha", "age_s", "subject"))
CHECKOUT_FIELDS = {
    "repo": S,
    "root": N,
    "path": S,
    "branch": S,
    "upstream": S,
    "ahead": N,
    "behind": N,
    **{key: N for key in ("staged", "unstaged", "untracked", "conflicted", "stash_count")},
    "last_commit": {**COMMIT, "type": ["object", "null"]},
    "merge_in_progress": B,
    "rebase_in_progress": B,
}
CHECKOUT = obj(CHECKOUT_FIELDS, CHECKOUT_FIELDS)


def register(registry):
    def declare(
        name,
        title,
        description,
        inputs,
        result,
        *,
        required=(),
        write=False,
        hashes=(),
        max_chars=None,
    ):
        async def handler(core, client, args):
            # The schema pattern is matched with re.search, where "$" also accepts a
            # trailing newline; branch and tag names must match exactly.
            for key in ("head", "base", "branch", "target"):
                if key in args and not REF_EXACT.fullmatch(args[key]):
                    raise CapabilityError("invalid_arguments", f"Invalid {key}")
            return await getattr(Git(core), name.removeprefix("git_"))(args)

        registry.declare(
            name,
            title,
            description,
            obj(inputs, required),
            scope="git:write" if write else "git:read",
            tier="write" if write else "read",
            idempotent=not write,
            timeout_s=90,
            max_result_chars=max_chars,
            service="local_git" if name == "git_local_status" else "gitea",
            output_schema=result,
            audit_hash_fields=frozenset(hashes),
        )(handler)

    declare(
        "git_repos",
        "Gitea repositories",
        "Gitea repositories: default branch, open PRs and latest CI. "
        "Use when finding Gitea repositories. Requires git:read. Returns up to limit (default 25) "
        "repositories with default branch, open PR count, update time and current "
        "default-branch CI state; pagination is bounded.",
        {"query": string(100), "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        output(
            repos={
                **array(
                    obj(
                        {
                            "repo": S,
                            "default_branch": S,
                            "open_prs": N,
                            "updated": S,
                            "ci_state": STATE,
                        },
                        ("repo", "default_branch", "open_prs", "updated", "ci_state"),
                    )
                ),
                "maxItems": 200,
            }
        ),
    )
    declare(
        "git_prs",
        "Gitea pull requests",
        "Gitea pull requests across repos with branches, CI state and mergeability. "
        "Use when finding pull requests in one or all owner repositories. Requires git:read. "
        "Returns up to 50 PRs (default 20), newest updated first, with branches, "
        "draft/mergeable flags and head CI state; partial views are marked.",
        {
            "repo": REPO,
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "limit": LIMIT,
        },
        output(prs={**array(PR_LIST), "maxItems": 50}),
    )
    declare(
        "git_pr",
        "Gitea pull request detail",
        "Gitea pull request detail: body, files, commits, reviews and CI checks. "
        "Use when reviewing a pull request and its CI. Requires git:read. Returns bounded "
        "untrusted body, up to 100 files, commits and reviews each, plus exact-head checks "
        "and Actions runs; truncated CI cannot authorize writes.",
        {"repo": REPO, "number": ID},
        output(pr=DETAIL, truncated=B),
        required=("repo", "number"),
    )
    declare(
        "git_ci_runs",
        "Gitea Actions runs",
        "Gitea Actions CI runs and jobs for a repo. "
        "Use when checking Actions runs and jobs. Requires git:read. Returns up to 50 runs "
        "(default 20), optionally by branch/status, and up to 100 jobs each; results are "
        "bounded and untrusted.",
        {
            "repo": REPO,
            "branch": REF,
            "status": {
                "type": "string",
                "enum": [
                    "queued",
                    "in_progress",
                    "waiting",
                    "completed",
                    "success",
                    "failure",
                    "cancelled",
                    "skipped",
                ],
            },
            "limit": LIMIT,
        },
        output(runs={**array(RUN_JOBS), "maxItems": 50}),
        required=("repo",),
    )
    declare(
        "git_ci_log",
        "Redacted Gitea job log",
        "Gitea Actions CI job log tail, redacted. "
        "Use when diagnosing one Actions job log. Requires git:read. Returns 1-400 redacted, "
        "ANSI-stripped lines from a bounded 1600-line tail (2 MiB response cap); contains is "
        "a literal case-insensitive filter on redacted text. Logs are untrusted.",
        {
            "repo": REPO,
            "job_id": ID,
            "lines": {"type": "integer", "minimum": 1, "maximum": 400},
            "contains": string(80),
        },
        output(
            repo=S,
            job_id=ID,
            lines={**array({"type": "string", "maxLength": 500}), "maxItems": 400},
            matched=N,
            truncated=B,
        ),
        required=("repo", "job_id"),
        max_chars=240_000,  # 400 lines x 500 chars plus JSON framing.
    )
    declare(
        "git_local_status",
        "Local checkout and worktree status",
        "Local git checkouts and worktrees: branch, dirty files, ahead/behind, stashes. "
        "Use when checking shared checkouts before editing. Requires git:read. Returns up to "
        "100 checkouts/worktrees under configured roots, relative paths, branch/upstream, "
        "ahead/behind, change/stash counts, last commit and merge/rebase flags; fixed "
        "read-only git commands.",
        {"repo": REPO},
        output(checkouts={**array(CHECKOUT), "maxItems": 100}),
    )
    declare(
        "git_pr_create",
        "Open Gitea pull request",
        "Open a Gitea pull request. "
        "Use when opening a pull request from an already-pushed branch. Requires git:write "
        "and a repository allowlist entry. Returns the created PR; base defaults to the "
        "default branch. Creation is not idempotent; inspect PRs before retrying a timeout.",
        {
            "repo": REPO,
            "head": REF,
            "base": REF,
            "title": string(200),
            "body": {"type": "string", "maxLength": 12000},
        },
        output(pr=PR),
        required=("repo", "head", "title"),
        write=True,
        hashes=("title", "body"),
    )
    declare(
        "git_pr_comment",
        "Comment on Gitea pull request",
        "Comment on a Gitea pull request. "
        "Use when posting one pull request comment. Requires git:write and a repository "
        "allowlist entry. Returns the comment ID; body is untrusted data and non-idempotent. "
        "Inspect the PR before retrying a timeout.",
        {"repo": REPO, "number": ID, "body": string(12000)},
        output(repo=S, number=ID, comment_id=ID),
        required=("repo", "number", "body"),
        write=True,
        hashes=("body",),
    )
    declare(
        "git_pr_merge",
        "Merge green Gitea pull request",
        "Merge a Gitea pull request when CI is green. "
        "Use when merging an open, non-draft, mergeable pull request. Requires git:write and "
        "an allowlisted repo. Returns verified merged SHA; pins head_commit_id and requires "
        "successful CI, or explicit allow_no_checks when no checks/runs exist. Deletes branch "
        "by default.",
        {
            "repo": REPO,
            "number": ID,
            "method": {"type": "string", "enum": ["merge", "squash", "rebase"]},
            "delete_branch": B,
            "allow_no_checks": B,
        },
        output(repo=S, number=ID, merged_sha=S),
        required=("repo", "number"),
        write=True,
    )
    declare(
        "git_ci_rerun",
        "Rerun Gitea Actions",
        "Rerun Gitea Actions CI jobs. "
        "Use when retrying a flaky Actions run once. Requires git:write and a repository "
        "allowlist entry. Returns acceptance for failed jobs by default, or the whole run "
        "with failed_only false; inspect CI afterwards. Non-idempotent.",
        {"repo": REPO, "run_id": ID, "failed_only": B},
        output(repo=S, run_id=ID),
        required=("repo", "run_id"),
        write=True,
    )
    declare(
        "git_tag_create",
        "Create green release tag",
        "Create a release tag on Gitea when CI is green. "
        "Use when releasing a snapshot-published repository after merge. Requires git:write "
        "and an allowlisted repo. Returns a new annotated semantic v-version tag pinned to a "
        "CI-successful SHA; target is a branch or full SHA, defaulting to the default branch. "
        "No overwrite.",
        {
            "repo": REPO,
            "tag": {**string(100), "pattern": "^" + TAG + "$"},
            "target": REF,
            "message": string(2000),
        },
        output(repo=S, tag=S, sha=S),
        required=("repo", "tag", "message"),
        write=True,
        hashes=("message",),
    )
