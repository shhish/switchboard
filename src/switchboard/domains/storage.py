"""Storage reads for agents and dashboards, all behind ops:read."""

from ..schema import obj, string
from .common import array, output

NUM = {"type": "number", "minimum": 0}
NULL_NUM = {"type": ["number", "null"], "minimum": 0}
NULL_TEXT = {"type": ["string", "null"], "maxLength": 120}
LABEL = {**string(40), "pattern": "^disk-[a-f0-9]{20}$"}
TREND = obj(
    {
        "bytes_per_day": {"type": ["number", "null"]},
        "days_to_full": NULL_NUM,
        "samples": {"type": "integer", "minimum": 0},
    },
    ("bytes_per_day", "days_to_full", "samples"),
)
SMART = obj(
    {
        "model": {"type": ["string", "null"], "maxLength": 120},
        "enabled": {"type": ["boolean", "null"]},
        "passed": {"type": ["boolean", "null"]},
        "state": {"type": "string", "enum": ["ok", "standby", "unsupported", "disabled"]},
        **{
            key: NULL_NUM
            for key in (
                "temperature_c",
                "power_on_hours",
                "reallocated",
                "pending",
                "uncorrectable",
                "crc_errors",
                "percentage_used",
                "media_errors",
                "available_spare",
            )
        },
        "warnings": {**array(string(200)), "maxItems": 20},
        "notes": {**array(string(200)), "maxItems": 20},
        "sampled_at": NUM,
    }
)
SMART["required"] = list(SMART["properties"])
DISK = obj(
    {
        "id": string(10),
        "health_id": string(40),
        "model": {"type": "string", "maxLength": 120},
        "capacity_bytes": NUM,
        "internal": {"type": "boolean"},
        "protocol": {"type": "string", "maxLength": 40},
        "smart": SMART,
    },
    ("id", "health_id", "model", "capacity_bytes", "internal", "protocol", "smart"),
)
VOLUME = obj(
    {
        "id": string(1024),
        "label": LABEL,
        "name": string(120),
        "mount": string(1024),
        "fs": string(40),
        "role": NULL_TEXT,
        "physical_disk": NULL_TEXT,
        "container_id": NULL_TEXT,
        "total_bytes": NUM,
        "used_bytes": NUM,
        "free_bytes": NUM,
        "purgeable_bytes": NUM,
        "used_percent": {**NUM, "maximum": 100},
        "shares_space_with": {**array(string(1024)), "maxItems": 128},
    }
)
VOLUME["required"] = list(VOLUME["properties"])
CONTAINER = obj({**VOLUME["properties"], "trend": TREND}, [*VOLUME["required"], "trend"])
ALERT = obj(
    {"name": string(80), "status": string(20), "down_since": NUM, "alerted": {"type": "boolean"}},
    ("name", "status", "down_since", "alerted"),
)
STATUS = output(
    host=string(40),
    sampled_at=NUM,
    disks={**array(DISK), "maxItems": 64},
    containers={**array(CONTAINER), "maxItems": 128},
    volumes={**array(VOLUME), "maxItems": 128},
    alerts={**array(ALERT), "maxItems": 192},
)
STATUS["required"] += ["host", "sampled_at", "disks", "containers", "volumes", "alerts"]


def register(registry):
    @registry.declare(
        "storage_status",
        "Disk space, trends and drive health",
        "Disk space, fill trends and SMART drive health for the Mac and remote hosts. "
        "Use when checking disk space, fill trends or SMART health. Requires ops:read "
        "and enabled storage config. Returns disks, shared APFS usage, mounted volumes, "
        "trends and alerts; remote hosts return fill only. SMART caches six hours; "
        "refresh forces a standby-safe read.",
        obj(
            {
                "host": {**string(40), "pattern": "^[a-z0-9_-]{1,40}$"},
                "refresh": {"type": "boolean"},
            }
        ),
        scope="ops:read",
        tier="read",
        timeout_s=180,
        idempotent=True,
        service="storage",
        output_schema=STATUS,
    )
    async def status(core, client, args):
        return await core.disk_health.status(args.get("host", "mac"), args.get("refresh", False))

    @registry.declare(
        "storage_history",
        "Hourly disk fill history",
        "Disk fill history: hourly used-space samples for one container or volume. "
        "Use when charting a disk container or volume's sampled usage. Requires ops:read. "
        "Supply its storage_status label and days (1–35, default 7). Returns at most "
        "840 hourly used_bytes points. APFS volumes share their container series; "
        "missing history returns not_found.",
        obj({"label": LABEL, "days": {"type": "integer", "minimum": 1, "maximum": 35}}, ("label",)),
        scope="ops:read",
        tier="read",
        timeout_s=10,
        idempotent=True,
        service="storage",
        output_schema=output(
            label=LABEL,
            days={"type": "integer"},
            points={
                **array(obj({"sampled_at": NUM, "used_bytes": NUM}, ("sampled_at", "used_bytes"))),
                "maxItems": 840,
            },
        ),
    )
    async def history(core, client, args):
        return core.disk_health.history(args["label"], args.get("days", 7))
