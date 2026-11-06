"""The job lifecycle belongs to Switchboard, including owner visibility."""

from ..schema import obj, string
from .common import array, output

JOB = obj(
    {
        "job_id": string(128),
        "capability": string(64),
        "client": string(64),
        "status": {"type": "string"},
        "progress": {"type": "number"},
        "result": {},
        "created_at": {"type": "number"},
        "updated_at": {"type": "number"},
    },
    ("job_id", "capability", "client", "status", "progress", "result", "created_at", "updated_at"),
)


def register(registry):
    @registry.declare(
        "jobs_get",
        "Get job",
        "Background job progress and results. Use when checking progress of a long-running "
        "action. Requires your job ID or owner access. Returns job state and result; poll until "
        "terminal.",
        obj({"job_id": string(128)}, ("job_id",)),
        scope="jobs:read",
        output_schema=JOB,
    )
    async def get(core, client, args):
        return core.jobs.get(args["job_id"], client)

    @registry.declare(
        "jobs_list",
        "List jobs",
        "Background job history. Use when finding recent jobs. Requires jobs read access. "
        "Returns your bounded job history; owner sees all clients.",
        obj({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
        scope="jobs:read",
        output_schema=output(jobs=array(JOB)),
    )
    async def list_jobs(core, client, args):
        limit = args.get("limit", 20)
        values = core.jobs.list(client, limit + 1)
        return {
            "status": "ok",
            "jobs": values[:limit],
            "limit": limit,
            "truncated": len(values) > limit,
        }

    @registry.declare(
        "jobs_cancel",
        "Cancel job",
        "Background job cancellation. Use when stopping a queued or running job. Requires your "
        "job ID or owner access. Returns updated state; cancellation is cooperative.",
        obj({"job_id": string(128)}, ("job_id",)),
        scope="jobs:cancel",
        tier="write",
        output_schema=JOB,
    )
    async def cancel(core, client, args):
        return core.jobs.cancel(args["job_id"], client)
