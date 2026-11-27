"""One shared Gitea project, with bounded reads and local actor receipts."""

import asyncio
import base64
import json
import math
import os
import re
import secrets
import shutil
import stat
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .ask_actions import apply_action, validate_action
from .auth import private_dir
from .domains.board import ASK_MEDIA_MAX_BYTES, COLUMNS
from .domains.common import sanitize_text
from .errors import CapabilityError
from .execution import Execution
from .git import Git, invalid, mapping
from .memory import CALL_CONTEXT

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
    sleep = staticmethod(asyncio.sleep)
    clock = staticmethod(time.monotonic)

    def __init__(self, core, *, sleep=None, clock=None):
        self.sleep = sleep or self.sleep
        self.clock = clock or self.clock
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
        # Local presentation cache for offline activity reads; preserve actor receipts.
        with self.core.storage.db:
            self.core.storage.db.execute(
                "INSERT INTO board_cards(issue_id, card, title, column_name) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(issue_id) DO UPDATE SET card=excluded.card, "
                "title=excluded.title, column_name=excluded.column_name",
                (
                    issue_id,
                    f"{repo.split('/', 1)[1]}#{number}",
                    text(issue.get("title")),
                    "Done" if issue["state"] == "closed" else column,
                ),
            )
        return {
            "card": f"{repo.split('/', 1)[1]}#{number}",
            "repo": repo.split("/", 1)[1],
            "number": number,
            "title": text(issue.get("title")),
            "column": "Done" if issue["state"] == "closed" else column,
            "state": issue["state"],
            "updated": text(issue.get("updated_at"), 80),
            "url": text(issue.get("html_url"), 600),
            "asks_open": self.core.storage.db.execute(
                "SELECT COUNT(*) FROM board_asks WHERE issue_id=? AND state='open'", (issue_id,)
            ).fetchone()[0],
            "labels": [text(mapping(label).get("name"), 80) for label in labels[:20]],
            **{
                key: receipt[key] if receipt else None
                for key in ("claimed_by", "last_note", "last_actor")
            },
        }

    async def list(self, client, args):
        await self.core.root_actions.recover()
        project = self.repo(args["project"]) if "project" in args else None
        board, rows, truncated = await self.snapshot()
        groups = {name: [] for name in COLUMNS}
        for issue, repo, column in rows:
            if project is None or repo == project:
                card = self.summary(issue, repo, column)
                card["open_asks"] = self.open_asks(issue["id"])
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
        open_cards = {
            issue["id"]
            for issue, repo, _ in rows
            if issue["state"] == "open" and (project is None or repo == project)
        }
        answered = []
        if open_cards:
            placeholders = ",".join("?" for _ in open_cards)
            recent = self.core.storage.db.execute(
                "SELECT * FROM board_asks WHERE state='answered' "
                f"AND issue_id IN ({placeholders}) AND julianday(answered_at)>=julianday(?) "
                "ORDER BY julianday(answered_at) DESC, rowid DESC LIMIT 10",
                (*sorted(open_cards), (datetime.now(UTC) - timedelta(hours=72)).isoformat()),
            )
            for row in recent:
                ask = self.ask_object(row)
                answered.append(
                    {
                        "ask_id": ask["id"],
                        **({"action": ask["action"]} if "action" in ask else {}),
                        **{
                            k: ask[k]
                            for k in (
                                "card",
                                "from",
                                "prompt",
                                "answer_text",
                                "answered",
                                "answered_by",
                            )
                        },
                    }
                )
        result = {
            "status": "ok",
            "board": board["board"],
            "columns": result,
            "needs_you": needs_you,
            "answered": answered,
            "limit": args.get("limit", 60),
            "truncated": truncated,
        }
        # Whole asks can be large. Keep the advertised 500k result budget without
        # cutting an ask or answer in half; counts still describe the full scan.
        overflow = len(json.dumps(result)) - 475_000
        for column in reversed(result["columns"]):
            while overflow > 0 and column["cards"]:
                overflow -= len(json.dumps(column["cards"].pop()))
                result["truncated"] = True
        return result

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
        await self.core.root_actions.recover()
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
        result = {
            "status": "ok",
            **self.summary(issue, repo, column),
            "asks": [
                self.ask_object(r)
                for r in self.core.storage.db.execute(
                    "SELECT * FROM board_asks WHERE issue_id=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 10",
                    (issue["id"],),
                )
            ],
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
        overflow = len(json.dumps(result)) - 475_000
        while overflow > 0 and result["asks"]:
            overflow -= len(json.dumps(result["asks"].pop()))
            result["truncated"] = True
        while overflow > 0 and result["comments"]:
            overflow -= len(json.dumps(result["comments"].pop(0)))
            result["truncated"] = True
        return result

    def record(self, issue, repo, column, client, note=None, *, claim=False):
        summary = self.summary(issue, repo, column)
        # Leaving the active columns ends a claim; Needs you keeps it so the work returns.
        claimed = (
            client
            if claim
            else None
            if column in {"Ideas", "Up next", "Done"}
            else summary["claimed_by"]
        )
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
                    claimed,
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

    @staticmethod
    def nonblank(value, limit):
        value = text(value, limit)
        if not value:
            raise CapabilityError("invalid_arguments", "Ask text must not be blank")
        return value

    def ask_spec(self, args):
        kind = args["kind"]
        option_args = {
            "action",
            "options",
            "multiple",
            "allow_other",
            "recommended",
            "yes_label",
            "no_label",
            "destructive",
        }
        allowed = {
            "confirm": {"yes_label", "no_label", "recommended", "destructive", "action"},
            "choice": {"options", "multiple", "allow_other", "recommended"},
            "text": set(),
            "form": {"fields"},
            "plan": {"steps", "destructive"},
        }[kind]
        if (option_args | {"fields", "steps"}) & args.keys() - allowed:
            raise CapabilityError("invalid_arguments", "Arguments do not match the ask kind")
        spec = {
            "prompt": self.nonblank(args["prompt"], 300),
            "detail": self.nonblank(args["detail"], 2000) if "detail" in args else None,
            "options": [],
            "pictures": [
                {
                    "id": f"pic{n}",
                    "caption": self.nonblank(p["caption"], 120) if "caption" in p else None,
                }
                for n, p in enumerate(args.get("pictures", []), 1)
            ],
            "multiple": args.get("multiple", False),
            "allow_other": args.get("allow_other", False),
            "recommended": args.get("recommended"),
            "destructive": args.get("destructive", False),
            "fields": [],
            "steps": [],
            "evidence": [
                {
                    "label": self.nonblank(r["label"], 100),
                    "value": self.nonblank(r["value"], 40) if "value" in r else None,
                    "state": r.get("state", "done"),
                }
                for r in args.get("evidence", [])
            ],
        }

        def options(rows, *, detail=False, label_limit=80):
            ids = [r["id"] for r in rows]
            if len(ids) != len(set(ids)) or any(
                re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value) is None for value in ids
            ):
                raise CapabilityError("invalid_arguments", "Option IDs must be unique")
            return [
                {
                    "id": text(r["id"], 32),
                    "label": self.nonblank(r["label"], label_limit),
                    **(
                        {"detail": self.nonblank(r["detail"], 200) if "detail" in r else None}
                        if detail
                        else {}
                    ),
                }
                for r in rows
            ]

        if kind == "confirm":
            spec["options"] = [
                {
                    "id": key,
                    "label": self.nonblank(args.get(key + "_label", label), 40),
                    "detail": None,
                }
                for key, label in (("yes", "Yes"), ("no", "No"))
            ]
        elif kind == "choice":
            if "options" not in args:
                raise CapabilityError("invalid_arguments", "Choice asks require options")
            spec["options"] = options(args["options"], detail=True)
            if sum("image" in r for r in args["options"]) > 8:
                raise CapabilityError("invalid_arguments", "At most eight option pictures per ask")
        elif kind == "plan":
            if "steps" not in args:
                raise CapabilityError("invalid_arguments", "Plan asks require steps")
            spec["steps"] = [
                {**step, "optional": raw.get("optional", False)}
                for step, raw in zip(
                    options(args["steps"], detail=True, label_limit=120), args["steps"], strict=True
                )
            ]
        elif kind == "form":
            if "fields" not in args:
                raise CapabilityError("invalid_arguments", "Form asks require fields")
            ids = [f["id"] for f in args["fields"]]
            if len(ids) != len(set(ids)) or any(
                re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value) is None for value in ids
            ):
                raise CapabilityError("invalid_arguments", "Field IDs must be unique")
            for field in args["fields"]:
                if (field["type"] == "choice") != ("options" in field):
                    raise CapabilityError("invalid_arguments", "Only choice fields require options")
                spec["fields"].append(
                    {
                        "id": text(field["id"], 32),
                        "label": self.nonblank(field["label"], 80),
                        "type": field["type"],
                        "required": field["required"],
                        "placeholder": self.nonblank(field["placeholder"], 80)
                        if "placeholder" in field
                        else None,
                        "options": options(field.get("options", [])),
                    }
                )
        if spec["recommended"] is not None and spec["recommended"] not in {
            o["id"] for o in spec["options"]
        }:
            raise CapabilityError("invalid_arguments", "Recommended option must be an option ID")
        for option in spec["options"]:
            option["image"] = False
        return spec

    def validate_answer(self, kind, spec, answer):
        allowed = {
            "confirm": {"choice"},
            "choice": {"choice", "other"},
            "text": {"text"},
            "form": {"values"},
            "plan": {"approve", "steps"},
        }[kind] | {"note"}
        if kind == "confirm" and spec.get("action", {}).get("type") == "run_as_root":
            allowed.add("signature")
            if "signature" in answer:
                signature = answer["signature"]
                try:
                    if not isinstance(signature, str) or not 1 <= len(signature) <= 200:
                        raise ValueError
                    base64.b64decode(signature, validate=True)
                except ValueError:
                    raise CapabilityError("invalid_arguments", "Invalid root signature") from None
        if answer.keys() - allowed:
            raise CapabilityError("invalid_arguments", "Answer does not match the ask kind")
        result = {}
        if kind == "plan":
            approve = answer.get("approve")
            steps = spec["steps"]
            ids = [s["id"] for s in steps]
            selected = answer.get("steps", ids)
            if (
                type(approve) is not bool
                or (not approve and "steps" in answer)
                or not isinstance(selected, list)
                or any(not isinstance(s, str) for s in selected)
                or len(selected) != len(set(selected))
                or set(selected) - set(ids)
                or (approve and any(not s["optional"] and s["id"] not in selected for s in steps))
            ):
                raise CapabilityError("invalid_arguments", "Approve the required plan steps only")
            result["approve"] = approve
            rendered = "Not yet"
            if approve:
                result["steps"] = [s for s in ids if s in selected]
                omitted = [s["label"] for s in steps if s["id"] not in selected]
                rendered = (
                    f"Approved {len(selected)} of {len(ids)} steps (left out: {', '.join(omitted)})"
                    if omitted
                    else f"Approved all {len(ids)} steps"
                )
        elif kind in {"confirm", "choice"}:
            choices = answer.get("choice", [])
            labels = {o["id"]: o["label"] for o in spec["options"]}
            other = answer.get("other")
            if (
                len(choices) != len(set(choices))
                or set(choices) - labels.keys()
                or (not spec["multiple"] and len(choices) > 1)
                or (not choices and other is None)
                or (other is not None and not spec["allow_other"])
            ):
                raise CapabilityError("invalid_arguments", "Choose the permitted option IDs")
            if "choice" in answer:
                result["choice"] = choices
            parts = [labels[c] for c in choices]
            if other is not None:
                result["other"] = self.nonblank(other, 500)
                parts.append(result["other"])
            rendered = ", ".join(parts)
        elif kind == "text":
            if "text" not in answer:
                raise CapabilityError("invalid_arguments", "Text answer is required")
            result["text"] = rendered = self.nonblank(answer["text"], 4000)
        else:
            fields = {f["id"]: f for f in spec["fields"]}
            values = answer.get("values")
            if (
                values is None
                or values.keys() - fields.keys()
                or any(f["required"] and key not in values for key, f in fields.items())
            ):
                raise CapabilityError("invalid_arguments", "Supply the required form fields only")
            result["values"] = {}
            parts = []
            for key, field in fields.items():
                if key not in values:
                    continue
                value = values[key]
                field_type = field["type"]
                valid = True
                label = value
                if field_type in {"text", "multiline"}:
                    valid = isinstance(value, str) and 1 <= len(value) <= 2000
                    if valid:
                        value = label = self.nonblank(value, 2000)
                elif field_type == "number":
                    valid = type(value) in (int, float) and (
                        not isinstance(value, float) or math.isfinite(value)
                    )
                elif field_type == "toggle":
                    valid = type(value) is bool
                    label = "Yes" if value else "No"
                elif field_type == "choice":
                    labels = {o["id"]: o["label"] for o in field["options"]}
                    valid = isinstance(value, str) and value in labels
                    if valid:
                        label = labels[value]
                elif field_type == "date":
                    valid = (
                        isinstance(value, str)
                        and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None
                    )
                    if valid:
                        try:
                            date.fromisoformat(value)
                        except ValueError:
                            valid = False
                if not valid:
                    raise CapabilityError("invalid_arguments", "Invalid form field value")
                result["values"][key] = value
                parts.append(f"{field['label']}: {label}")
            rendered = "; ".join(parts)
        if "note" in answer:
            result["note"] = self.nonblank(answer["note"], 1000)
            rendered += " — " + result["note"]
        return result, text(rendered, 600)

    def ask_row(self, ask_id):
        row = self.core.storage.db.execute(
            "SELECT * FROM board_asks WHERE ask_id=?", (ask_id,)
        ).fetchone()
        if row is None:
            raise CapabilityError("not_found", "Ask not found")
        return row

    def ask_object(self, row):
        spec = json.loads(row["spec"])
        spec.pop("closed_at", None)
        spec.setdefault("steps", [])
        spec.setdefault("evidence", [])
        spec.setdefault("pictures", [])
        if "action" in spec:
            action = spec["action"]
            if action.get("type") == "run_as_root" and action.get("result") == "running":
                self.core.root_actions.schedule(row["ask_id"])
            spec["action"] = {
                k: v
                for k, v in action.items()
                if k
                in {
                    "type",
                    "summary",
                    "result",
                    "message",
                    "sign_payload",
                    "kind",
                    "recipe",
                    "steps",
                    "job",
                    "exit",
                    "output",
                    "changes",
                }
            }
        for option in spec["options"]:
            option.setdefault("image", False)
        answer, rendered = None, None
        if row["answer"] is not None:
            answer, rendered = self.validate_answer(row["kind"], spec, json.loads(row["answer"]))

        # Sanitize every persisted string, including nested answer values and identities.
        def clean(value):
            if isinstance(value, str):
                return text(value, 4000)
            if isinstance(value, dict):
                return {text(k, 80): clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        result = clean(
            {
                "id": row["ask_id"],
                "card": row["card"],
                "from": row["asker"],
                "kind": row["kind"],
                **spec,
                "state": row["state"],
                "created": row["created_at"],
                "answered": row["answered_at"],
                "answered_by": row["answered_by"],
                "answer": answer,
                "answer_text": rendered,
            }
        )

        if "action" in spec and "sign_payload" in spec["action"]:
            for name in ("sign_payload", "steps", "summary"):
                result["action"][name] = spec["action"][name]
        return result

    def open_asks(self, issue_id, limit=3):
        return [
            self.ask_object(r)
            for r in self.core.storage.db.execute(
                "SELECT * FROM board_asks WHERE issue_id=? AND state='open' "
                "ORDER BY created_at, rowid LIMIT ?",
                (issue_id, limit),
            )
        ]

    @staticmethod
    def read_picture(path):
        # Walk an already resolved absolute path without following any new symlinks.
        # O_NONBLOCK also prevents an exchanged FIFO from hanging the service.
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > ASK_MEDIA_MAX_BYTES:
                    raise ValueError("Invalid picture size or file type")
                data = source.read(ASK_MEDIA_MAX_BYTES + 1)
        finally:
            os.close(directory)
        if len(data) > ASK_MEDIA_MAX_BYTES:
            raise ValueError("Picture too large")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return data, "png", "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return data, "jpg", "image/jpeg"
        raise ValueError("Picture must be PNG or JPEG")

    def picture_inputs(self, args):
        pictures = {}
        roots = [Path(r) for r in self.core.config.board["ask_media_roots"]]
        inputs = [
            *args.get("options", []),
            *({**p, "id": f"pic{n}"} for n, p in enumerate(args.get("pictures", []), 1)),
        ]
        for picture in inputs:
            if "image" not in picture:
                continue
            if picture["id"] in pictures:
                raise CapabilityError("invalid_arguments", "Picture IDs must not overlap")
            try:
                path = Path(picture["image"])
                if not roots or not path.is_absolute():
                    raise ValueError("Pictures disabled or path not absolute")
                path = path.resolve(strict=True)
                if not any(path.is_relative_to(root) for root in roots):
                    raise ValueError("Picture outside configured roots")
                pictures[picture["id"]] = self.read_picture(path)
            except (OSError, ValueError, RuntimeError):
                raise CapabilityError(
                    "invalid_arguments",
                    "Pictures require a regular PNG/JPEG within configured roots, up to 360 KiB",
                ) from None
        return pictures

    def media_directory(self, ask_id):
        return self.core.config.state_dir.resolve() / "ask-media" / ask_id

    def copy_pictures(self, ask_id, pictures):
        if not pictures:
            return
        directory = self.media_directory(ask_id)
        private_dir(directory.parent)
        private_dir(directory)
        for option_id, (data, extension, _) in pictures.items():
            fd = os.open(
                directory / f"{option_id}.{extension}",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(data)

    async def ask_media(self, client, args):
        row = self.ask_row(args["ask_id"])
        spec = json.loads(row["spec"])
        picture_id = args["option_id"]
        has_picture = any(
            o["id"] == picture_id and o.get("image", False) for o in spec["options"]
        ) or any(p["id"] == picture_id for p in spec.get("pictures", []))
        if has_picture:
            directory = self.media_directory(row["ask_id"])
            for extension in ("png", "jpg"):
                try:
                    data, _, media_type = self.read_picture(directory / f"{picture_id}.{extension}")
                except (OSError, ValueError):
                    continue
                return {
                    "status": "ok",
                    "ask_id": row["ask_id"],
                    "option_id": picture_id,
                    "media_type": media_type,
                    "bytes": len(data),
                    "data_base64": base64.b64encode(data).decode("ascii"),
                }
        raise CapabilityError("not_found", "Ask picture not found")

    def prune_ask_media(self, now):
        cutoff = now - timedelta(days=30)
        for row in self.core.storage.db.execute("SELECT * FROM board_asks WHERE state!='open'"):
            closed = row["answered_at"] or json.loads(row["spec"]).get("closed_at")
            # Old withdrawn rows have no closure timestamp and predate picture support.
            if closed and datetime.fromisoformat(closed) < cutoff:
                shutil.rmtree(self.media_directory(row["ask_id"]), ignore_errors=True)

    async def ask(self, client, args):
        spec = self.ask_spec(args)
        repo, number = self.reference(args["card"])
        async with self._write_lock:
            self.core.policy.require_git_repo(repo)
            if "action" in args:
                try:
                    helper_status = None
                    if args["action"]["type"] == "run_as_root":
                        from .elevate import NotInstalled

                        self.core.policy.require_scope(
                            client,
                            "elevate:pair"
                            if args["action"].get("manage") == "add-key"
                            else "elevate:request",
                        )
                        try:
                            helper_status = await self.core.elevate.status()
                        except NotInstalled:
                            raise CapabilityError(
                                "invalid_arguments", "Root helper not installed"
                            ) from None
                    spec["action"] = validate_action(
                        self.core, args["action"], helper_status=helper_status
                    )
                except (OSError, ValueError, TypeError, RuntimeError):
                    raise CapabilityError(
                        "invalid_arguments", "Invalid or unavailable ask action"
                    ) from None
                spec["destructive"] = True
            pictures = self.picture_inputs(args)
            option_pictures = {o["id"] for o in args.get("options", []) if "image" in o}
            for option in spec["options"]:
                option["image"] = option["id"] in option_pictures
            board, issue, column = await self.lookup(repo, number)
            summary = self.summary(issue, repo, column)
            if issue["state"] != "open" or column == "Done":
                raise CapabilityError("invalid_arguments", "reopen the card first")
            if summary["asks_open"] >= 5:
                raise CapabilityError("invalid_arguments", "At most five open asks per card")
            return_column = column
            if column == "Needs you":
                previous = self.core.storage.db.execute(
                    "SELECT return_column FROM board_asks WHERE issue_id=? AND state='open' "
                    "ORDER BY created_at, rowid LIMIT 1",
                    (issue["id"],),
                ).fetchone()
                return_column = (
                    previous["return_column"]
                    if previous
                    else ("In progress" if summary["claimed_by"] else "Up next")
                )
            ask_id = "ask_" + secrets.token_hex(8)
            if spec.get("action", {}).get("type") == "run_as_root":
                from .elevate import sign_action

                sign_action(spec["action"], ask_id, client, spec["prompt"])
            try:
                self.copy_pictures(ask_id, pictures)
                if column != "Needs you":
                    await self.move_ask_card(board, issue, "Needs you")
                lines = [f"**{client}** asks: {spec['prompt']}"]
                if spec["detail"]:
                    lines.extend(["", spec["detail"]])
                if "action" in spec:
                    lines.extend(["", "Switchboard action: " + spec["action"]["summary"]])
                if args["kind"] == "plan":
                    lines.append("")
                    lines.extend(
                        f"{n}. {step['label']}" + (" (optional)" if step["optional"] else "")
                        for n, step in enumerate(spec["steps"], 1)
                    )
                rows = spec["fields"] if args["kind"] == "form" else spec["options"]
                if rows:
                    lines.append("")
                    lines.extend(
                        "- "
                        + r["label"]
                        + (" (suggested)" if r["id"] == spec["recommended"] else "")
                        for r in rows
                    )
                lines.extend(["", f"_Answer in the Server app. Ask {ask_id}._"])
                await self.api.request(
                    f"{self.git.path(repo)}/issues/{number}/comments",
                    method="POST",
                    payload={"body": "\n".join(lines)},
                )
                with self.core.storage.db as db:
                    db.execute(
                        "INSERT INTO board_asks (ask_id, issue_id, card, asker, kind, spec, "
                        "state, return_column, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                        (
                            ask_id,
                            issue["id"],
                            summary["card"],
                            client,
                            args["kind"],
                            json.dumps(spec),
                            return_column,
                            datetime.now(UTC).isoformat(),
                        ),
                    )
            except BaseException:
                shutil.rmtree(self.media_directory(ask_id), ignore_errors=True)
                raise
            self.record(issue, repo, "Needs you", client, spec["prompt"])
            issue = await self.read_issue(repo, number, issue["id"])
            return {
                "status": "ok",
                **self.summary(issue, repo, "Needs you"),
                "ask": self.ask_object(self.ask_row(ask_id)),
            }

    async def ask_wait(self, client, args):
        start = self.clock()
        deadline = start + args.get("timeout_s", 120)
        row = self.ask_row(args["ask_id"])
        await self.core.root_actions.refresh(row["ask_id"])
        row = self.ask_row(row["ask_id"])

        def waiting(row):
            return (
                row["state"] == "open"
                or json.loads(row["spec"]).get("action", {}).get("result") == "running"
            )

        while waiting(row):
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            await self.sleep(min(2, remaining))
            await self.core.root_actions.refresh(args["ask_id"])
            row = self.ask_row(args["ask_id"])
        return {
            "status": "ok",
            "ask": self.ask_object(row),
            "waited_s": max(0, self.clock() - start),
            "timed_out": waiting(row),
        }

    async def move_ask_card(self, board, issue, column):
        await self.api.request(
            f"{board['prefix']}/issues/{issue['id']}/move",
            method="POST",
            payload={"column_id": board["columns"][column]},
        )

    async def finish_ask(self, client, args, *, withdraw=False):
        async with self._write_lock:
            row = self.ask_row(args["ask_id"])
            stored_action = json.loads(row["spec"]).get("action", {})
            pairing = (
                stored_action.get("type") == "run_as_root"
                and stored_action.get("kind") == "add-key"
            )
            if pairing and not withdraw:
                self.core.policy.require_scope(client, "elevate:pair")
            if (
                withdraw
                and row["asker"] != client
                or not withdraw
                and row["asker"] == client
                and not pairing
            ):
                raise CapabilityError(
                    "resource_denied",
                    "Only the asker may withdraw; clients cannot answer their own asks",
                )
            if row["state"] != "open":
                raise CapabilityError(
                    "invalid_arguments",
                    "Ask already answered" if row["state"] == "answered" else "Ask withdrawn",
                )
            answer, rendered = (
                (None, None)
                if withdraw
                else self.validate_answer(row["kind"], json.loads(row["spec"]), args["answer"])
            )
            repo, number = self.reference(row["card"])
            self.core.policy.require_git_repo(repo)
            board, issue, column = await self.lookup(repo, number)
            spec = json.loads(row["spec"])
            if not withdraw and "action" in spec and answer.get("choice") == ["yes"]:
                action = spec["action"]
                if action["type"] == "run_as_root":
                    # The answer and running nonce are durable before contacting the helper.
                    with self.core.storage.db as db:
                        db.execute(
                            "UPDATE board_asks SET state='answered', answer=?, answered_by=?, "
                            "answered_at=? WHERE ask_id=?",
                            (
                                json.dumps(answer),
                                client,
                                datetime.now(UTC).isoformat(),
                                row["ask_id"],
                            ),
                        )
                    await self.core.root_actions.submit(
                        self.ask_row(row["ask_id"]), spec, args["answer"].get("signature")
                    )
                else:
                    action.update(apply_action(self.core, row["ask_id"], action))
                self.core.audit.append(
                    client=client,
                    capability="board_answer",
                    tier="write",
                    decision="allowed",
                    outcome=action["result"],
                    args={
                        "ask_id": row["ask_id"],
                        "action_type": action["type"],
                        **(
                            {
                                "kind": action["kind"],
                                "recipe": action.get("recipe"),
                                "result": action["result"],
                                "exit": action.get("exit"),
                            }
                            if action["type"] == "run_as_root"
                            else {}
                        ),
                    },
                    ctx=CALL_CONTEXT.get() or Execution.new("owner"),
                )
            with self.core.storage.db as db:
                if withdraw:
                    spec["closed_at"] = datetime.now(UTC).isoformat()
                    db.execute(
                        "UPDATE board_asks SET state='withdrawn', spec=? WHERE ask_id=?",
                        (json.dumps(spec), row["ask_id"]),
                    )
                else:
                    db.execute(
                        "UPDATE board_asks SET state='answered', answer=?, answered_by=?, "
                        "answered_at=?, spec=? WHERE ask_id=?",
                        (
                            json.dumps(answer),
                            client,
                            datetime.now(UTC).isoformat(),
                            json.dumps(spec),
                            row["ask_id"],
                        ),
                    )
            if spec.get("action", {}).get("result") == "running":
                self.core.root_actions.schedule(row["ask_id"])
            prompt = json.loads(row["spec"])["prompt"]
            body = (
                f"**{client}** withdrew the ask: {prompt}"
                if withdraw
                else f"**{client}** answered **{row['asker']}**: {rendered}"
            )
            if not withdraw and "action" in spec:
                action = spec["action"]
                body += "\n\nSwitchboard action: " + action["summary"] + ". "
                body += action.get("message", "Not applied (declined)") + "."
            await self.api.request(
                f"{self.git.path(repo)}/issues/{number}/comments",
                method="POST",
                payload={"body": body},
            )
            self.record(issue, repo, column, client, prompt if withdraw else rendered)
            if (
                not self.open_asks(issue["id"], 1)
                and column == "Needs you"
                and issue["state"] == "open"
                and row["return_column"] != "Needs you"
            ):
                await self.move_ask_card(board, issue, row["return_column"])
                column = row["return_column"]
            issue = await self.read_issue(repo, number, issue["id"])
            return {
                "status": "ok",
                **self.summary(issue, repo, column),
                "ask": self.ask_object(self.ask_row(row["ask_id"])),
            }

    async def ask_withdraw(self, client, args):
        return await self.finish_ask(client, args, withdraw=True)

    async def answer(self, client, args):
        result = await self.finish_ask(client, args)
        await self.core.root_actions.refresh(args["ask_id"])
        result["ask"] = self.ask_object(self.ask_row(args["ask_id"]))
        return result

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

    async def _create_things_project(self, name):
        """Owner-configured capture bootstrap, audited even on a manual import."""
        from .domains.tasks import resource

        started = time.monotonic()
        decision, outcome, error_code = "allowed", "cancelled", "cancelled"
        try:
            rules = self.core.policy.task_rules()
            self.core.policy.require_task(rules)
            data = await self.core.backends.things.call(
                "things.add_project", {"title": name, **rules}
            )
            project = resource(data.get("project"), sanitize=False)
            if project["name"] != name or project["status"] != "open" or project["area_id"]:
                raise CapabilityError("backend_invalid", "Things project creation unconfirmed")
            outcome, error_code = "ok", None
        except CapabilityError as exc:
            error_code, outcome = exc.code, "error"
            if exc.code in {"resource_denied", "policy_invalid", "things_resource_denied"}:
                decision = "denied"
            raise
        except Exception:
            error_code, outcome = "internal_error", "error"
            raise
        finally:
            self.core.audit.append(
                client=None,
                capability="tasks_add_project",
                tier="write",
                args={"title": name},
                decision=decision,
                outcome=outcome,
                error_code=error_code,
                duration=time.monotonic() - started,
                ctx=Execution.new("system"),
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
            name = self.api.settings()["board_things_project"]
            try:
                data = await self.core.backends.things.project_todos(name)
            except CapabilityError as exc:
                if exc.code != "things_project_missing":
                    raise
                try:
                    await self._create_things_project(name)
                except CapabilityError as create_error:
                    # Another writer may have won the race; discover once again.
                    if create_error.code != "things_conflict":
                        raise
                data = await self.core.backends.things.project_todos(name)
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
