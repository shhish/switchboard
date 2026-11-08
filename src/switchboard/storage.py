"""Private SQLite state shared by approvals, jobs, audit and owner operations."""

import os
import sqlite3

from .auth import private_dir


class Storage:
    def __init__(self, directory):
        private_dir(directory)
        path = directory / "state.sqlite3"
        # Create with restrictive permissions before SQLite opens the file.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS storage_samples (
                label TEXT NOT NULL, bucket INTEGER NOT NULL, sampled_at REAL NOT NULL,
                used_bytes INTEGER NOT NULL, total_bytes INTEGER NOT NULL,
                PRIMARY KEY (label, bucket)
            );
            CREATE INDEX IF NOT EXISTS storage_samples_time ON storage_samples(sampled_at);
            CREATE TABLE IF NOT EXISTS storage_smart (
                identity TEXT PRIMARY KEY, sampled_at REAL NOT NULL,
                reading TEXT NOT NULL, baseline TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS storage_inventory (
                host TEXT PRIMARY KEY, sampled_at REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitor_state (
                name TEXT PRIMARY KEY, status TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL, down_since REAL,
                alerted INTEGER NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
                last_alert_at REAL, last_change_at REAL NOT NULL,
                incident_id TEXT
            );
            CREATE TABLE IF NOT EXISTS monitor_silence (
                name TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT NOT NULL,
                client TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitor_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS service_restarts (
                name TEXT PRIMARY KEY, restarted_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert_channel_state (
                channel TEXT PRIMARY KEY, last_success_at REAL, last_failure_at REAL,
                last_error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS alert_outbox (
                id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL,
                dedupe_key TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
                delivered_at REAL, failed_at REAL, superseded_at REAL, last_error_code TEXT,
                UNIQUE(channel, dedupe_key)
            );
            CREATE INDEX IF NOT EXISTS alert_outbox_due ON alert_outbox(next_attempt_at)
                WHERE delivered_at IS NULL AND failed_at IS NULL;
            CREATE TABLE IF NOT EXISTS alert_watches (
                id TEXT PRIMARY KEY, client TEXT NOT NULL, kind TEXT NOT NULL,
                args TEXT NOT NULL, state TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                last_checked_at REAL, fired_at REAL
            );
            CREATE INDEX IF NOT EXISTS alert_watches_due ON alert_watches(last_checked_at)
                WHERE fired_at IS NULL;
            CREATE TABLE IF NOT EXISTS context_aliases (
                alias TEXT NOT NULL, domain TEXT NOT NULL, kind TEXT NOT NULL,
                target_id TEXT NOT NULL, target_name TEXT NOT NULL,
                client TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (alias, domain)
            );
            CREATE TABLE IF NOT EXISTS context_preferences (
                key TEXT PRIMARY KEY, value TEXT NOT NULL,
                client TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, client TEXT NOT NULL, capability TEXT NOT NULL,
                args_hash TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, resolved_at REAL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, capability TEXT NOT NULL, client TEXT NOT NULL,
                status TEXT NOT NULL, progress REAL NOT NULL, result TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            -- v1 kept one JSON record per row in `audit`; Audit.migrate() moves those
            -- rows here once and drops that table.
            CREATE TABLE IF NOT EXISTS audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                ts_ms INTEGER NOT NULL, client TEXT, client_name TEXT, client_version TEXT,
                catalog_hash TEXT, trace_id TEXT, span_id TEXT, parent_span_id TEXT,
                session_key TEXT, turn_key TEXT, episode_id TEXT, origin TEXT,
                causation_id TEXT, capability TEXT, tier TEXT, decision TEXT,
                deny_code TEXT, error_code TEXT, approval_id TEXT, job_id TEXT,
                outcome TEXT, duration_ms INTEGER, args_json TEXT, args_hash TEXT,
                result_bytes INTEGER, asserted INTEGER NOT NULL DEFAULT 0,
                prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_ts ON audit_log(ts_ms);
            CREATE INDEX IF NOT EXISTS audit_client ON audit_log(client, seq);
            CREATE INDEX IF NOT EXISTS audit_capability ON audit_log(capability, ts_ms);
            CREATE INDEX IF NOT EXISTS audit_trace ON audit_log(trace_id);
            CREATE INDEX IF NOT EXISTS audit_cause ON audit_log(causation_id)
                WHERE causation_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS audit_approval ON audit_log(approval_id)
                WHERE approval_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS audit_job ON audit_log(job_id) WHERE job_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS audit_episode ON audit_log(episode_id);
            CREATE INDEX IF NOT EXISTS audit_session ON audit_log(session_key)
                WHERE session_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS audit_problems ON audit_log(ts_ms)
                WHERE decision<>'allowed' OR error_code IS NOT NULL;
            CREATE INDEX IF NOT EXISTS audit_loops
                ON audit_log(client, capability, args_hash, ts_ms);
            CREATE TABLE IF NOT EXISTS audit_anchor (
                kind TEXT NOT NULL, day TEXT, seq INTEGER NOT NULL,
                entry_hash TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (kind, seq), UNIQUE (kind, day)
            );
            CREATE TABLE IF NOT EXISTS audit_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_rollup (
                day TEXT NOT NULL, client TEXT, capability TEXT, outcome TEXT,
                n INTEGER NOT NULL, UNIQUE (day, client, capability, outcome)
            );
            CREATE TABLE IF NOT EXISTS issues (
                id TEXT PRIMARY KEY, client TEXT NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, details TEXT NOT NULL, capability TEXT,
                status TEXT NOT NULL, resolution TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_requests (
                kind TEXT NOT NULL, tmdb_id INTEGER NOT NULL, request_id INTEGER,
                PRIMARY KEY (kind, tmdb_id)
            );
            CREATE TABLE IF NOT EXISTS compress_requests (
                request_id TEXT NOT NULL, client TEXT NOT NULL,
                tmdb_id INTEGER, path TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (request_id, client)
            );
            CREATE INDEX IF NOT EXISTS compress_requests_client_created
                ON compress_requests(client, created_at DESC);
        """)

        # Existing releases predate supersession; migrate without losing retry/dedupe state.
        state_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(monitor_state)")}
        if "notified" not in state_columns:
            self.db.execute(
                "ALTER TABLE monitor_state ADD COLUMN notified INTEGER NOT NULL DEFAULT 0"
            )
            self.db.execute(
                "UPDATE monitor_state SET notified=EXISTS (SELECT 1 FROM alert_outbox "
                "WHERE delivered_at IS NOT NULL "
                "AND substr(dedupe_key, 1, instr(dedupe_key, ':')-1)=monitor_state.incident_id "
                "AND json_extract(payload, '$.kind') IN ('down', 'reminder'))"
            )
            self.db.commit()
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(alert_outbox)")}
        if "superseded_at" not in columns:
            self.db.execute("ALTER TABLE alert_outbox ADD COLUMN superseded_at REAL")
            self.db.commit()

        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(monitor_silence)")}
        if "system" not in columns:
            self.db.execute(
                "ALTER TABLE monitor_silence ADD COLUMN system INTEGER NOT NULL DEFAULT 0"
            )
            self.db.commit()

        from .memory import migrate

        migrate(self.db)

    def close(self):
        self.db.close()
