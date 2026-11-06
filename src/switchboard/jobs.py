"""Persistent local job handles; tasks cooperate with cancellation."""

import asyncio
import json
import time
import uuid

from .errors import CapabilityError
from .names import canonical_name


class Jobs:
    def __init__(self, storage):
        self.db = storage.db
        self.tasks = {}
        # No Phase-2 handler supports resume. Never claim an interrupted action succeeded.
        with self.db:
            self.db.execute(
                """UPDATE jobs SET status='failed',result=?,updated_at=?
                WHERE status='running'""",
                (json.dumps({"reason": "service restarted"}), time.time()),
            )

    def create(self, capability, client):
        capability = canonical_name(capability)
        job_id = "job_" + uuid.uuid4().hex
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                (job_id, capability, client, "queued", 0, None, now, now),
            )
        return {"job_id": job_id, "status": "queued"}

    def get(self, job_id, client):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or (client != "owner" and row["client"] != client):
            raise CapabilityError("job_not_found", "Job does not exist or is not accessible")
        result = dict(row)
        result["capability"] = canonical_name(result["capability"])
        result["job_id"] = result.pop("id")
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def list(self, client, limit=20):
        rows = self.db.execute(
            """SELECT id FROM jobs WHERE (?='owner' OR client=?)
            ORDER BY created_at DESC LIMIT ?""",
            (client, client, limit),
        )
        return [self.get(row["id"], client) for row in rows]

    def update(self, job_id, status, *, progress=0, result=None):
        if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError("Invalid job status")
        if not 0 <= progress <= 1:
            raise ValueError("Job progress must be 0..1")
        with self.db:
            self.db.execute(
                """UPDATE jobs SET status=?,progress=?,result=?,updated_at=?
                WHERE id=? AND status IN ('queued','running')""",
                (status, progress, json.dumps(result, allow_nan=False), time.time(), job_id),
            )

    def cancel(self, job_id, client):
        job = self.get(job_id, client)
        if job["status"] in ("queued", "running"):
            self.update(job_id, "cancelled", result={"reason": "cancelled by client"})
            if task := self.tasks.get(job_id):
                task.cancel()
        return self.get(job_id, client)

    def submit(self, capability, client, operation):
        """operation(job_id) can update progress; remote cancellation needs a future handler."""
        handle = self.create(capability, client)
        job_id = handle["job_id"]

        async def run():
            self.update(job_id, "running")
            try:
                result = await operation(job_id)
                self.update(job_id, "succeeded", progress=1, result=result)
            except asyncio.CancelledError:
                self.update(job_id, "cancelled", result={"reason": "cancelled"})
                raise
            except Exception:
                self.update(job_id, "failed", result={"reason": "handler failed"})
            finally:
                self.tasks.pop(job_id, None)

        task = asyncio.create_task(run())
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return handle

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
