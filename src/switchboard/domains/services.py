"""Typed operations for the configured non-finance service inventory."""

from ..schema import obj, string
from .common import array, output

NUMBER = {"type": "number", "minimum": 0}
PROCESS = obj(
    {"pid": {"type": "integer"}, "cpu_percent": NUMBER, "rss_mb": NUMBER, "name": string(300)},
    ("pid", "cpu_percent", "rss_mb", "name"),
)
SERVICE = obj(
    {
        "name": string(40),
        "runtime": {"type": "string", "enum": ["launchd", "docker", "app", "process", "remote"]},
        "host": string(40),
        "state": string(100),
        "status": {"type": "string", "enum": ["running", "stopped", "unknown"]},
        "running": {"type": "boolean"},
        "pid": {"type": ["integer", "null"]},
        "last_exit": {"type": "integer"},
        "started_at": string(100),
        "restart_count": {"type": "integer", "minimum": 0},
        "docker_health": {"type": "string", "enum": ["starting", "healthy", "unhealthy"]},
        "cpu_percent": NUMBER,
        "rss_mb": NUMBER,
        "probe_status": {"type": "string", "enum": ["up", "degraded", "down", "unknown"]},
        "silenced": {"type": "boolean"},
        "reason": string(100),
    },
    ("name", "runtime", "status", "running", "pid", "silenced"),
)
SERVICE_NAME = {**string(40), "pattern": "^[a-z0-9_-]{1,40}$"}


SNAPSHOT = output(
    load_averages=array(NUMBER),
    cpu_count={"type": ["integer", "null"]},
    uptime_s=NUMBER,
    memory=obj(
        {
            key + "_mb": NUMBER
            for key in (
                "total",
                "used",
                "free",
                "available",
                "compressed",
                "wired",
                "active",
                "cached",
            )
        }
    ),
    swap=obj({key + "_mb": NUMBER for key in ("total", "used", "free")}),
    disks=array(
        obj(
            {
                "name": string(1024),
                "total_gb": NUMBER,
                "free_gb": NUMBER,
                "used_percent": NUMBER,
            },
            ("name", "total_gb", "free_gb", "used_percent"),
        )
    ),
    top_cpu=array(PROCESS),
    top_rss=array(PROCESS),
    unavailable=array(string()),
    hostname=string(253),
    kernel=string(200),
    temperature_c={"type": "number"},
    throttled={**array(string(64)), "maxItems": 8},
    failed_units={**array(string(200)), "maxItems": 20},
    reboot_required={"type": "boolean"},
    # Remote hosts sample CPU busy % and default-interface throughput over ~1 s.
    cpu_percent={"type": "number", "minimum": 0, "maximum": 100},
    network=obj(
        {
            "interface": string(15),
            "rx_bytes_per_s": {"type": "integer"},
            "tx_bytes_per_s": {"type": "integer"},
        },
        ("interface", "rx_bytes_per_s", "tx_bytes_per_s"),
    ),
)
SNAPSHOT["properties"]["load_averages"]["maxItems"] = 3
for _key, _limit in (("top_cpu", 8), ("top_rss", 8), ("disks", 20)):
    SNAPSHOT["properties"][_key]["maxItems"] = _limit

HOST_CHECKS = output(
    dns={
        **array(
            obj(
                {
                    "name": string(253),
                    "ok": {"type": "boolean"},
                    "answers": {**array(string(64)), "maxItems": 4},
                },
                ("name", "ok", "answers"),
            )
        ),
        "maxItems": 20,
    },
    tailscale=obj(
        {
            "ok": {"type": "boolean"},
            "state": string(32),
            "online": {"type": "boolean"},
            "routes": {**array(string(43)), "maxItems": 8},
            "exit_node_offered": {"type": "boolean"},
            "peers_online": {"type": "integer", "minimum": 0},
        },
        ("ok", "state"),
    ),
    kuma=obj(
        {
            "ok": {"type": "boolean"},
            "active": {"type": "integer", "minimum": 0},
            "reason": string(100),
            "not_up": {
                **array(
                    obj(
                        {
                            "name": string(150),
                            "type": string(20),
                            "status": string(20),
                            "last_check": {"type": ["string", "null"], "maxLength": 100},
                        },
                        ("name", "type", "status", "last_check"),
                    )
                ),
                "maxItems": 200,
            },
        },
        ("ok",),
    ),
    updates=obj(
        {
            "count": {"type": "integer", "minimum": 0},
            "packages": {**array(string(200)), "maxItems": 40},
            "lists_age_s": {"type": ["number", "null"], "minimum": 0},
        },
        ("count", "packages", "lists_age_s"),
    ),
)
HOST_CHECKS["required"] += ["dns", "tailscale", "kuma", "updates"]


def register(registry):
    @registry.declare(
        "ops_service_status",
        "Configured service status",
        "Configured service runtime and process status. Use when diagnosing one or all "
        "configured services. Requires ops:diagnose. Returns runtime, process metrics, probe "
        "state and silence. Apps use launchctl list application.<bundle id>. labels. Remote "
        "status batches once per configured host; failed commands report unknown per service.",
        obj({"service": SERVICE_NAME}),
        scope="ops:diagnose",
        timeout_s=30,
        output_schema=output(services=array(SERVICE)),
    )
    async def status(core, client, args):
        return await core.services.status(args.get("service"))

    @registry.declare(
        "ops_service_logs",
        "Redacted service log tail",
        "Configured service logs: redacted tail and literal filtering. Use when diagnosing a "
        "configured service's logs. Requires ops:diagnose. Returns up to 200 redacted lines "
        "from one configured source (default index 0); contains is a literal case-insensitive "
        "filter. Matched counts only the bounded tail.",
        obj(
            {
                "service": SERVICE_NAME,
                "lines": {"type": "integer", "minimum": 1, "maximum": 200},
                "contains": string(80),
                "source": {"type": "integer", "minimum": 0, "maximum": 3},
            },
            ("service",),
        ),
        max_result_chars=128_000,  # 200 lines x 500 chars plus JSON framing.
        scope="ops:diagnose",
        timeout_s=25,
        output_schema=output(
            service=string(40),
            lines=array({"type": "string", "maxLength": 500}),
            truncated={"type": "boolean"},
            matched={"type": "integer", "minimum": 0},
        ),
    )
    async def logs(core, client, args):
        return await core.services.logs(args)

    @registry.declare(
        "ops_host_snapshot",
        "Host resource snapshot",
        "Mac and remote host CPU, memory, swap and disk resources. Use when diagnosing Mac or "
        "configured remote host CPU, memory, swap or disks. Requires ops:diagnose. Returns "
        "load, uptime and top eight processes by CPU/RSS; command basenames only.",
        obj({"host": SERVICE_NAME}),
        scope="ops:diagnose",
        timeout_s=25,
        output_schema=SNAPSHOT,
    )
    async def snapshot(core, client, args):
        return await core.services.snapshot(args.get("host", "mac"))

    @registry.declare(
        "ops_host_checks",
        "Remote host checks",
        "Remote host DNS, Tailscale, Kuma and update checks. Use when diagnosing a configured "
        "remote host's DNS, Tailscale, Kuma and updates. Requires ops:diagnose. Returns bounded "
        "read-only checks from the Pi agent.",
        obj({"host": SERVICE_NAME}, ("host",)),
        scope="ops:diagnose",
        timeout_s=25,
        output_schema=HOST_CHECKS,
    )
    async def checks(core, client, args):
        from .. import remote

        data = await remote.call(core, args["host"], "checks")
        return remote.project({**data, "status": "ok"}, HOST_CHECKS)

    @registry.declare(
        "ops_host_refresh_updates",
        "Refresh remote package lists",
        "Remote host package list refresh without installs. Use when checking a configured "
        "remote host for new updates; installs nothing. Requires ops:restart. Returns the "
        "upgradable list after apt-get update.",
        obj({"host": SERVICE_NAME}, ("host",)),
        scope="ops:restart",
        tier="write",
        timeout_s=200,
        output_schema=output(
            refreshed={"type": "boolean"}, updates=HOST_CHECKS["properties"]["updates"]
        ),
    )
    async def refresh_updates(core, client, args):
        from .. import remote

        data = await remote.call(core, args["host"], "refresh-updates")
        schema = output(refreshed={"type": "boolean"}, updates=HOST_CHECKS["properties"]["updates"])
        return remote.project({**data, "status": "ok"}, schema)

    @registry.declare(
        "ops_service_restart",
        "Restart configured service",
        "Configured launchd, Docker and remote service restart. Use when restarting an "
        "allowlisted launchd/docker/remote service. Requires ops:restart and a reason. Returns "
        "a recovery job (up to 120s) after automatically silencing its probe for ten minutes; "
        "enforces a durable ten-minute cooldown. Poll jobs_get for recovered; Switchboard "
        "cannot restart itself.",
        obj({"service": SERVICE_NAME, "reason": string(200)}, ("service", "reason")),
        scope="ops:restart",
        tier="write",
        idempotent=False,
        timeout_s=105,
        output_schema=output(service=string(40), job_id=string(64)),
    )
    async def restart(core, client, args):
        return await core.services.restart(client, args)
