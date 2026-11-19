"""One shared Gitea project, with bounded reads and local actor receipts."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime

from .domains.board import COLUMNS
from .domains.common import sanitize_text
from .errors import CapabilityError
from .execution import Execution
from .git import Git, invalid, mapping

CACHE_SECONDS = 300


def text(value, limit=200):
    return sanitize_text(value if isinstance(value, str) else "", limit)


def positive(value):
    if type(value) is not int or value < 1:
        invalid()
    return value


def title_key(value):
    return " ".join(value.split()).casefold()


class Board:
    def __init__(self, core):
        self.core = core
        self.api = core.backends.gitea
        self.git = Git(core)
        self._cache = None
        self._discovery_lock = asyncio.Lock()
        # Serialise local board writers so concurrent adds share the duplicate guard.
        self._write_lock = asyncio.Lock()
        self._things_lock = asyncio.Lock()

    def repo(self, value=None):
        return self.git.repo(
            value if value is not None else self.api.settings()["board_general_repo"]
        )

    def reference(self, value):
        match = re.fullmatch(r"([^#]*)#([1-9][0-9]*)", value)
        if not match:
            raise CapabilityError("invalid_arguments", "Card must be repo#number or #number")
        return self.repo(match[1] or None), int(match[2])

    async def discover(self):
        settings = self.api.settings()
        key = (settings["url"], settings["owner"], settings["board_project"])
        async with self._discovery_lock:
            if self._cache and self._cache[0] == key and time.monotonic() < self._cache[1]:
                return self._cache[2]
            projects, _ = await self.api.pages("/user/projects", params={"state": "open"})
            matches = [p for p in projects if p.get("title") == key[2]]
            if len(matches) != 1:
                raise CapabilityError(
                    "backend_invalid", f"gitea: board project {key[2]} missing or ambiguous"
                )
            project = matches[0]
            prefix = f"/user/projects/{positive(project.get('id'))}"
            rows, partial = await self.api.pages(prefix + "/columns")
            columns = {}
            for name in COLUMNS:
                matches = [c for c in rows if c.get("title") == name]
                if len(matches) != 1:
                    raise CapabilityError(
                        "backend_invalid", f"gitea: board column {name} missing or ambiguous"
                    )
                columns[name] = positive(matches[0].get("id"))
            if partial:
                raise CapabilityError("backend_invalid", "gitea: incomplete board column discovery")
            value = {
                "board": {
                    "title": text(project["title"]),
                    "url": text(project.get("html_url"), 600),
                },
                "prefix": prefix,
                "columns": columns,
            }
            self._cache = (key, time.monotonic() + CACHE_SECONDS, value)
            return value

    async def snapshot(self):
        board = await self.discover()
        results = await asyncio.gather(
            *(
                self.api.pages(f"{board['prefix']}/columns/{board['columns'][name]}/issues")
                for name in COLUMNS
            )
        )
        cards, seen = [], set()
        owner = self.api.settings()["owner"]
        for name, (issues, _) in zip(COLUMNS, results, strict=True):
            for issue in issues:
                if issue.get("pull_request") is not None:
                    continue
                repository = mapping(issue.get("repository"))
                full = repository.get("full_name")
                if not isinstance(full, str) or not isinstance(issue.get("title"), str):
                    invalid()
                if isinstance(full, str) and full.split("/", 1)[0] != owner:
                    continue  # A user project may also contain another owner's issues.
                try:
                    repo = self.repo(full)
                except CapabilityError:
                    invalid()
                issue_id = positive(issue.get("id"))
                positive(issue.get("number"))
                if issue.get("state") not in ("open", "closed"):
                    invalid()
                if issue_id not in seen:
                    cards.append((issue, repo, name))
                    seen.add(issue_id)
        return board, cards, any(partial for _, partial in results)

    def summary(self, issue, repo, column):
        issue_id = positive(issue.get("id"))
        number = positive(issue.get("number"))
        receipt = self.core.storage.db.execute(
            "SELECT claimed_by, last_actor, last_note FROM board_cards WHERE issue_id=?",
            (issue_id,),
        ).fetchone()
        labels = issue.get("labels") or []
        if not isinstance(labels, list) or issue.get("state") not in ("open", "closed"):
            invalid()
        return {
            "card": f"{repo.split('/', 1)[1]}#{number}",
            "repo": repo.split("/", 1)[1],
            "number": number,
            "title": text(issue.get("title")),
            "column": "Done" if issue["state"] == "closed" else column,
            "state": issue["state"],
            "updated": text(issue.get("updated_at"), 80),
            "url": text(issue.get("html_url"), 600),
            "labels": [text(mapping(label).get("name"), 80) for label in labels[:20]],
            **{
                key: receipt[key] if receipt else None
                for key in ("claimed_by", "last_note", "last_actor")
            },
        }

    async def list(self, client, args):
        project = self.repo(args["project"]) if "project" in args else None
        board, rows, truncated = await self.snapshot()
        groups = {name: [] for name in COLUMNS}
        for issue, repo, column in rows:
            if project is None or repo == project:
                card = self.summary(issue, repo, column)
                groups[card["column"]].append(card)
        needs_you = sum(c["state"] == "open" for c in groups["Needs you"])
        remaining = args.get("limit", 60)
        result = []
        for name, cards in groups.items():
            if args.get("column") and args["column"] != name:
                continue
            if name == "Done" and not (
                args.get("include_done", False) or args.get("column") == name
            ):
                continue
            cards.sort(key=lambda c: (c["updated"], c["number"]), reverse=True)
            count = len(cards)
            size = min(remaining, 15 if name == "Done" else remaining)
            selected = cards[:size]
            remaining -= len(selected)
            truncated |= len(selected) < count
            result.append({"title": name, "count": count, "cards": selected})
        return {
            "status": "ok",
            "board": board["board"],
            "columns": result,
            "needs_you": needs_you,
            "limit": args.get("limit", 60),
            "truncated": truncated,
        }

    async def lookup(self, repo, number):
        board, rows, partial = await self.snapshot()
        member = next((r for r in rows if r[1] == repo and r[0]["number"] == number), None)
        if member is None:
            if partial:
                raise CapabilityError("backend_invalid", "gitea: board membership scan truncated")
            raise CapabilityError("not_found", "Card is not attached to the configured board")
        issue = await self.read_issue(repo, number, member[0]["id"])
        self.summary(issue, repo, member[2])  # Validate presentation fields before any mutation.
        return board, issue, member[2]

    async def read_issue(self, repo, number, issue_id):
        issue = mapping(await self.api.request(f"{self.git.path(repo)}/issues/{number}"))
        if (
            issue.get("id") != issue_id
            or issue.get("number") != number
            or issue.get("pull_request") is not None
            or issue.get("state") not in ("open", "closed")
        ):
            invalid()
        return issue

    async def card(self, client, args):
        repo, number = self.reference(args["card"])
        _, issue, column = await self.lookup(repo, number)
        count = issue.get("comments", 0)
        if type(count) is not int or count < 0:
            invalid()
        # Gitea comments are oldest first. Read the last two 30-row pages using
        # the issue's count, rather than fetching a page for every old comment.
        last = max(1, (count + 29) // 30)
        comments = []
        for page in range(max(1, last - 1), last + 1):
            rows = await self.api.request(
                f"{self.git.path(repo)}/issues/{number}/comments",
                params={"page": page, "limit": 30},
            )
            if (
                not isinstance(rows, list)
                or not all(isinstance(r, dict) for r in rows)
                or len(rows) > 30
            ):
                invalid()
            comments.extend(rows)
        comments.sort(key=lambda r: (text(r.get("created_at"), 80), positive(r.get("id"))))
        return {
            "status": "ok",
            **self.summary(issue, repo, column),
            "body": text(issue.get("body"), 6000),
            "created": text(issue.get("created_at"), 80),
            "comments": [
                {
                    "author": text(mapping(c.get("user")).get("login"), 100),
                    "created": text(c.get("created_at"), 80),
                    "body": text(c.get("body"), 1500),
                }
                for c in comments[-30:]
            ],
            "truncated": count > 30,
        }

    def record(self, issue, repo, column, client, note=None, *, claim=False):
        summary = self.summary(issue, repo, column)
        db = self.core.storage.db
        with db:
            db.execute(
                "INSERT INTO board_cards "
                "(issue_id, card, claimed_by, last_actor, last_note, last_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(issue_id) DO UPDATE SET "
                "card=excluded.card, claimed_by=excluded.claimed_by, "
                "last_actor=excluded.last_actor, "
                "last_note=excluded.last_note, last_at=excluded.last_at",
                (
                    issue["id"],
                    summary["card"],
                    client if claim else summary["claimed_by"],
                    client,
                    text(note, 200) if note is not None else summary["last_note"],
                    datetime.now(UTC).isoformat(),
                ),
            )
        return self.summary(issue, repo, column)

    async def add(self, client, args):
        repo = self.repo(args.get("repo"))
        self.core.policy.require_git_repo(repo)
        if not text(args["title"]):
            raise CapabilityError("invalid_arguments", "Title must not be blank")
        column = args.get("column", "Ideas")
        async with self._write_lock:
            board, rows, partial = await self.snapshot()
            for issue, full, current in rows:
                if (
                    full == repo
                    and issue["state"] == "open"
                    and title_key(issue.get("title", "")) == title_key(args["title"])
                ):
                    return {"status": "ok", **self.summary(issue, repo, current), "duplicate": True}
            if partial:
                raise CapabilityError(
                    "backend_invalid", "gitea: duplicate scan truncated; no issue created"
                )
            body = args.get("body", "")
            issue = mapping(
                await self.api.request(
                    self.git.path(repo) + "/issues",
                    method="POST",
                    payload={
                        "title": args["title"],
                        "body": f"{body}\n\n_Added by {client} via Switchboard._",
                    },
                )
            )
            positive(issue.get("number"))
            await self.api.request(
                f"{board['prefix']}/columns/{board['columns'][column]}/issues/{positive(issue.get('id'))}",
                method="POST",
            )
            return {"status": "ok", **self.record(issue, repo, column, client), "duplicate": False}

    async def move(self, client, args):
        repo, number = self.reference(args["card"])
        self.core.policy.require_git_repo(repo)
        column, note = args["column"], args.get("note")
        if column == "Needs you" and (note is None or not text(note, 2000)):
            raise CapabilityError(
                "invalid_arguments",
                "Needs you requires a note saying exactly what the owner needs to do",
            )
        async with self._write_lock:
            board, issue, _ = await self.lookup(repo, number)
            # Move before closing/reopening, so failed moves do not change issue state.
            await self.api.request(
                f"{board['prefix']}/issues/{issue['id']}/move",
                method="POST",
                payload={"column_id": board["columns"][column]},
            )
            desired = "closed" if column == "Done" else "open"
            if issue["state"] != desired:
                await self.api.request(
                    f"{self.git.path(repo)}/issues/{number}",
                    method="PATCH",
                    payload={"state": desired},
                )
            # Retain a successful claim even if the later comment or refresh fails.
            self.record(issue, repo, column, client, claim=column == "In progress")
            if note is not None:
                await self.api.request(
                    f"{self.git.path(repo)}/issues/{number}/comments",
                    method="POST",
                    payload={"body": f"**{client}** moved this to {column}: {note}"},
                )
                self.record(issue, repo, column, client, note)
            issue = await self.read_issue(repo, number, issue["id"])
            if issue["state"] != desired:
                invalid()
            return {
                "status": "ok",
                **self.summary(issue, repo, column),
            }

    async def comment(self, client, args):
        repo, number = self.reference(args["card"])
        self.core.policy.require_git_repo(repo)
        if not text(args["body"], 4000):
            raise CapabilityError("invalid_arguments", "Comment must not be blank")
        async with self._write_lock:
            _, issue, column = await self.lookup(repo, number)
            comment = mapping(
                await self.api.request(
                    f"{self.git.path(repo)}/issues/{number}/comments",
                    method="POST",
                    payload={"body": f"**{client}**: {args['body']}"},
                )
            )
            comment_id = positive(comment.get("id"))
            self.record(issue, repo, column, client, args["body"])
            issue = await self.read_issue(repo, number, issue["id"])
            return {
                "status": "ok",
                "comment_id": comment_id,
                **self.summary(issue, repo, column),
            }

    def things_enabled(self):
        settings = self.core.config.backends
        return bool(
            settings["gitea"].get("enabled", False)
            and settings["gitea"].get("board_things_project", "")
            and settings["things"].get("enabled", False)
        )

    def mark_things_attempt(self, now):
        with self.core.storage.db as db:
            db.execute(
                "INSERT OR REPLACE INTO monitor_meta VALUES ('board_things_last_attempt', ?)",
                (now,),
            )

    async def import_things(self, client, args):
        self.core.policy.require_scope(client, "tasks:write")
        async with self._things_lock:
            if self.things_enabled():
                self.mark_things_attempt(time.time())
            return await self._import_things()

    async def evaluate_things(self, now=None):
        """Owner-configured system work: no borrowed client identity or new grants."""
        now = time.time() if now is None else now
        async with self._things_lock:
            if not self.things_enabled():
                return
            db = self.core.storage.db
            previous = db.execute(
                "SELECT value FROM monitor_meta WHERE key='board_things_last_attempt'"
            ).fetchone()
            interval = self.core.config.backends["gitea"].get("board_things_interval_s", 300)
            if previous is not None and now - previous["value"] < interval:
                return
            # Failed attempts are gated too, and restarts retain this deadline.
            self.mark_things_attempt(now)
            started = time.monotonic()
            result, error_code, decision, outcome = None, "cancelled", "allowed", "cancelled"
            try:
                async with asyncio.timeout(180):
                    result = await self._import_things()
                error_code = result["errors"][0]["code"] if result["errors"] else None
                outcome = "error" if error_code else "ok"
            except CapabilityError as exc:
                error_code, outcome = exc.code, "error"
                if exc.code in {"resource_denied", "policy_invalid"}:
                    decision = "denied"
            except TimeoutError:
                error_code, outcome = "timeout", "error"
            except Exception:
                error_code, outcome = "internal_error", "error"
            finally:
                self.core.audit.append(
                    client=None,
                    capability="board_import_things",
                    tier="write",
                    args={
                        key: len(result[key]) if isinstance(result[key], list) else result[key]
                        for key in ("imported", "completed", "skipped", "errors")
                    }
                    if result
                    else {},
                    decision=decision,
                    outcome=outcome,
                    error_code=error_code,
                    duration=time.monotonic() - started,
                    ctx=Execution.new("system"),
                    result_bytes=len(json.dumps(result)) if result is not None else None,
                )

    async def _import_things(self):
        from .domains.tasks import resource

        result = {
            "status": "ok",
            "imported": [],
            "completed": 0,
            "skipped": 0,
            "errors": [],
            "truncated": False,
        }
        if not self.things_enabled():
            return result
        repo = self.repo()
        self.core.policy.require_git_repo(repo)
        rules = self.core.policy.task_rules()
        try:
            data = await self.core.backends.things.project_todos(
                self.api.settings()["board_things_project"]
            )
        except CapabilityError as exc:
            if exc.code not in {
                "backend_disabled",
                "backend_down",
                "things_unauthorized",
                "things_timeout",
                "things_backend_error",
            }:
                result["errors"].append({"things_id": None, "code": exc.code})
            return result
        project = data["project"]
        # Preflight the project before creating any card. The native completion
        # also rechecks each to-do's current destination using the same policy.
        self.core.policy.require_task(rules, project["id"], project["area_id"])
        todos = data["todos"]
        result["skipped"] = max(0, len(todos) - 20)
        result["truncated"] = data["truncated"] or len(todos) > 20
        db = self.core.storage.db
        for todo in todos[:20]:
            ident = todo["id"]
            try:
                # Refresh the policy snapshot for each item; revocation must take
                # effect even when it happens partway through a batch.
                self.core.policy.require_git_repo(repo)
                rules = self.core.policy.task_rules()
                self.core.policy.require_task(rules, todo["project_id"], todo["area_id"])
                receipt = db.execute(
                    "SELECT card FROM board_things_imports WHERE things_id=?", (ident,)
                ).fetchone()
                if receipt is None:
                    title = todo["name"]
                    if not text(title) or len(title) > 200:
                        result["skipped"] += 1
                        raise CapabilityError(
                            "invalid_arguments", "Things title must be 1-200 characters"
                        )
                    card = await self.add(
                        "things",
                        {
                            "title": title,
                            "repo": repo,
                            "column": "Ideas",
                            "body": todo.get("notes", "") + "\n\n_Added from Things._",
                        },
                    )
                    # Commit before touching Things. Multiple captures may share
                    # one card through board_add's existing title duplicate guard.
                    with db:
                        db.execute(
                            "INSERT INTO board_things_imports (things_id, card, imported_at) "
                            "VALUES (?, ?, ?)",
                            (ident, card["card"], datetime.now(UTC).isoformat()),
                        )
                    result["imported"].append(
                        {"things_id": ident, "card": card["card"], "title": card["title"]}
                    )
                else:
                    self.core.policy.require_git_repo(self.reference(receipt["card"])[0])
                    result["skipped"] += 1  # Creation skipped; completion is still retried.
                completed = resource(
                    (
                        await self.core.backends.things.call(
                            "things.complete", {"todo_id": ident, **self.core.policy.task_rules()}
                        )
                    ).get("todo")
                )
                if completed["id"] != ident or completed["status"] != "completed":
                    raise CapabilityError("backend_invalid", "Things completion was not confirmed")
                result["completed"] += 1
            except CapabilityError as exc:
                result["errors"].append({"things_id": ident, "code": exc.code})
        return result
