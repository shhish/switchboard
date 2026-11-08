"""Hash-chained audit log: one typed row per call, plus a redacted JSONL copy.

Every row carries who called (authenticated client plus self-reported client
info), the causal chain (trace, span, origin, causation), what happened
(capability, tier, decision, codes, outcome) and safe arguments. Arguments are
logged by value only when they are short scalars; long free text is reduced to
length plus a hash, and anything that looks like a credential is redacted,
whatever field it arrives in.
"""

import contextlib
import hashlib
import json
import os
import re
import time
from datetime import UTC, date, datetime, timedelta

from .auth import private_dir
from .execution import Execution, ulid
from .secretscan import looks_secret

SENSITIVE = re.compile(r"token|key|secret|password|auth", re.I)
GENESIS = "0" * 64
MAX_TEXT = 120
MAX_ITEMS = 50
MAX_ARGS_BYTES = 8192
RETENTION_DAYS = 180
EPISODE_GAP_MS = 10 * 60 * 1000
COLUMNS = (
    "id",
    "ts_ms",
    "client",
    "client_name",
    "client_version",
    "catalog_hash",
    "trace_id",
    "span_id",
    "parent_span_id",
    "session_key",
    "turn_key",
    "episode_id",
    "origin",
    "causation_id",
    "capability",
    "tier",
    "decision",
    "deny_code",
    "error_code",
    "approval_id",
    "job_id",
    "outcome",
    "duration_ms",
    "args_json",
    "args_hash",
    "result_bytes",
    "asserted",
    "prev_hash",
)
# Text columns that can carry caller-supplied values: scanned and capped like arguments.
CALLER_TEXT = (
    "client_name",
    "client_version",
    "session_key",
    "turn_key",
    "causation_id",
    "capability",
    "tier",
    "decision",
    "error_code",
    "approval_id",
    "job_id",
    "outcome",
)
DENIALS = {"denied", "approval_required"}


def redact(value):
    """Key-name redaction, kept for callers outside the audit log."""
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SENSITIVE.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def safe_args(value, hash_fields=frozenset(), _key=None):
    """Arguments as they may be stored: short scalars verbatim, long text hashed."""
    if _key in hash_fields and isinstance(value, (dict, list)):
        text = canonical(value)
        return {"len": len(text), "sha256": digest(text)[:16]}
    if isinstance(value, dict):
        return {
            safe_key(k): (
                "[REDACTED]" if SENSITIVE.search(str(k)) else safe_args(v, hash_fields, str(k))
            )
            for k, v in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, list):
        return [safe_args(v, hash_fields, _key) for v in value[:MAX_ITEMS]]
    if isinstance(value, str):
        if looks_secret(value):
            return "[REDACTED:secret]"
        if _key in hash_fields or len(value) > MAX_TEXT:
            return {"len": len(value), "sha256": digest(value)[:16]}
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    return f"[{type(value).__name__}]"


def safe_key(key):
    key = str(key)
    return "[REDACTED_KEY]" if looks_secret(key) else key[:64]


def clean(value, limit=160):
    """A caller-influenced text column: secrets redacted, length capped."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return "[REDACTED:secret]" if looks_secret(value) else value[:limit]


def timestamp(ms=None):
    ms = int(time.time() * 1000) if ms is None else ms
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


class Audit:
    def __init__(self, storage, log_dir, catalog_hash=None):
        self.storage = storage
        self.db = storage.db
        private_dir(log_dir)
        self.log_dir = log_dir
        self.path = log_dir / "audit.jsonl"
        self.catalog_hash = catalog_hash
        self.jsonl_day = self.first_day()
        self.migrate()

    # -- writing ---------------------------------------------------------------

    def append(
        self,
        *,
        client,
        capability,
        args=None,
        tier=None,
        decision="denied",
        approval_id=None,
        job_id=None,
        outcome="error",
        duration=0,
        ctx=None,
        error_code=None,
        hash_fields=frozenset(),
        result_bytes=None,
    ):
        ctx = ctx or Execution.new("system" if client in (None, "anonymous") else "agent")
        stored = args if args == "[REDACTED]" else safe_args(args, hash_fields)
        args_json = canonical(stored)
        if len(args_json) > MAX_ARGS_BYTES:
            args_json = canonical({"oversize": len(args_json), "sha256": digest(args_json)[:16]})
        ts_ms = int(time.time() * 1000)
        row = {
            "id": ulid(ts_ms / 1000),
            "ts_ms": ts_ms,
            "client": client,
            "client_name": ctx.client_name,
            "client_version": ctx.client_version,
            "catalog_hash": self.catalog_hash,
            "trace_id": ctx.trace_id,
            "span_id": ctx.span_id,
            "parent_span_id": ctx.parent_span_id,
            "session_key": ctx.session_key,
            "turn_key": ctx.turn_key,
            "episode_id": None,
            "origin": ctx.origin,
            "causation_id": ctx.causation_id,
            "capability": capability,
            "tier": tier,
            "decision": decision,
            "deny_code": error_code if decision in DENIALS and error_code else None,
            "error_code": error_code,
            "approval_id": approval_id,
            "job_id": job_id,
            "outcome": outcome,
            "duration_ms": max(0, round(duration * 1000)),
            "args_json": args_json,
            # Loop detection compares calls with identical arguments, never their values.
            "args_hash": digest(canonical({"c": capability, "a": stored}))[:16],
            "result_bytes": result_bytes,
            "asserted": ctx.asserted,
            "prev_hash": None,
        }
        for column in CALLER_TEXT:
            row[column] = clean(row[column])
        self.rotate_if_new_day()
        with self.db:
            last = self.db.execute(
                "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            row["prev_hash"] = last["entry_hash"] if last else self.chain_start()
            row["episode_id"] = self.episode(client, row)
            entry_hash = digest(canonical(row) + row["prev_hash"])
            self.db.execute(
                f"INSERT INTO audit_log({','.join(COLUMNS)},entry_hash) "
                f"VALUES ({','.join('?' * (len(COLUMNS) + 1))})",
                (*(row[c] for c in COLUMNS), entry_hash),
            )
        record = self.public({**row, "entry_hash": entry_hash})
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as output:
            output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        return record

    def episode(self, client, row):
        """Calls from one client with gaps under ten minutes form an episode.

        An explicit session key wins, so clients that send one get exact grouping."""
        if row["session_key"]:
            return f"s:{client}:{row['session_key']}"[:160]
        last = self.db.execute(
            "SELECT ts_ms, episode_id FROM audit_log WHERE client IS ? ORDER BY seq DESC LIMIT 1",
            (client,),
        ).fetchone()
        if last and last["episode_id"] and row["ts_ms"] - last["ts_ms"] < EPISODE_GAP_MS:
            return last["episode_id"]
        return "e:" + row["id"]

    def chain_start(self):
        anchor = self.db.execute(
            "SELECT entry_hash FROM audit_anchor WHERE kind='prune' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return anchor["entry_hash"] if anchor else GENESIS

    # -- reading ---------------------------------------------------------------

    @staticmethod
    def public(row):
        record = {key: row[key] for key in row.keys() if key not in ("args_json", "seq")}
        record["ts"] = timestamp(row["ts_ms"])
        record["args"] = json.loads(row["args_json"]) if row["args_json"] else None
        record["duration"] = (row["duration_ms"] or 0) / 1000
        return record

    def tail(self, limit=20):
        rows = self.db.execute(
            "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
        return [self.public(row) for row in reversed(rows)]

    def query(self, *, viewer, see_all, filters, limit=50):
        where, params = [], []
        if not see_all:
            where.append("client=?")
            params.append(viewer)
        elif filters.get("client"):
            where.append("client=?")
            params.append(filters["client"])
        for key in (
            "capability",
            "outcome",
            "decision",
            "trace_id",
            "approval_id",
            "job_id",
            "session_key",
            "episode_id",
            "origin",
        ):
            if filters.get(key) is not None:
                where.append(f"{key}=?")
                params.append(filters[key])
        if filters.get("since_ms") is not None:
            where.append("ts_ms>=?")
            params.append(filters["since_ms"])
        if filters.get("until_ms") is not None:
            where.append("ts_ms<?")
            params.append(filters["until_ms"])
        if filters.get("problems_only"):
            where.append("(decision<>'allowed' OR error_code IS NOT NULL)")
        if filters.get("arg_key") and filters.get("arg_value") is not None:
            where.append("json_extract(args_json, ?)=?")
            params.extend([f'$."{filters["arg_key"]}"', filters["arg_value"]])
        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self.db.execute(sql + " ORDER BY seq DESC LIMIT ?", (*params, limit + 1)).fetchall()
        return [self.public(r) for r in rows[:limit]], len(rows) > limit

    def trace(self, *, viewer, see_all, ref, limit=100):
        """The causal neighbourhood of any ID: trace, call, approval, job, cause or episode.

        Links a caller asserted itself (its own traceparent or causation hint) only
        join rows from that same caller, so one client cannot splice its calls into
        another client's chain by reusing a known ID."""
        own = "" if see_all else " AND client=?"
        extra = () if see_all else (viewer,)
        seeds = self.db.execute(
            "SELECT * FROM audit_log WHERE (id=? OR trace_id=? OR approval_id=? OR job_id=? "
            "OR (causation_id=? AND (asserted & 2) = 0) OR episode_id=? OR session_key=?)"
            f"{own} ORDER BY seq LIMIT ?",
            (ref, ref, ref, ref, ref, ref, ref, *extra, limit),
        ).fetchall()
        if not seeds:
            return []
        clauses, params = [], []

        def any_of(column, values, clients=None):
            values = sorted(v for v in values if v)
            if not values:
                return
            clause = f"{column} IN ({','.join('?' * len(values))})"
            if clients is not None:
                clause += f" AND client IN ({','.join('?' * len(clients))})"
            clauses.append(f"({clause})")
            params.extend(values)
            params.extend(sorted(clients or ()))

        seed_clients = {s["client"] for s in seeds}
        # Same trace. A trace Switchboard minted joins every row of it except other
        # clients' asserted copies; a trace the seed asserted joins only its own client.
        trusted = sorted({s["trace_id"] for s in seeds if not s["asserted"] & 1})
        if trusted:
            clauses.append(
                f"(trace_id IN ({','.join('?' * len(trusted))}) AND ((asserted & 1) = 0 "
                f"OR client IN ({','.join('?' * len(seed_clients))})))"
            )
            params.extend(trusted)
            params.extend(sorted(seed_clients))
        for seed in seeds:
            if seed["asserted"] & 1:
                any_of("trace_id", {seed["trace_id"]}, {seed["client"]})
        any_of("approval_id", {s["approval_id"] for s in seeds})
        any_of("job_id", {s["job_id"] for s in seeds})
        any_of("episode_id", {s["episode_id"] for s in seeds})  # client-namespaced already
        # Rows caused by a seed: trusted when Switchboard set the cause, else same caller only.
        ids = sorted(s["id"] for s in seeds)
        clauses.append(
            f"(causation_id IN ({','.join('?' * len(ids))}) AND ((asserted & 2) = 0 "
            f"OR client IN ({','.join('?' * len(seed_clients))})))"
        )
        params.extend(ids)
        params.extend(sorted(seed_clients))
        rows = self.db.execute(
            f"SELECT * FROM audit_log WHERE ({' OR '.join(clauses)}){own} ORDER BY seq LIMIT ?",
            (*params, *extra, limit),
        ).fetchall()
        return [self.public(r) for r in rows]

    def digest_report(self, *, viewer, see_all, since_ms, until_ms):
        """Deterministic activity summary; prose is left to the calling agent."""
        own = "" if see_all else " AND client=?"
        extra = () if see_all else (viewer,)
        window = (since_ms, until_ms, *extra)
        base = f"FROM audit_log WHERE ts_ms>=? AND ts_ms<?{own}"
        db = self.db

        def rows(sql, params=window):
            return [dict(r) for r in db.execute(sql, params).fetchall()]

        span_days = max((until_ms - since_ms) / 86_400_000, 1 / 24)
        baseline_since = since_ms - 7 * 86_400_000
        baseline = {
            r["client"]: r["n"] / 7
            for r in rows(
                f"SELECT client, COUNT(*) n FROM audit_log WHERE ts_ms>=? AND ts_ms<?{own} "
                "GROUP BY client",
                (baseline_since, since_ms, *extra),
            )
        }
        clients = []
        for r in rows(
            "SELECT client, COUNT(*) calls, "
            "SUM(decision<>'allowed') problems, SUM(error_code IS NOT NULL) errors, "
            f"SUM(capability='tools/list') lists {base} GROUP BY client ORDER BY calls DESC"
        ):
            per_day = r["calls"] / span_days
            usual = baseline.get(r["client"])
            r["per_day_vs_7d"] = round(per_day / usual, 2) if usual else None
            clients.append(r)
        for client, usual in baseline.items():
            if usual >= 5 and client not in {c["client"] for c in clients}:
                clients.append({"client": client, "calls": 0, "per_day_vs_7d": 0.0})
        return {
            "clients": clients,
            "capabilities": rows(
                f"SELECT capability, COUNT(*) calls, SUM(error_code IS NOT NULL) errors {base} "
                "GROUP BY capability ORDER BY calls DESC LIMIT 25"
            ),
            "errors": rows(
                f"SELECT error_code code, capability, client, COUNT(*) n {base} "
                "AND error_code IS NOT NULL GROUP BY 1,2,3 ORDER BY n DESC LIMIT 25"
            ),
            "approvals": rows(
                f"SELECT outcome, decision, COUNT(*) n {base} AND (tier='approval' OR "
                "capability IN ('approvals_approve','approvals_deny','approvals_confirm')) "
                "GROUP BY 1,2"
            ),
            "actions": rows(
                "SELECT ts_ms, client, capability, args_json args, outcome, trace_id "
                f"{base} AND tier IN ('write','approval') AND decision='allowed' "
                "ORDER BY seq DESC LIMIT 50"
            ),
            "loops": rows(
                "SELECT client, capability, args_hash, COUNT(*) n, MIN(ts_ms) first_ms "
                f"{base} AND capability NOT IN ('tools/list','transport.request') "
                "GROUP BY client, capability, args_hash, ts_ms/600000 HAVING n>=5 "
                "ORDER BY n DESC LIMIT 20"
            ),
            "catalog_changes": rows(
                f"SELECT catalog_hash, MIN(ts_ms) first_seen_ms {base} "
                "AND catalog_hash IS NOT NULL GROUP BY catalog_hash ORDER BY first_seen_ms"
            ),
        }

    # -- integrity and retention ----------------------------------------------

    def verify(self, *, full=True):
        """Full recomputation by default; full=False starts at the last verified point."""
        start = None if full else self.checkpoint()
        result = verify_db(self.db, start=start)
        if result["ok"]:
            with self.db:
                self.db.executemany(
                    "INSERT OR REPLACE INTO audit_meta VALUES (?, ?)",
                    (("verified_seq", str(result["seq"])), ("verified_hash", result["head"])),
                )
        return result

    def checkpoint(self):
        return read_checkpoint(self.db)

    def maintain(self, now=None):
        """Hourly: daily anchors, incremental verify, 180-day retention with rollups."""
        now = time.time() if now is None else now
        last = self.db.execute(
            "SELECT value FROM monitor_meta WHERE key='audit_maintained_at'"
        ).fetchone()
        if last and now - last["value"] < 3600:
            return False
        today = datetime.fromtimestamp(now, UTC).date()
        with self.db:
            # One anchor per finished day: the last row's hash, to copy off-box later.
            day_end = int(datetime(today.year, today.month, today.day, tzinfo=UTC).timestamp())
            head = self.db.execute(
                "SELECT seq, entry_hash FROM audit_log WHERE ts_ms<? ORDER BY seq DESC LIMIT 1",
                (day_end * 1000,),
            ).fetchone()
            if head:
                self.db.execute(
                    "INSERT OR IGNORE INTO audit_anchor(kind, day, seq, entry_hash, created_at) "
                    "VALUES ('daily', ?, ?, ?, ?)",
                    ((today - timedelta(days=1)).isoformat(), head["seq"], head["entry_hash"], now),
                )
            cutoff_ms = int((now - RETENTION_DAYS * 86400) * 1000)
            cut = self.db.execute(
                "SELECT seq, entry_hash FROM audit_log WHERE ts_ms<? ORDER BY seq DESC LIMIT 1",
                (cutoff_ms,),
            ).fetchone()
            if cut:
                self.db.execute(
                    "INSERT INTO audit_rollup(day, client, capability, outcome, n) "
                    "SELECT date(ts_ms/1000,'unixepoch'), IFNULL(client,''), "
                    "IFNULL(capability,''), IFNULL(outcome,''), COUNT(*) "
                    "FROM audit_log WHERE seq<=? GROUP BY 1,2,3,4 "
                    "ON CONFLICT(day, client, capability, outcome) DO UPDATE SET n=n+excluded.n",
                    (cut["seq"],),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO audit_anchor(kind, day, seq, entry_hash, created_at) "
                    "VALUES ('prune', NULL, ?, ?, ?)",
                    (cut["seq"], cut["entry_hash"], now),
                )
                self.db.execute("DELETE FROM audit_log WHERE seq<=?", (cut["seq"],))
                self.db.execute(
                    "DELETE FROM audit_anchor WHERE kind='daily' AND seq<=?", (cut["seq"],)
                )
            self.db.execute(
                "INSERT OR REPLACE INTO monitor_meta VALUES ('audit_maintained_at', ?)", (now,)
            )
        self.verify(full=False)
        self.prune_files(today)
        return True

    # -- JSONL copy --------------------------------------------------------------

    def first_day(self):
        """UTC day of the current JSONL file's first record (its content day)."""
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            with open(self.path, encoding="utf-8") as handle:
                first = handle.readline()
            if first:
                return datetime.fromisoformat(json.loads(first)["ts"]).astimezone(UTC).date()
        return datetime.now(UTC).date()

    def rotate_if_new_day(self):
        """Start a new JSONL file at the first write of each UTC day, so busy days rotate."""
        today = datetime.now(UTC).date()
        if self.jsonl_day >= today:
            return
        with contextlib.suppress(OSError):
            if self.path.exists():
                target = self.log_dir / f"audit-{self.jsonl_day.isoformat()}.jsonl"
                if target.exists():
                    target = self.log_dir / f"audit-{self.jsonl_day.isoformat()}-{ulid()}.jsonl"
                os.replace(self.path, target)
        self.jsonl_day = today

    def prune_files(self, today):
        keep_after = today - timedelta(days=RETENTION_DAYS)
        for old in self.log_dir.glob("audit-*.jsonl"):
            with contextlib.suppress(ValueError, OSError):
                if date.fromisoformat(old.stem[len("audit-") :][:10]) < keep_after:
                    old.unlink()

    # -- migration -------------------------------------------------------------

    def migrate(self):
        """Move v1 rows (one JSON record each) into typed, chained rows, once.

        Unparseable records are kept as `legacy_unparsed` rows (length and hash only),
        and the v1 table is renamed to `audit_v1`, never dropped."""
        legacy = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit'"
        ).fetchone()
        if not legacy:
            return
        last = self.db.execute(
            "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev = last["entry_hash"] if last else GENESIS
        ts_ms = 0
        with self.db:
            cursor = self.db.execute("SELECT id, record FROM audit ORDER BY id")
            while batch := cursor.fetchmany(500):
                for old in batch:
                    row = dict.fromkeys(COLUMNS)
                    row["asserted"] = 0
                    try:
                        rec = json.loads(old["record"])
                        ts_ms = int(datetime.fromisoformat(rec["ts"]).timestamp() * 1000)
                        args = rec.get("args")
                        stored = args if args == "[REDACTED]" else safe_args(args)
                        row.update(
                            client=clean(rec.get("client"), 64),
                            capability=rec.get("capability"),
                            tier=rec.get("tier"),
                            decision=rec.get("decision"),
                            approval_id=rec.get("approval_id"),
                            job_id=rec.get("job_id"),
                            outcome=rec.get("outcome"),
                            duration_ms=round((rec.get("duration") or 0) * 1000),
                        )
                    except (ValueError, KeyError, TypeError, AttributeError):
                        raw = str(old["record"])
                        stored = {"len": len(raw), "sha256": digest(raw)[:16]}
                        row.update(capability="legacy_unparsed", outcome="legacy_unparsed")
                    for column in CALLER_TEXT:
                        row[column] = clean(row[column])
                    row.update(
                        id=ulid(ts_ms / 1000),
                        ts_ms=ts_ms,
                        args_json=canonical(stored),
                        args_hash=digest(canonical({"c": row["capability"], "a": stored}))[:16],
                        prev_hash=prev,
                    )
                    entry_hash = digest(canonical(row) + prev)
                    self.db.execute(
                        f"INSERT INTO audit_log({','.join(COLUMNS)},entry_hash) "
                        f"VALUES ({','.join('?' * (len(COLUMNS) + 1))})",
                        (*(row[c] for c in COLUMNS), entry_hash),
                    )
                    prev = entry_hash
            self.db.execute("ALTER TABLE audit RENAME TO audit_v1")


def read_checkpoint(db):
    """(seq, hash) of the last verified row, if it still exists and still matches."""
    rows = dict(db.execute("SELECT key, value FROM audit_meta").fetchall())
    try:
        seq, head = int(rows["verified_seq"]), rows["verified_hash"]
    except (KeyError, ValueError, TypeError):
        return None
    row = db.execute("SELECT entry_hash FROM audit_log WHERE seq=?", (seq,)).fetchone()
    return (seq, head) if row and row[0] == head else None


def verify_db(db, start=None, limit=None):
    """Recompute the chain from `start` (seq, hash) or the last pruning anchor.

    Daily anchors above the pruning point must still exist and match, so deleting a
    suffix of anchored history is detected. Works on a read-only connection too."""
    anchor = db.execute(
        "SELECT seq, entry_hash FROM audit_anchor WHERE kind='prune' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    floor = anchor[0] if anchor else 0
    seq, prev = start or ((anchor[0], anchor[1]) if anchor else (0, GENESIS))
    checked = 0
    cursor = db.execute(
        f"SELECT seq, {','.join(COLUMNS)}, entry_hash FROM audit_log WHERE seq>? "
        "ORDER BY seq LIMIT ?",
        (seq, limit or -1),
    )
    for row in cursor:
        data = dict(zip(COLUMNS, row[1:-1], strict=True))
        if data["prev_hash"] != prev or digest(canonical(data) + prev) != row[-1]:
            return {"ok": False, "checked": checked, "broken_at_seq": row[0]}
        prev, seq = row[-1], row[0]
        checked += 1
    broken = db.execute(
        "SELECT a.seq FROM audit_anchor a LEFT JOIN audit_log l ON l.seq=a.seq "
        "WHERE a.kind='daily' AND a.seq>? AND (l.seq IS NULL OR l.entry_hash<>a.entry_hash) "
        "LIMIT 1",
        (floor,),
    ).fetchone()
    if broken:
        return {"ok": False, "checked": checked, "broken_at_seq": broken[0]}
    return {"ok": True, "checked": checked, "seq": seq, "head": prev}
