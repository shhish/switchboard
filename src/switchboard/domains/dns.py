"""Home DNS filtering (AdGuard Home on the remote host) through the host agent.

Stats are AdGuard's own dashboard numbers, read with a dedicated account the owner
sets up once with scripts/owner-setup-adguard.sh. Pausing is bounded: AdGuard turns
protection back on by itself when the pause ends.
"""

from ..memory_subjects import MemorySubject
from ..schema import obj, string
from .common import array, output
from .console import bridge_host

COUNT = {"type": "integer", "minimum": 0}
TOP = {
    **array(obj({"name": string(253), "count": COUNT}, ("name", "count"))),
    "maxItems": 8,
}
SERIES = {**array(COUNT), "maxItems": 744}

STATS = output(
    protection_enabled={"type": "boolean"},
    paused_ms=COUNT,
    version=string(40),
    time_units={"type": "string", "enum": ["hours", "days"]},
    period_days={"type": "number", "minimum": 0},
    queries=COUNT,
    blocked=COUNT,
    blocked_percent={"type": "number", "minimum": 0, "maximum": 100},
    avg_ms={"type": "number", "minimum": 0},
    series=obj({"queries": SERIES, "blocked": SERIES}, ("queries", "blocked")),
    top_blocked=TOP,
    top_queried=TOP,
    top_clients=TOP,
)
STATS["required"] += ["protection_enabled", "queries", "blocked", "series"]

CONTROL = output(protection_enabled={"type": "boolean"}, paused_minutes=COUNT)
CONTROL["required"] += ["protection_enabled", "paused_minutes"]


async def agent(core, *tokens, schema):
    from .. import remote

    data = await remote.call(core, bridge_host(core), *tokens)
    return remote.project({**data, "status": "ok"}, schema)


def register(registry):
    @registry.declare(
        "dns_stats",
        "Home DNS filtering stats",
        "AdGuard Home DNS filtering statistics and protection state. Use when checking home DNS "
        "filtering (AdGuard Home on the Pi). Requires ops:read. Returns queries, share blocked, "
        "answer time, per-day series, top blocked domains and devices for its 14-day window, "
        "protection state and pause time left.",
        scope="ops:read",
        service="dns",
        timeout_s=30,
        output_schema=STATS,
        memory=MemorySubject("dns", subjects=("dns", "adguard")),
    )
    async def stats(core, client, args):
        return await agent(core, "dns-stats", schema=STATS)

    @registry.declare(
        "dns_pause",
        "Pause DNS filtering",
        "AdGuard Home DNS filtering: temporary pause. Use when a site is wrongly blocked and "
        "the owner wants DNS filtering off briefly. Requires dns:control and 1-120 minutes; "
        "AdGuard resumes by itself and DNS keeps answering. Returns whether protection is on "
        "and the pause length.",
        obj(
            {
                "minutes": {"type": "integer", "minimum": 1, "maximum": 120},
                "reason": string(200),
            },
            ("minutes", "reason"),
        ),
        scope="dns:control",
        tier="write",
        service="dns",
        idempotent=False,
        timeout_s=30,
        output_schema=CONTROL,
        memory=MemorySubject("dns", subjects=("dns", "adguard")),
    )
    async def pause(core, client, args):
        return await agent(core, "dns-pause", str(args["minutes"]), schema=CONTROL)

    @registry.declare(
        "dns_resume",
        "Resume DNS filtering",
        "AdGuard Home DNS filtering: resume protection. Use when a DNS filtering pause should "
        "end early. Requires dns:control. Returns whether AdGuard protection is back on.",
        obj({"reason": string(200)}, ("reason",)),
        scope="dns:control",
        tier="write",
        service="dns",
        timeout_s=30,
        output_schema=CONTROL,
        memory=MemorySubject("dns", subjects=("dns", "adguard")),
    )
    async def resume(core, client, args):
        return await agent(core, "dns-resume", schema=CONTROL)
