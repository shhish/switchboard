"""Switchboard-owned service probes and read-only diagnostics."""

from ..auth import CLIENTS
from ..config import DEFAULT_BACKENDS
from ..doctor import diagnose
from ..schema import obj, string
from .common import array, output

CHECK = obj({"status": string(), "reason": string()})
BACKEND_CHECK = obj(
    {
        **CHECK["properties"],
        "authorization": obj({"events": string(), "reminders": string()}),
        "running": {"type": "boolean"},
        "automation": string(),
    }
)
CHECKS = obj(
    {
        **{
            key: CHECK
            for key in (
                "config_dir",
                "config",
                "policy_file",
                "token_dir",
                "log_dir",
                "alerts_ntfy",
                "alerts_email",
                "alerts",
                "home_control_token",
                "policy",
                "heartbeat",
            )
        },
        "remote_hosts": {
            **array(obj({"host": string(40), **CHECK["properties"]}, ("host", "status", "reason"))),
            "maxItems": 8,
        },
        "probes": obj({"status": string(), "configured": {"type": "integer"}}),
        "audit_chain": obj(
            {
                **CHECK["properties"],
                "rows": {"type": "integer", "minimum": 0},
                "seq": {"type": "integer", "minimum": 0},
            }
        ),
        "monitor": obj(
            {
                "status": string(),
                "enabled": {"type": "boolean"},
                "last_cycle_age_s": {"type": ["number", "null"]},
            }
        ),
    }
)

SERVICE = obj(
    {
        "name": string(40),
        "group": {"type": "string"},
        "status": {"type": "string", "enum": ["up", "degraded", "down", "unknown"]},
        "latency_ms": {"type": "number", "minimum": 0},
        "reason": string(100),
        "http_status": {"type": "integer", "minimum": 100},
    },
    ("name", "group", "status", "latency_ms"),
)


def register(registry):
    @registry.declare(
        "ops_health",
        "Service health",
        "Use when checking local service health. Requires ops read access. Returns cached "
        "non-finance probe status.",
        scope="ops:read",
        timeout_s=35,
        output_schema=obj(
            {
                "status": {"type": "string", "enum": ["ok"]},
                "checked_at": string(),
                "summary": obj(
                    {
                        key: {"type": "integer", "minimum": 0}
                        for key in ("up", "degraded", "down", "unknown")
                    },
                    ("up", "degraded", "down"),
                ),
                "services": array(SERVICE),
            },
            ("status", "checked_at", "summary", "services"),
        ),
    )
    async def health(core, client, args):
        return await core.probes.health()

    @registry.declare(
        "ops_switchboard_status",
        "Switchboard doctor",
        "Use when a tool fails or Switchboard needs diagnosis. Requires ops read access. "
        "Returns configuration and backend health checks.",
        scope="ops:read",
        timeout_s=60,
        output_schema=output(
            checks=CHECKS,
            tokens=obj({name: CHECK for name in CLIENTS}),
            backends=obj({name: BACKEND_CHECK for name in DEFAULT_BACKENDS if name != "health"}),
            upstreams=obj(
                {
                    "orb": obj(
                        {**CHECK["properties"], "clients": obj({name: CHECK for name in CLIENTS})}
                    )
                }
            ),
            limitations={"type": "array", "items": {"type": "string"}},
        ),
    )
    async def status(core, client, args):
        return await diagnose(
            core.config, core.backends, core.policy, core.orb, core.monitor, core=core
        )

    @registry.declare(
        "ops_alerts_status",
        "Monitoring and alerts",
        "Use when checking monitoring incidents and alert delivery. Requires ops read "
        "access. Returns cycle times, incidents and delivery status.",
        scope="ops:read",
        output_schema=obj(
            {
                "status": {"type": "string", "enum": ["ok"]},
                "silences": array(
                    obj(
                        {
                            "probe": string(40),
                            "until": string(100),
                            "reason": string(200),
                            "client": string(40),
                        },
                        ("probe", "until", "reason", "client"),
                    )
                ),
                "heartbeat": obj(
                    {
                        "configured": {"type": "boolean"},
                        "last_ok": {"type": ["boolean", "null"]},
                        "last_at": {"type": ["number", "null"]},
                    },
                    ("configured", "last_ok", "last_at"),
                ),
                "monitor_enabled": {"type": "boolean"},
                "last_cycle_at": {"type": ["number", "null"]},
                "interval_s": {"type": "integer", "minimum": 1},
                "open_incidents": array(
                    obj(
                        {
                            "name": string(40),
                            "status": string(),
                            "down_since": {"type": "number"},
                            "alerted": {"type": "boolean"},
                            "consecutive_failures": {"type": "integer", "minimum": 0},
                        },
                        ("name", "status", "down_since", "alerted", "consecutive_failures"),
                    )
                ),
                "channels": obj(
                    {
                        name: obj(
                            {
                                "last_success_at": {"type": ["number", "null"]},
                                "last_failure_at": {"type": ["number", "null"]},
                                "last_failure_code": {"type": ["string", "null"]},
                            },
                            ("last_success_at", "last_failure_at", "last_failure_code"),
                        )
                        for name in ("ntfy", "email")
                    }
                ),
                "outbox_backlog": {"type": "integer", "minimum": 0},
                "outbox_failed": {"type": "integer", "minimum": 0},
            },
            (
                "status",
                "silences",
                "monitor_enabled",
                "heartbeat",
                "last_cycle_at",
                "interval_s",
                "open_incidents",
                "channels",
                "outbox_backlog",
                "outbox_failed",
            ),
        ),
    )
    async def alerts_status(core, client, args):
        return core.monitor.status()

    @registry.declare(
        "ops_alerts_test",
        "Send test alerts",
        "Use when testing configured notification channels. Requires ops admin access and "
        "provisioned channels. Returns queued test IDs; inspect ops_alerts_status for "
        "delivery.",
        scope="ops:admin",
        tier="write",
        idempotent=False,
        output_schema=obj(
            {"status": {"type": "string", "enum": ["queued"]}, "outbox_ids": array(string(64))},
            ("status", "outbox_ids"),
        ),
    )
    async def alerts_test(core, client, args):
        return {"status": "queued", "outbox_ids": core.alerts.test()}

    @registry.declare(
        "ops_alerts_silence",
        "Silence planned maintenance alerts",
        "Use when planning maintenance to silence down/reminder alerts for a configured "
        "probe or all probes. Requires ops silence access, a reason and 1–240 minutes. "
        "Returns probe and expiry; state tracking and awaited recoveries continue.",
        obj(
            {
                "probe": string(40),
                "minutes": {"type": "integer", "minimum": 1, "maximum": 240},
                "reason": string(200),
            },
            ("probe", "minutes", "reason"),
        ),
        scope="ops:silence",
        tier="write",
        idempotent=False,
        output_schema=output(probe=string(40), until=string(100)),
    )
    async def alerts_silence(core, client, args):
        return core.monitor.silence(args["probe"], args["minutes"], args["reason"], client)

    @registry.declare(
        "ops_alerts_unsilence",
        "End alert silence",
        "Use when maintenance ends to remove a configured probe's silence, or the all "
        "silence. Requires ops silence access. Returns the probe; normal outage alerting "
        "resumes on the next monitor cycle.",
        obj({"probe": string(40)}, ("probe",)),
        scope="ops:silence",
        tier="write",
        output_schema=output(probe=string(40)),
    )
    async def alerts_unsilence(core, client, args):
        return core.monitor.unsilence(args["probe"])
