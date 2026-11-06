"""Append-only audit copies with recursive key redaction."""

import json
import os
import re
from datetime import UTC, datetime

from .auth import private_dir

SENSITIVE = re.compile(r"token|key|secret|password|auth", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SENSITIVE.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def timestamp():
    return datetime.now(UTC).isoformat()


class Audit:
    def __init__(self, storage, log_dir):
        self.storage = storage
        private_dir(log_dir)
        self.path = log_dir / "audit.jsonl"

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
    ):
        record = {
            "ts": timestamp(),
            "client": client,
            "capability": capability,
            "args": redact(args),
            "tier": tier,
            "decision": decision,
            "approval_id": approval_id,
            "job_id": job_id,
            "outcome": outcome,
            "duration": round(duration, 6),
        }
        line = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with self.storage.db:
            self.storage.db.execute("INSERT INTO audit(record) VALUES (?)", (line,))
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as output:
            output.write(line + "\n")
        return record

    def tail(self, limit=20):
        rows = self.storage.db.execute(
            "SELECT record FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
        return [json.loads(row["record"]) for row in reversed(rows)]
