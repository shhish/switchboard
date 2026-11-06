"""Routine tables in Switchboard's state database.

Versions are immutable once written; the owner's approval flips one version to
`active`. Runs and their step journal survive restarts, so a run resumes where
it stopped instead of repeating writes.
"""

import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS routine_versions (
    routine TEXT NOT NULL, version INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE,
    yaml TEXT NOT NULL, definition TEXT NOT NULL, scopes TEXT NOT NULL, author TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, approved_at REAL, approved_by TEXT, note TEXT,
    PRIMARY KEY (routine, version));
CREATE TABLE IF NOT EXISTS routine_runs (
    run_id TEXT PRIMARY KEY, routine TEXT NOT NULL, hash TEXT NOT NULL, trigger TEXT NOT NULL,
    status TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0, cause TEXT, started_at REAL NOT NULL,
    finished_at REAL, error TEXT, trace_id TEXT);
CREATE INDEX IF NOT EXISTS routine_runs_routine ON routine_runs(routine, started_at DESC);
CREATE INDEX IF NOT EXISTS routine_runs_open ON routine_runs(status)
    WHERE status IN ('running', 'waiting');
CREATE TABLE IF NOT EXISTS routine_steps (
    run_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
    detail TEXT, output TEXT, error TEXT, wake_at REAL, started_at REAL NOT NULL, finished_at REAL,
    PRIMARY KEY (run_id, path));
CREATE TABLE IF NOT EXISTS routine_held (
    routine TEXT NOT NULL, trigger INTEGER NOT NULL, accessory TEXT NOT NULL,
    since_ms INTEGER NOT NULL, fired INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (routine, trigger, accessory));
CREATE TABLE IF NOT EXISTS routine_fired (
    routine TEXT NOT NULL, trigger INTEGER NOT NULL, fired_ms INTEGER NOT NULL,
    PRIMARY KEY (routine, trigger));
CREATE TABLE IF NOT EXISTS routine_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

OPEN = ("running", "waiting")


class RoutineStore:
    def __init__(self, storage, clock=time.time):
        self.db = storage.db
        self.clock = clock
        self.db.executescript(SCHEMA)
        self.db.commit()

    # MARK: - Meta

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM routine_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT INTO routine_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    @property
    def paused(self):
        return self.meta("paused", "0") == "1"

    # MARK: - Versions

    def add_version(self, routine, source, definition, scopes, author, hashed):
        existing = self.db.execute(
            "SELECT routine, version, status FROM routine_versions WHERE hash=?", (hashed,)
        ).fetchone()
        if existing:
            return dict(existing) | {"existing": True}
        with self.db:
            row = self.db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM routine_versions WHERE routine=?",
                (routine,),
            ).fetchone()
            self.db.execute(
                "INSERT INTO routine_versions "
                "(routine, version, hash, yaml, definition, scopes, author, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
                (
                    routine,
                    row["v"],
                    hashed,
                    source,
                    json.dumps(definition),
                    json.dumps(sorted(scopes)),
                    author,
                    self.clock(),
                ),
            )
        return {"routine": routine, "version": row["v"], "status": "proposed", "existing": False}

    def version(self, *, routine=None, hashed=None, status=None):
        sql = "SELECT * FROM routine_versions WHERE 1=1"
        args = []
        if routine:
            sql += " AND routine=?"
            args.append(routine)
        if hashed:
            sql += " AND hash=?"
            args.append(hashed)
        if status:
            sql += " AND status=?"
            args.append(status)
        row = self.db.execute(sql + " ORDER BY version DESC LIMIT 1", args).fetchone()
        return self.decode(row)

    def versions(self, routine=None):
        sql = "SELECT * FROM routine_versions"
        args = ()
        if routine:
            sql += " WHERE routine=?"
            args = (routine,)
        return [
            self.decode(r) for r in self.db.execute(sql + " ORDER BY routine, version DESC", args)
        ]

    def active(self):
        return [
            self.decode(r)
            for r in self.db.execute("SELECT * FROM routine_versions WHERE status='active'")
        ]

    @staticmethod
    def decode(row):
        if row is None:
            return None
        out = dict(row)
        out["definition"] = json.loads(out["definition"])
        out["scopes"] = json.loads(out["scopes"])
        return out

    def approve(self, hashed, owner):
        """Activates one version; the routine's previous active version is superseded."""
        version = self.version(hashed=hashed)
        if version is None:
            return None
        with self.db:
            self.db.execute(
                (
                    "UPDATE routine_versions SET status='superseded' "
                    "WHERE routine=? AND status='active'"
                ),
                (version["routine"],),
            )
            self.db.execute(
                (
                    "UPDATE routine_versions SET status='active', approved_at=?, "
                    "approved_by=?, note=NULL WHERE hash=?"
                ),
                (self.clock(), owner, hashed),
            )
        return self.version(hashed=hashed)

    def set_status(self, routine, status, note=None, *, only=("active",)):
        with self.db:
            cur = self.db.execute(
                (
                    "UPDATE routine_versions SET status=?, note=? "
                    f"WHERE routine=? AND status IN ({','.join('?' * len(only))})"
                ),
                (status, note, routine, *only),
            )
        return cur.rowcount

    # MARK: - Runs

    def add_run(self, run_id, routine, hashed, trigger, depth, cause, trace_id):
        with self.db:
            self.db.execute(
                (
                    "INSERT INTO routine_runs "
                    "(run_id, routine, hash, trigger, status, depth, cause, started_at, trace_id) "
                    "VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)"
                ),
                (
                    run_id,
                    routine,
                    hashed,
                    json.dumps(trigger),
                    depth,
                    cause,
                    self.clock(),
                    trace_id,
                ),
            )

    def finish_run(self, run_id, status, error=None):
        with self.db:
            self.db.execute(
                (
                    "UPDATE routine_runs SET status=?, finished_at=?, error=? "
                    "WHERE run_id=? AND status IN ('running', 'waiting')"
                ),
                (status, self.clock(), error, run_id),
            )

    def set_run_status(self, run_id, status):
        with self.db:
            self.db.execute(
                (
                    "UPDATE routine_runs SET status=? "
                    "WHERE run_id=? AND status IN ('running', 'waiting')"
                ),
                (status, run_id),
            )

    def run(self, run_id):
        row = self.db.execute("SELECT * FROM routine_runs WHERE run_id=?", (run_id,)).fetchone()
        return self.decode_run(row)

    def runs(self, routine=None, limit=20):
        sql = "SELECT * FROM routine_runs"
        args = []
        if routine:
            sql += " WHERE routine=?"
            args.append(routine)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        return [self.decode_run(r) for r in self.db.execute(sql, args)]

    def open_runs(self):
        return [
            self.decode_run(r)
            for r in self.db.execute(
                "SELECT * FROM routine_runs "
                "WHERE status IN ('running', 'waiting') ORDER BY started_at"
            )
        ]

    @staticmethod
    def decode_run(row):
        if row is None:
            return None
        out = dict(row)
        out["trigger"] = json.loads(out["trigger"])
        return out

    def recent_failures(self, routine, hashed):
        """Consecutive failed runs of this version, newest first."""
        count = 0
        for row in self.db.execute(
            (
                "SELECT status FROM routine_runs "
                "WHERE routine=? AND hash=? AND status NOT IN ('running', 'waiting') "
                "ORDER BY started_at DESC LIMIT 10"
            ),
            (routine, hashed),
        ):
            if row["status"] not in ("failed", "indeterminate"):
                break
            count += 1
        return count

    def last_started(self, routine):
        row = self.db.execute(
            "SELECT MAX(started_at) AS t FROM routine_runs WHERE routine=?", (routine,)
        ).fetchone()
        return row["t"]

    # MARK: - Journal

    def step(self, run_id, path):
        row = self.db.execute(
            "SELECT * FROM routine_steps WHERE run_id=? AND path=?", (run_id, path)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["output"] = json.loads(out["output"]) if out["output"] else None
        out["detail"] = json.loads(out["detail"]) if out["detail"] else None
        return out

    def steps(self, run_id):
        return [
            dict(r)
            | {
                "output": json.loads(r["output"]) if r["output"] else None,
                "detail": json.loads(r["detail"]) if r["detail"] else None,
            }
            for r in self.db.execute(
                "SELECT * FROM routine_steps WHERE run_id=? ORDER BY started_at, path", (run_id,)
            )
        ]

    def start_step(self, run_id, path, kind, detail=None, wake_at=None):
        with self.db:
            self.db.execute(
                (
                    "INSERT INTO routine_steps "
                    "(run_id, path, kind, status, detail, wake_at, started_at) "
                    "VALUES (?, ?, ?, 'started', ?, ?, ?) "
                    "ON CONFLICT(run_id, path) DO UPDATE SET "
                    "status='started', detail=excluded.detail, wake_at=excluded.wake_at"
                ),
                (
                    run_id,
                    path,
                    kind,
                    json.dumps(detail) if detail is not None else None,
                    wake_at,
                    self.clock(),
                ),
            )

    def finish_step(self, run_id, path, status, output=None, error=None):
        with self.db:
            self.db.execute(
                (
                    "UPDATE routine_steps SET status=?, output=?, error=?, finished_at=? "
                    "WHERE run_id=? AND path=?"
                ),
                (
                    status,
                    json.dumps(output) if output is not None else None,
                    error,
                    self.clock(),
                    run_id,
                    path,
                ),
            )

    # MARK: - Triggers

    def held(self, routine, trigger):
        return {
            r["accessory"]: dict(r)
            for r in self.db.execute(
                "SELECT * FROM routine_held WHERE routine=? AND trigger=?", (routine, trigger)
            )
        }

    def all_held(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM routine_held")]

    def hold(self, routine, trigger, accessory, since_ms):
        with self.db:
            self.db.execute(
                (
                    "INSERT OR IGNORE INTO routine_held "
                    "(routine, trigger, accessory, since_ms) VALUES (?, ?, ?, ?)"
                ),
                (routine, trigger, accessory, since_ms),
            )

    def release(self, routine, trigger, accessory=None):
        with self.db:
            if accessory is None:
                self.db.execute(
                    "DELETE FROM routine_held WHERE routine=? AND trigger=?", (routine, trigger)
                )
            else:
                self.db.execute(
                    "DELETE FROM routine_held WHERE routine=? AND trigger=? AND accessory=?",
                    (routine, trigger, accessory),
                )

    def mark_held_fired(self, routine, trigger, accessory):
        with self.db:
            self.db.execute(
                "UPDATE routine_held SET fired=1 WHERE routine=? AND trigger=? AND accessory=?",
                (routine, trigger, accessory),
            )

    def fired(self, routine, trigger):
        row = self.db.execute(
            "SELECT fired_ms FROM routine_fired WHERE routine=? AND trigger=?", (routine, trigger)
        ).fetchone()
        return row["fired_ms"] if row else None

    def set_fired(self, routine, trigger, ms):
        with self.db:
            self.db.execute(
                "INSERT INTO routine_fired (routine, trigger, fired_ms) VALUES (?, ?, ?) "
                "ON CONFLICT(routine, trigger) DO UPDATE SET fired_ms=excluded.fired_ms",
                (routine, trigger, ms),
            )

    def clear_triggers(self, routine):
        with self.db:
            self.db.execute("DELETE FROM routine_held WHERE routine=?", (routine,))
            self.db.execute("DELETE FROM routine_fired WHERE routine=?", (routine,))
