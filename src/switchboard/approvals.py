"""Exact-argument, expiring, single-use grants. Notifications never grant approval."""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Protocol

from .errors import CapabilityError
from .names import canonical_name


def args_hash(arguments):
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalNotifier(Protocol):
    async def notify(self, approval: dict) -> None: ...


class Approvals:
    def __init__(self, storage, ttl_s=1800, now=time.time):
        self.db = storage.db
        self.ttl_s = ttl_s
        self.now = now

    def request(self, client, capability, arguments):
        capability = canonical_name(capability)
        now = self.now()
        # Summaries identify the exact grant without embedding user/backend prose.
        # Full arguments remain bound by hash and are shown only through safe adapters.
        from .domains.common import sanitize_text

        preview = (
            sanitize_text(arguments["title"], 80)
            if isinstance(arguments.get("title"), str)
            else "exact requested action"
        )
        record = {
            "id": "apr_" + uuid.uuid4().hex,
            "client": client,
            "capability": capability,
            "args_hash": args_hash(arguments),
            "status": "pending",
            "summary": f"Approve {capability} for {client}: {preview}",
            "created_at": now,
            "expires_at": now + self.ttl_s,
        }
        with self.db:
            self.db.execute(
                """INSERT INTO approvals
                (id,client,capability,args_hash,status,summary,created_at,expires_at)
                VALUES (:id,:client,:capability,:args_hash,:status,:summary,:created_at,:expires_at)
            """,
                record,
            )
        return {
            "status": "approval_required",
            "approval_id": record["id"],
            "expires_at": datetime.fromtimestamp(record["expires_at"], UTC).isoformat(),
            "summary": record["summary"],
        }

    def get(self, approval_id):
        row = self.db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise CapabilityError(
                "approval_invalid", "Approval does not exist or is not accessible"
            )
        return self.canonical_record(dict(row))

    @staticmethod
    def canonical_record(row):
        old = row["capability"]
        row["capability"] = canonical_name(old)
        if old != row["capability"]:
            # Old previews could contain arbitrary argument prose. Do not replay it.
            row["summary"] = (
                f"Approve {row['capability']} for {row['client']}: exact requested action"
            )
        return row

    def resolve(self, approval_id, *, actor, approve=True, trusted=False):
        row = self.get(approval_id)
        if (trusted and row["client"] != actor) or (not trusted and actor != "owner"):
            raise CapabilityError("approval_forbidden", "Cannot resolve another client's approval")
        if row["expires_at"] <= self.now():
            raise CapabilityError("approval_expired", "Approval expired; request a new one")
        status = "approved" if approve else "denied"
        with self.db:
            cursor = self.db.execute(
                """UPDATE approvals SET status=?,resolved_at=?
                WHERE id=? AND status='pending' AND expires_at>?""",
                (status, self.now(), approval_id, self.now()),
            )
            if cursor.rowcount != 1:
                raise CapabilityError("approval_invalid", "Approval is already resolved or used")
        return {"status": status, "approval_id": approval_id}

    def consume(self, approval_id, client, capability, arguments):
        row = self.get(approval_id)
        if (
            row["client"] != client
            or row["capability"] != canonical_name(capability)
            or row["args_hash"] != args_hash(arguments)
        ):
            raise CapabilityError(
                "approval_mismatch", "Approval does not match client, tool and arguments"
            )
        if row["expires_at"] <= self.now():
            raise CapabilityError("approval_expired", "Approval expired; request a new one")
        with self.db:
            cursor = self.db.execute(
                """UPDATE approvals SET status='used'
                WHERE id=? AND status='approved' AND expires_at>?""",
                (approval_id, self.now()),
            )
            if cursor.rowcount != 1:
                raise CapabilityError(
                    "approval_invalid", "Approval is not approved or was already used"
                )

    def list(self):
        return [
            self.canonical_record(dict(row))
            for row in self.db.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 100")
        ]
