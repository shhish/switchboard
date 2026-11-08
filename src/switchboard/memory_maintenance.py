"""Deterministic nightly findings and daily owner notifications; never runs a model."""

import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher

from .errors import CapabilityError
from .execution import ulid
from .secretscan import looks_secret

DAY = 86400
FINDING_KINDS = ("duplicate", "contradiction", "stale", "unused", "proposal_backlog")
SCHEMA = """
    CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS memory_findings (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, memory_ids TEXT NOT NULL,
        detail TEXT NOT NULL, found_at REAL NOT NULL, resolved_at REAL,
        resolution TEXT, fingerprint TEXT NOT NULL UNIQUE
    );
    CREATE INDEX IF NOT EXISTS memory_findings_open ON memory_findings(found_at)
        WHERE resolved_at IS NULL;
    CREATE TABLE IF NOT EXISTS memory_usage (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL, at REAL NOT NULL,
        event TEXT NOT NULL, landing_status TEXT, n INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS memory_usage_time ON memory_usage(at, client);
"""


def meta(db, key, default=None):
    row = db.execute("SELECT value FROM memory_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute(
        "INSERT INTO memory_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def usage(memory, client, event, status=None, n=1):
    memory.db.execute(
        "INSERT INTO memory_usage(client,at,event,landing_status,n) VALUES(?,?,?,?,?)",
        (client, time.time(), event, status, n),
    )


def overlap(a, b):
    a, b = " ".join(a.lower().split()), " ".join(b.lower().split())
    x, y = set(a.split()), set(b.split())
    return len(x & y) / max(1, len(x | y)) >= 0.85 or SequenceMatcher(None, a, b).ratio() >= 0.92


class MemoryMaintenance:
    def __init__(self, memory):
        self.memory = memory
        self.core, self.db = memory.core, memory.db

    def finding(self, kind, rows, detail, now):
        rows = sorted(rows, key=lambda r: r["id"])
        ids = [r["id"] for r in rows]
        values = [
            (r["id"], r["content_hash"], r["review_after"] if kind == "stale" else None)
            for r in rows
        ]
        fingerprint = hashlib.sha256(
            json.dumps([kind, values], sort_keys=True).encode()
        ).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO memory_findings VALUES(?,?,?,?,?,NULL,NULL,?)",
            ("finding_" + ulid(), kind, json.dumps(ids), detail[:500], now, fingerprint),
        )

    def tidy(self, now):
        # Rollup windows are bounded by audit_digest's 30-day maximum; knowledge stays durable.
        self.db.execute("DELETE FROM memory_usage WHERE at<?", (now - 35 * DAY,))
        self.db.execute("DELETE FROM memory_incidents WHERE at<?", (now - 7 * DAY,))
        self.db.execute("DELETE FROM memory_reflection_refs WHERE issued_at<?", (now - 3 * DAY,))
        active = [
            dict(r)
            for r in self.db.execute("SELECT * FROM memory WHERE status='active' ORDER BY id")
        ]
        by_id = {r["id"]: r for r in active}
        preferences = defaultdict(list)
        seen = set()
        for row in active:
            if row["review_after"] is not None and row["review_after"] < now:
                self.finding(
                    "stale", [row], "Review date has passed; verify against the source.", now
                )
            if row["use_count"] == 0 and row["recorded_at"] < now - 90 * DAY:
                self.finding("unused", [row], "Active for over 90 days without retrieval.", now)
            if row["kind"] == "preference":
                preferences[(row["subject"], row["title"].casefold())].append(row)
            query = self.memory.matches(row["title"] + " " + row["body"])
            if not query:
                continue
            candidates = self.db.execute(
                "SELECT m.* FROM memory_fts JOIN memory m ON m.rowid=memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND m.subject=? AND m.domain=? AND m.status='active' "
                "AND m.id<>? ORDER BY bm25(memory_fts),m.id LIMIT 20",
                (query, row["subject"], row["domain"], row["id"]),
            )
            for other in candidates:
                pair = tuple(sorted((row["id"], other["id"])))
                if pair in seen:
                    continue
                seen.add(pair)
                if overlap(row["title"] + " " + row["body"], other["title"] + " " + other["body"]):
                    self.finding(
                        "duplicate",
                        [row, dict(other)],
                        "Same subject and overlapping text; consider superseding one record.",
                        now,
                    )
        for rows in preferences.values():
            for row in rows[1:]:
                self.finding(
                    "contradiction",
                    [rows[0], row],
                    "Multiple active preferences share a subject and title.",
                    now,
                )
        for link in self.db.execute(
            "SELECT from_id,to_id FROM memory_link WHERE rel='contradicts' ORDER BY from_id,to_id"
        ):
            if link["from_id"] in by_id and link["to_id"] in by_id:
                self.finding(
                    "contradiction",
                    [by_id[link["from_id"]], by_id[link["to_id"]]],
                    "Active records are linked as contradictory.",
                    now,
                )
        backlog = defaultdict(list)
        for row in self.db.execute(
            (
                "SELECT * FROM memory WHERE status='proposed' AND recorded_at<? "
                "ORDER BY source_client,id"
            ),
            (now - 7 * DAY,),
        ):
            backlog[row["source_client"]].append(dict(row))
        for rows in backlog.values():
            for offset in range(0, len(rows), 100):
                self.finding(
                    "proposal_backlog",
                    rows[offset : offset + 100],
                    "Proposals have waited at least seven days for owner review.",
                    now,
                )
        set_meta(self.db, "last_tidy_at", now)

    def findings(self, client, *, limit=100):
        self.memory.ready()
        self.memory.domains(client)
        result = []
        for row in self.db.execute(
            "SELECT * FROM memory_findings WHERE resolved_at IS NULL ORDER BY found_at,id"
        ):
            ids = json.loads(row["memory_ids"])
            try:
                for ident in ids:
                    self.memory.row(client, ident)
            except CapabilityError:
                continue
            result.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "memory_ids": ids,
                    "detail": row["detail"],
                    "found_at": row["found_at"],
                }
            )
            if len(result) >= limit:
                break
        return result

    def resolve(self, client, args):
        self.memory.ready()
        self.core.policy.require_scope(client, "memory:review")
        if not self.memory.reviewer(client):
            raise CapabilityError("owner_required", "Owner review required")
        rows = []
        for ident in dict.fromkeys(args["finding_ids"]):
            row = self.db.execute(
                "SELECT * FROM memory_findings WHERE id=? AND resolved_at IS NULL", (ident,)
            ).fetchone()
            if not row:
                raise CapabilityError("not_found", "Open finding not found")
            for memory_id in json.loads(row["memory_ids"]):
                self.memory.row(client, memory_id)
            rows.append(row)
        decision = args["decision"]
        target = args.get("with_id")
        if (decision == "supersede_with") != bool(target):
            raise CapabilityError("invalid_arguments", "supersede_with requires with_id only")
        if target:
            replacement = self.memory.row(client, target)
            if replacement["status"] != "active":
                raise CapabilityError("invalid_arguments", "Replacement must be an active memory")
        ids = sorted({i for r in rows for i in json.loads(r["memory_ids"])})
        now = time.time()
        for ident in ids:
            row = self.memory.row(client, ident)
            if decision == "keep" or ident == target:
                continue
            self.memory.require_domain(client, row["domain"], write=True)
            if row["status"] not in ("active", "proposed"):
                continue
            if target:
                if row["domain"] != replacement["domain"]:
                    raise CapabilityError(
                        "invalid_arguments", "Supersession requires the same domain"
                    )
                self.db.execute(
                    "UPDATE memory SET status='superseded',valid_to=? WHERE id=?", (now, ident)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO memory_link VALUES(?,?,'supersedes')", (target, ident)
                )
                self.memory.history(ident, "superseded", client, {"by": target, "valid_to": now})
            else:
                self.db.execute(
                    "UPDATE memory SET status='retracted',valid_to=? WHERE id=?", (now, ident)
                )
                self.memory.history(
                    ident, "retract", client, {"reason": "Owner resolved a memory finding"}
                )
            usage(self.memory, client, "write", "superseded" if target else "retracted")
        for row in rows:
            self.db.execute(
                "UPDATE memory_findings SET resolved_at=?,resolution=? WHERE id=?",
                (now, decision, row["id"]),
            )
        return {"status": "ok", "finding_ids": [r["id"] for r in rows]}

    def digest(self, now, day):
        domains = self.memory.domains("owner")
        proposals = self.db.execute(
            "SELECT id,title,kind FROM memory WHERE status='proposed' AND domain IN ("
            + ",".join("?" for _ in domains)
            + ") AND (? OR source_client='owner') "
            "ORDER BY recorded_at,id",
            (*domains, self.memory.reviewer("owner")),
        ).fetchall()
        findings = self.findings("owner", limit=100000)
        if not (proposals or findings) or not self.core.alerts.channels():
            return
        counts = {kind: sum(r["kind"] == kind for r in findings) for kind in FINDING_KINDS}
        proposal_counts = Counter(row["kind"] for row in proposals)
        lines = [
            "Proposals: " + ", ".join(f"{k}: {v}" for k, v in sorted(proposal_counts.items())),
            "Findings: " + ", ".join(f"{kind}: {n}" for kind, n in counts.items() if n),
        ]
        for row in proposals[:5]:
            title = row["title"] if not looks_secret(row["title"]) else "[redacted]"
            lines.append(f"{row['id']}: {title[:120]}")
        for row in findings[: max(0, 5 - len(proposals))]:
            lines.append(f"{row['id']} ({row['kind']}): {row['detail'][:120]}")
        lines.append("Review: sb memory review")
        ids = self.core.alerts.queue(
            "memory-digest:" + day,
            {
                "title": f"Memory: {len(proposals)} proposals, {len(findings)} findings",
                "body": "\n".join(line for line in lines if line),
                "kind": "memory_digest",
            },
            now,
        )
        if ids:
            set_meta(self.db, "digest_day", day)
            set_meta(self.db, "last_digest_at", now)

    def maintain(self, now):
        if not self.core.config.memory["enabled"]:
            return
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            local = datetime.fromtimestamp(now).astimezone()
            day = local.date().isoformat()
            if local.hour >= 3 and meta(self.db, "tidy_day") != day:
                self.tidy(now)
                set_meta(self.db, "tidy_day", day)
            if (
                local.hour >= self.core.config.memory["digest_hour"]
                and meta(self.db, "digest_day") != day
            ):
                self.digest(now, day)


def stats(memory, viewer, see_all, since_ms, until_ms):
    own = "" if see_all else " AND client=?"
    params = [since_ms / 1000, until_ms / 1000] + ([] if see_all else [viewer])
    clients = {}
    for row in memory.db.execute(
        "SELECT client,event,landing_status,SUM(n) n FROM memory_usage "
        "WHERE at>=? AND at<?" + own + " GROUP BY client,event,landing_status",
        params,
    ):
        record = clients.setdefault(
            row["client"],
            {
                "client": row["client"],
                "brief": 0,
                "search": 0,
                "get": 0,
                "notes": 0,
                "writes": {},
                "approved": 0,
                "rejected": 0,
            },
        )
        if row["event"] == "write":
            record["writes"][row["landing_status"]] = row["n"]
        else:
            record[row["event"]] = row["n"]
    try:
        findings = memory.maintenance.findings(viewer, limit=10000)
    except CapabilityError:
        findings = []
    return {"clients": [clients[k] for k in sorted(clients)], "findings_open": len(findings)}
