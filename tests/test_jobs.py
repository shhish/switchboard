import asyncio

from switchboard.jobs import Jobs
from switchboard.storage import Storage


async def test_job_visibility_cancel(core):
    own = core.jobs.create("test.work", "dot")["job_id"]
    other = core.jobs.create("test.work", "claude")["job_id"]
    rows = (await core.call("dot", "jobs_list", {}))["jobs"]
    assert [r["job_id"] for r in rows] == [own]
    assert len((await core.call("owner", "jobs_list", {}))["jobs"]) == 2
    assert (await core.call("dot", "jobs_get", {"job_id": other}))["error"][
        "code"
    ] == "job_not_found"
    assert (await core.call("dot", "jobs_cancel", {"job_id": other}))["error"][
        "code"
    ] == "job_not_found"
    cancelled = await core.call("dot", "jobs_cancel", {"job_id": own})
    assert cancelled["status"] == "cancelled"
    assert (await core.call("dot", "jobs_cancel", {"job_id": own}))["status"] == "cancelled"
    assert (await core.call("owner", "jobs_cancel", {"job_id": other}))["status"] == "cancelled"


def test_job_persistence_recovery(tmp_path):
    storage = Storage(tmp_path / "state")
    jobs = Jobs(storage)
    running = jobs.create("test.work", "dot")["job_id"]
    finished = jobs.create("test.work", "dot")["job_id"]
    jobs.update(running, "running", progress=0.3)
    jobs.update(finished, "succeeded", progress=1, result={"ok": True})
    storage.close()
    storage = Storage(tmp_path / "state")
    try:
        restored = Jobs(storage)
        job = restored.get(running, "dot")
        assert job["status"] == "failed"
        assert job["result"]["reason"] == "service restarted"
        assert restored.get(finished, "dot")["result"] == {"ok": True}
    finally:
        storage.close()


async def test_real_job_task_success_failure_and_cancel(core):
    async def success(job_id):
        core.jobs.update(job_id, "running", progress=0.5)
        return {"answer": 42}

    handle = core.jobs.submit("test.work", "dot", success)
    await asyncio.gather(*list(core.jobs.tasks.values()))
    assert core.jobs.get(handle["job_id"], "dot")["status"] == "succeeded"

    async def failure(job_id):
        raise ValueError("PRIVATE")

    handle = core.jobs.submit("test.work", "dot", failure)
    await asyncio.gather(*list(core.jobs.tasks.values()))
    assert core.jobs.get(handle["job_id"], "dot")["result"] == {"reason": "handler failed"}
    entered = asyncio.Event()

    async def pending(job_id):
        entered.set()
        await asyncio.Event().wait()

    handle = core.jobs.submit("test.work", "dot", pending)
    await entered.wait()
    task = core.jobs.tasks[handle["job_id"]]
    core.jobs.cancel(handle["job_id"], "dot")
    await asyncio.gather(task, return_exceptions=True)
    assert core.jobs.get(handle["job_id"], "dot")["status"] == "cancelled"
