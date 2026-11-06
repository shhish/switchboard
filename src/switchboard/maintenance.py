"""Read only Media Doctor's owner-published snapshot, never its private state."""

import json
import math
import time
from pathlib import Path

from .auth import read_private
from .errors import CapabilityError
from .schema import obj, string

SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
# Media Doctor publishes on its hourly wake (homelab-ops media.autonomous-triage, :05),
# and a long wake can push the next write later; past two hours it really is stuck.
STALE_AFTER_S = 2 * 3600

LANES = ("requests", "compatibility", "compression")
STAGES = (
    "audio",
    "compatibility",
    "compression",
    "download",
    "handoff",
    "missing",
    "ready",
    "repair",
    "subtitles",
    "verification",
    "waiting",
    "unknown",
)
COUNT = {"type": "integer", "minimum": 0}
TIME = {"type": ["number", "null"], "minimum": 0}
SAVINGS_PROPERTIES = {
    "freed_all_time_bytes": {"type": ["integer", "null"]},
    "jobs_all_time": {"type": ["integer", "null"], "minimum": 0},
    "since": TIME,
    **{
        field: {"type": ["integer", "null"], "minimum": 0}
        for field in (
            "freed_last_7_days_bytes",
            "jobs_last_7_days",
            "freed_last_30_days_bytes",
            "jobs_last_30_days",
            "queued_estimate_bytes",
            "reclaimable_estimate_bytes",
        )
    },
}
LANE = {"type": "string", "enum": list(LANES)}
STATUS_SCHEMA = obj(
    {
        "status": {"type": "string", "enum": ["ok", "degraded"]},
        "generated_at": TIME,
        "age_seconds": TIME,
        "stale": {"type": "boolean"},
        "queues": obj({lane: COUNT for lane in LANES}, LANES),
        "active_work": {
            "type": "array",
            "maxItems": 3,
            "items": obj({"lane": LANE, "last_progress_at": TIME}, ("lane", "last_progress_at")),
        },
        "pending_installs": COUNT,
        "repair_holds": COUNT,
        "savings": obj(SAVINGS_PROPERTIES, tuple(SAVINGS_PROPERTIES)),
        "attention_items": {
            "type": "array",
            "maxItems": 10,
            "items": obj(
                {"title": string(300), "stage": {"type": "string", "enum": list(STAGES)}},
                ("title", "stage"),
            ),
        },
        "attention_total": COUNT,
        "truncated": {"type": "boolean"},
    },
    (
        "status",
        "generated_at",
        "age_seconds",
        "stale",
        "queues",
        "active_work",
        "pending_installs",
        "repair_holds",
        "savings",
        "attention_items",
        "attention_total",
        "truncated",
    ),
)


def optional_object(data, key):
    if key not in data:
        return {}
    value = data[key]
    if not isinstance(value, dict):
        raise ValueError
    return value


def optional_integer(data, key, *, signed=False):
    if key not in data:
        return None
    value = data[key]
    if type(value) is not int or (not signed and value < 0):
        raise ValueError
    return value


def read_savings(data):
    all_time = optional_object(data, "savingsAllTime")
    reclaimable = optional_object(data, "reclaimable")
    last_7_days = optional_object(reclaimable, "last_7_days")
    since = all_time.get("since")
    if since is not None and (
        type(since) not in (int, float) or not math.isfinite(since) or since < 0
    ):
        raise ValueError
    measured = optional_integer(reclaimable, "measured_bytes")
    estimated = optional_integer(reclaimable, "estimated_only_bytes")
    return {
        "freed_all_time_bytes": optional_integer(all_time, "bytes", signed=True),
        "jobs_all_time": optional_integer(all_time, "jobs"),
        "since": since,
        "freed_last_7_days_bytes": optional_integer(last_7_days, "bytes"),
        "jobs_last_7_days": optional_integer(last_7_days, "jobs"),
        "freed_last_30_days_bytes": optional_integer(data, "savingsLast30DaysBytes"),
        "jobs_last_30_days": optional_integer(data, "completedLast30Days"),
        "queued_estimate_bytes": optional_integer(data, "queuedSavingsEstimateBytes"),
        "reclaimable_estimate_bytes": measured + estimated
        if measured is not None and estimated is not None
        else None,
    }


def read_status(path):
    try:
        # Media Doctor's snapshot lists every queued item and passed 1 MiB on 2026-11-05.
        data = json.loads(read_private(Path(path).expanduser(), SNAPSHOT_MAX_BYTES))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CapabilityError(
            "backend_invalid", "maintenance: private snapshot missing or invalid"
        ) from None
    try:
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise ValueError
        generated = data["generatedAt"]
        if type(generated) not in (int, float) or not math.isfinite(generated) or generated <= 0:
            raise ValueError
        now = time.time()
        if generated > now + 60:
            raise ValueError
        lanes, items = data["lanes"], data["items"]
        if not isinstance(lanes, list) or not isinstance(items, list):
            raise ValueError
        queues, active, attention, count = {}, [], [], 0
        for lane in lanes:
            if not isinstance(lane, dict):
                raise ValueError
            name = lane.get("id")
            if not isinstance(name, str):
                raise ValueError
            if name not in LANES:
                continue
            pending = lane["pending"]
            if name in queues or type(pending) is not int or pending < 0:
                raise ValueError
            queues[name] = pending
            if lane.get("status") == "working":
                progress = lane.get("lastProgressAt")
                if progress is not None and (
                    type(progress) not in (int, float)
                    or not math.isfinite(progress)
                    or progress < 0
                ):
                    raise ValueError
                active.append({"lane": name, "last_progress_at": progress})
        if set(queues) != set(LANES):
            raise ValueError
        for row in items:
            if not isinstance(row, dict):
                raise ValueError
            if not row.get("blocker"):
                continue
            count += 1
            if len(attention) < 10:
                title = row.get("title")
                if not isinstance(title, str) or not title.strip():
                    raise ValueError
                # A producer may accidentally supply a path as its title. Never
                # forward directories, free-form details, blockers or playback.
                title = title.replace("\\", "/").rsplit("/", 1)[-1].strip()
                if not title:
                    title = "Untitled"
                stage = row.get("stage")
                attention.append(
                    {
                        "title": title[:300],
                        "stage": stage if isinstance(stage, str) and stage in STAGES else "unknown",
                    }
                )
        installs, holds = data["compressionInstallPending"], data["repairHolds"]
        if any(type(n) is not int or n < 0 for n in (installs, holds)):
            raise ValueError
        age = round(max(0, now - generated), 1)
        stale = age > STALE_AFTER_S
        return {
            "status": "degraded" if stale else "ok",
            "generated_at": generated,
            "age_seconds": age,
            "stale": stale,
            "queues": queues,
            "active_work": active,
            "pending_installs": installs,
            "repair_holds": holds,
            "savings": read_savings(data),
            "attention_items": attention,
            "attention_total": count,
            "truncated": count > len(attention),
        }
    except (KeyError, ValueError, TypeError, OverflowError):
        raise CapabilityError(
            "backend_invalid", "maintenance: unsupported or invalid snapshot"
        ) from None
