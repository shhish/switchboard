"""Home DNS filtering (AdGuard Home on the remote host) through the host agent.

Stats are AdGuard's own dashboard numbers, read with a dedicated account the owner
sets up once with scripts/owner-setup-adguard.sh. Pausing is bounded: AdGuard turns
protection back on by itself when the pause ends.
"""

from ..hostcmd import DNS_DOMAIN_PATTERN, DNS_SEARCH_PATTERN
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


def text(max_length):
    return {"type": "string", "maxLength": max_length}


# Absolute end assertion works in both JSON Schema's ECMA regexes and Python.
DOMAIN = {**string(253), "pattern": "^" + DNS_DOMAIN_PATTERN + r"(?![\s\S])"}
MATCH = obj({"rule": text(512), "filter_id": {"type": "integer"}}, ("rule", "filter_id"))
QUERY = obj(
    {
        "time": text(40),
        "client": obj({"ip": text(45), "name": text(120)}, ("ip", "name")),
        "question": obj({"domain": text(253), "type": text(20)}, ("domain", "type")),
        "answer_status": text(40),
        "reason": text(120),
        "rules": {**array(MATCH), "maxItems": 8},
    },
    ("time", "client", "question", "answer_status", "reason", "rules"),
)
QUERY_LOG = output(
    queries={**array(QUERY), "maxItems": 200},
    limit={"type": "integer", "minimum": 1, "maximum": 200},
    truncated={"type": "boolean"},
)
QUERY_LOG["required"] += ["queries", "limit", "truncated"]
FILTER = obj(
    {
        "id": {"type": "integer"},
        "name": text(120),
        "url_host": text(253),
        "enabled": {"type": "boolean"},
        "rules_count": COUNT,
        "last_updated": text(40),
    },
    ("id", "name", "url_host", "enabled", "rules_count", "last_updated"),
)
RULES = output(
    enabled={"type": "boolean"},
    user_rules={**array(text(512)), "maxItems": 1000},
    user_rules_count=COUNT,
    filters={**array(FILTER), "maxItems": 100},
    truncated={"type": "boolean"},
)
RULES["required"] += ["enabled", "user_rules", "user_rules_count", "filters", "truncated"]
RULE_CHANGE = output(
    domain=DOMAIN,
    action={"type": "string", "enum": ["allow", "block", "remove"]},
    rule=text(512),
    changed={"type": "boolean"},
    rules_count=COUNT,
)
RULE_CHANGE["required"] += ["domain", "action", "rule", "changed", "rules_count"]
REFRESH = output(updated=COUNT)
REFRESH["required"] += ["updated"]


async def agent(core, *tokens, schema):
    from .. import remote

    data = await remote.call(core, bridge_host(core), *tokens)
    return remote.project({**data, "status": "ok"}, schema)


def register(registry):
    @registry.declare(
        "dns_query_log",
        "Home DNS query log",
        "AdGuard Home DNS query log and matched rules. Use when finding why a domain was "
        "blocked or which device queried it. Requires ops:read, limit 1-200, filter all or "
        "blocked; optional search uses domain/IP characters, at most 100. Returns time, "
        "client, question, status/reason and rule/filter IDs; filters locally across up to "
        "5 pages on v1.0.0/v0.107.x, with truncation flagged.",
        obj(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "filter": {"type": "string", "enum": ["all", "blocked"]},
                "search": {**string(100), "pattern": "^" + DNS_SEARCH_PATTERN + r"(?![\s\S])"},
            },
            ("limit", "filter"),
        ),
        scope="ops:read",
        service="dns",
        timeout_s=60,
        output_schema=QUERY_LOG,
        max_result_chars=250000,
        memory=MemorySubject("dns", subjects=("dns", "adguard")),
    )
    async def query_log(core, client, args):
        tokens = ["dns-querylog", str(args["limit"]), args["filter"]]
        if "search" in args:
            tokens.append(args["search"])
        return await agent(core, *tokens, schema=QUERY_LOG)

    @registry.declare(
        "dns_rules",
        "Home DNS rules and filter lists",
        "AdGuard Home DNS rules and filter lists. Use when inspecting home DNS filtering. "
        "Requires ops:read. Returns filtering state, bounded user rules and filter lists "
        "with ID, name, URL host only, enabled state, rule count and update time.",
        scope="ops:read",
        service="dns",
        timeout_s=30,
        output_schema=RULES,
        max_result_chars=250000,
        memory=MemorySubject("dns", subjects=("dns", "adguard")),
    )
    async def rules(core, client, args):
        return await agent(core, "dns-rules", schema=RULES)

    def register_rule(name, action, title, use):
        @registry.declare(
            name,
            title,
            f"AdGuard Home DNS user rules: {action}. Use when {use}. "
            "Requires dns:control and a hostname up to 253 characters (optional leading *.); "
            "changes DNS for the whole house. Reversible with dns_allow, dns_block and "
            "dns_rule_remove. Returns the rule, changed state and new user-rule count.",
            obj({"domain": DOMAIN}, ("domain",)),
            scope="dns:control",
            tier="write",
            service="dns",
            timeout_s=30,
            output_schema=RULE_CHANGE,
            max_result_chars=2000,
            memory=MemorySubject("dns", subjects=("dns", "adguard")),
        )
        async def change_rule(core, client, args):
            return await agent(core, "dns-rule", action, args["domain"], schema=RULE_CHANGE)

    register_rule("dns_allow", "allow", "Allow a DNS domain", "allowing a wrongly blocked domain")
    register_rule("dns_block", "block", "Block a DNS domain", "blocking a domain")
    register_rule(
        "dns_rule_remove", "remove", "Remove a DNS domain rule", "removing a domain's user rules"
    )

    @registry.declare(
        "dns_filters_refresh",
        "Refresh home DNS filter lists",
        "AdGuard Home DNS blocking filter lists: refresh. Use when fetching current filter "
        "rules. Requires dns:control; whitelist lists are excluded. "
        "Returns the updated-list count.",
        scope="dns:control",
        tier="write",
        service="dns",
        timeout_s=80,
        idempotent=False,
        output_schema=REFRESH,
        max_result_chars=1000,
        memory=MemorySubject("dns", subjects=("dns", "adguard")),
    )
    async def filters_refresh(core, client, args):
        return await agent(core, "dns-filters-refresh", schema=REFRESH)

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
