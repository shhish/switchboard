"""Owner-only local memory mirror and Claude note importer. Never mines transcripts."""

import asyncio
import os
import re
import stat
from pathlib import Path

import yaml

from .auth import Tokens, read_token
from .errors import CapabilityError
from .execution import Execution
from .memory import CALL_CONTEXT, SENSITIVE, content_hash


def owner(config, client):
    token = read_token(config.token_dir / f"{client}.token")
    if client != "owner" or Tokens(config.token_dir).authenticate("Bearer " + token) != "owner":
        raise CapabilityError("owner_required", "Local owner credential required")


def read_note(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16000:
            raise ValueError("Invalid note file")
        text = stream.read(16001).decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Missing front matter")
    end = lines.index("---", 1)
    front = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(front, dict) or not isinstance(front.get("name"), str):
        raise ValueError("Invalid note front matter")
    return front, "\n".join(lines[end + 1 :]).strip()


def infer_domain(text):
    keywords = {
        "ops": r"\b(service|launchd|docker|server|host)\b",
        "media": r"\b(plex|seerr|movie|show|sonarr|radarr)\b",
        "home": r"\b(homekit|accessory|lamp|lights|homerelay)\b",
        "tasks": r"\b(things|todo|to-do|tasks)\b",
        "calendar": r"\b(calendar|events)\b",
        "books": r"\b(audiobook|ebook|books)\b",
        "console": r"\b(ps5|playstation|console)\b",
        "dns": r"\b(dns|adguard)\b",
        "storage": r"\b(disk|apfs|nvme|smart)\b",
        "git": r"\b(git|gitea|repository)\b",
        "routines": r"\b(routines|automation)\b",
    }
    # Prefer specific domains over the general server vocabulary.
    for domain in [d for d in keywords if d != "ops"] + ["ops"]:
        if re.search(keywords[domain], text, re.I):
            return domain
    return "general"


def import_claude(core, directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise CapabilityError("invalid_arguments", "A regular note directory is required")
    paths = sorted(root.glob("*.md"))
    if len(paths) > 100:
        raise CapabilityError("invalid_arguments", "Import at most 100 notes at a time")
    results, names, pending = [], {}, []
    for path in paths:
        result = None
        ctx = Execution.new("owner")
        item = {}
        try:
            front, body = read_note(path)
            name = front["name"]
            description = front.get("description", name)
            if not isinstance(description, str) or len(name) > 270:
                raise ValueError("Invalid description")
            item = {
                "title": name,
                "body": body,
                "kind": {
                    "user": "preference",
                    "feedback": "procedure",
                    "project": "fact",
                    "reference": "note",
                }.get(front.get("type"), "note"),
                "domain": infer_domain(name + " " + description + " " + body),
                "search_keys": description[:300],
                "evidence_ref": "claude-memory:" + name,
                # One label for the batch, so --approve-source/retract-source claude-memory work.
                "source_label": "claude-memory",
            }
            # Validate lengths rather than silently truncating the source note.
            from .domains.memory import FIELDS
            from .schema import obj, validate

            validate(
                {k: v for k, v in item.items() if k not in ("evidence_ref", "source_label")},
                obj(FIELDS, ("title", "body", "kind", "domain")),
            )
            if SENSITIVE.search(name + " " + description + " " + body):
                raise CapabilityError("sensitive", "Sensitive note")
            token = CALL_CONTEXT.set(ctx)
            try:
                result = core.memory.remember("owner", item, imported=True, source_client="claude")
            finally:
                CALL_CONTEXT.reset(token)
            identifier = result.get("duplicate_of") or result["memory"]["id"]
            # The importer never creates links on an unrelated near-duplicate.
            stored = core.memory.row("owner", identifier)
            if (
                stored["evidence_ref"] == item["evidence_ref"]
                and stored["source_client"] == "claude"
                and stored["content_hash"] == content_hash(core.memory.clean(item))
            ):
                names[name] = identifier
                names[path.stem] = identifier
                pending.append((identifier, re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", body)))
            results.append(
                {"result": "duplicate" if "duplicate_of" in result else "written", "id": identifier}
            )
        except (ValueError, OSError, UnicodeError, yaml.YAMLError, CapabilityError) as exc:
            result = {"status": "error"}
            results.append(
                {
                    "result": "rejected",
                    "reason": exc.code if isinstance(exc, CapabilityError) else "invalid_note",
                }
            )
        finally:
            core.audit.append(
                client="owner",
                capability="memory_import_claude",
                tier="write",
                args=item,
                decision="allowed",
                outcome=result["status"] if result else "error",
                ctx=ctx,
                hash_fields=frozenset(item),
            )
    with core.storage.db:
        for identifier, links in pending:
            for target in dict.fromkeys(links[:10]):
                if target in names and names[target] != identifier:
                    core.storage.db.execute(
                        "INSERT OR IGNORE INTO memory_link VALUES(?,?,'relates')",
                        (identifier, names[target]),
                    )
    return {"status": "ok", "results": results}


def export(core, directory):
    core.memory.ready()
    domains = core.memory.domains("owner")
    root = Path(directory)
    if root.is_symlink():
        raise CapabilityError("invalid_arguments", "Export directory cannot be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows = [
        dict(r)
        for r in core.storage.db.execute("SELECT * FROM memory WHERE status='active' ORDER BY id")
        if r["domain"] in domains
    ]
    visible = {r["id"] for r in rows}
    names = {}
    for record in rows:
        names.setdefault(record["title"], []).append(record["id"])
    count = 0
    for row in rows:
        front = {
            "id": row["id"],
            "kind": row["kind"],
            "domain": row["domain"],
            "subject": row["subject"],
            "source": row["source_client"],
            "recorded_at": row["recorded_at"],
        }
        links = sorted(
            {
                r["to_id"]
                for r in core.storage.db.execute(
                    "SELECT to_id FROM memory_link WHERE from_id=?", (row["id"],)
                )
                if r["to_id"] in visible
            }
        )

        def resolve_link(match, links=links):
            targets = names.get(match.group(1), [])
            return (
                "[[" + targets[0] + "]]" if len(targets) == 1 and targets[0] in links else match[0]
            )

        body = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", resolve_link, row["body"])
        text = (
            "---\n"
            + yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
            + "---\n\n# "
            + row["title"]
            + "\n\n"
            + body
            + "\n"
        )
        if links:
            text += "\n" + "\n".join("[[" + i + "]]" for i in links) + "\n"
        path = root / (row["id"] + ".md")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        count += 1
    # Remove stale mirror files only when their names belong to our generated namespace.
    for path in root.glob("mem_*.md"):
        if (
            re.fullmatch(r"mem_[0-9A-HJKMNP-TV-Z]{26}\.md", path.name)
            and path.stem not in visible
            and not path.is_symlink()
        ):
            path.unlink()
    return {"status": "ok", "exported": count}


async def execute(args, config):
    from .core import Core

    if args.action == "brief":
        from .cli import request

        try:
            async with asyncio.timeout(3):
                result = await request(
                    config,
                    args.client,
                    "POST",
                    "/v1/call/memory_brief",
                    {"domain": args.domain} if args.domain else {},
                )
            if "error" not in result and isinstance(result.get("brief"), str):
                return result
        except Exception:
            pass
        return {"status": "ok", "brief": ""}
    owner(config, args.client)
    core = Core(config)
    try:
        core.memory.ready()
        core.policy.require_scope("owner", "memory:review")
        if args.action == "import-claude":
            return import_claude(core, args.directory)
        if args.action == "export":
            result = export(core, args.directory)
            core.audit.append(
                client="owner",
                capability="memory_export",
                args={},
                tier="read",
                decision="allowed",
                outcome="ok",
            )
            return result
        if args.action == "retract-source":
            rows = core.storage.db.execute(
                (
                    "SELECT id FROM memory WHERE "
                    "COALESCE(source_label,evidence_ref)=? AND status IN "
                    "('active','proposed') ORDER BY id"
                ),
                (args.source_label,),
            ).fetchall()
            results = [
                await core.call("owner", "memory_retract", {"id": r["id"], "reason": args.reason})
                for r in rows
            ]
            return {
                "status": "error" if any("error" in r for r in results) else "ok",
                "results": results,
            }
        for flag, decision in (
            ("keep_finding", "keep"),
            ("retract_finding", "retract"),
            ("supersede_finding", "supersede_with"),
        ):
            if finding_ids := getattr(args, flag, None):
                return await core.call(
                    "owner",
                    "memory_resolve",
                    {
                        "finding_ids": finding_ids,
                        "decision": decision,
                        **({"with_id": args.with_id} if args.with_id else {}),
                    },
                )
        if args.with_id:
            raise CapabilityError("invalid_arguments", "with-id requires supersede-finding")
        ids, decision = args.approve or args.reject, "approve" if args.approve else "reject"
        if args.approve_source:
            ids = [
                r["id"]
                for r in core.storage.db.execute(
                    (
                        "SELECT id FROM memory WHERE status='proposed' AND "
                        "COALESCE(source_label,evidence_ref)=? ORDER BY id"
                    ),
                    (args.approve_source,),
                )
            ]
            decision = "approve"
        if ids:
            results = [
                await core.call(
                    "owner", "memory_review", {"ids": ids[i : i + 100], "decision": decision}
                )
                for i in range(0, len(ids), 100)
            ]
            return {
                "status": "error" if any("error" in r for r in results) else "ok",
                "results": results,
            }
        rows = [
            dict(r)
            for r in core.storage.db.execute(
                "SELECT id,source_client,kind,title,domain FROM memory WHERE "
                "status='proposed' ORDER BY recorded_at,id"
            )
            if r["domain"] in core.memory.domains("owner")
        ]
        core.audit.append(
            client="owner",
            capability="memory_review_list",
            args={},
            tier="read",
            decision="allowed",
            outcome="ok",
        )
        return {
            "status": "ok",
            "proposed": rows,
            "findings": core.memory.maintenance.findings("owner"),
        }
    finally:
        # No background tasks or backend sessions are opened for these local operations.
        core.storage.close()
