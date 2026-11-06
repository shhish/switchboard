"""Compact public field selection; backend bodies never escape wholesale."""

import math
import re
from datetime import UTC, datetime

from ..schema import obj

OUTCOMES = [
    "ok",
    "accepted",
    "done",
    "already_done",
    "ambiguous",
    "not_found",
    "needs_approval",
    "blocked",
    "unavailable",
    "error",
]
STATUS_OUTCOMES = {
    "requested": "accepted",
    "queued": "accepted",
    "running": "accepted",
    "pending": "accepted",
    "approved": "done",
    "executed": "done",
    "encoding": "accepted",
    "encoded_pending_install": "accepted",
    "attention": "blocked",
    "completed": "done",
    "succeeded": "done",
    "installed": "done",
    "already_available": "already_done",
    "already_requested": "already_done",
    "approval_required": "needs_approval",
    "blocklisted": "blocked",
    "resource_denied": "blocked",
    "denied": "blocked",
    "cancelled": "blocked",
    "skipped": "done",
    "failed": "error",
    "error": "error",
    "ambiguous": "ambiguous",
    "not_found": "not_found",
    "unavailable": "unavailable",
    "backend_down": "unavailable",
    "timeout": "unavailable",
}
ENVELOPE = {
    "outcome": {"type": "string", "enum": OUTCOMES},
    "as_of": {"type": "string", "minLength": 1},
    "source": {"type": "string", "minLength": 1},
}
ERROR = obj(
    {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
        "hint": {"type": "string"},
    },
    ("code", "message", "retryable"),
)
# Presentation fields only: identifiers, paths, timestamps and enums must round-trip.
TEXT_LIMITS = {
    "title": 200,
    "name": 300,
    "notes": 2000,
    "overview": 600,
    "description": 600,
    "headline": 300,
    "location": 300,
    "room": 300,
    "home": 300,
    "show": 300,
    "user": 300,
    "product": 300,
    "project_name": 300,
    "area_name": 300,
    "target_name": 300,
    "conditions": 300,
    "short_forecast": 300,
    "forecast": 600,
    "timezone_note": 200,
    "summary": 300,
    "details": 2000,
    "resolution": 500,
    "wind": 300,
    "wind_direction": 300,
    "event": 300,
    "severity": 300,
    "urgency": 300,
}


def sanitize_text(value, limit=300):
    """Treat backend prose as bounded, single-line data, never instructions."""
    value = re.sub(r"[\r\n]+", " ", value)
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value)
    return " ".join(value.split())[:limit]


def sanitize_external(value, field=""):
    if isinstance(value, dict):
        return {key: sanitize_external(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_external(item, field) for item in value]
    if isinstance(value, str):
        limit = TEXT_LIMITS.get(field)
        if field in {"genres", "tags", "names", "areas", "default_name"}:
            limit = 100 if field == "genres" else 300
        if limit:
            return sanitize_text(value, limit)
    return value


def result_envelope(value, *, source="switchboard", arguments=None):
    """One public result contract for all local tools, including early approvals."""
    result = sanitize_external(value)
    result["outcome"] = STATUS_OUTCOMES.get(result.get("status"), "ok")
    observed = result.get("checked_at")
    if type(result.get("generated_at")) in (int, float):
        observed = datetime.fromtimestamp(result["generated_at"], UTC).isoformat()
    result.setdefault("as_of", observed or datetime.now(UTC).isoformat())
    result.setdefault("source", source)
    if any(isinstance(item, list) for item in result.values()):
        result.setdefault("truncated", False)
    if arguments and "limit" in arguments:
        result["limit"] = arguments["limit"]
    return result


def result_schema(schema, *, takes_limit=False):
    """Refine existing domain fields with the shared, closed success envelope."""
    properties = {**schema.get("properties", {}), **ENVELOPE}
    properties.setdefault("status", {"type": "string"})
    required = list(dict.fromkeys([*schema.get("required", ()), "status", *ENVELOPE]))
    if any(prop.get("type") == "array" for prop in properties.values()):
        properties["truncated"] = {"type": "boolean"}
        properties.setdefault("limit", {"type": "integer", "minimum": 1})
        required.append("truncated")
    if takes_limit:
        properties["limit"] = {"type": "integer", "minimum": 1}
        required.append("limit")
    return {**schema, **obj(properties, required)}


def error_result(code, message):
    unavailable = code == "timeout" or code.endswith(
        ("_down", "_timeout", "_unavailable", "_disabled")
    )
    blocked = code in {
        "resource_denied",
        "compress_path_required",
        "scope_denied",
        "unknown_client",
        "policy_invalid",
        "owner_required",
    } or (code.endswith(("_forbidden", "_denied", "_unauthorized")))
    retryable = code in {
        "timeout",
        "backend_down",
        "weather_unavailable",
        "upstream_unavailable",
    } or code.endswith("_timeout")
    return {
        "status": "error",
        "outcome": "unavailable"
        if unavailable
        else "needs_approval"
        if code == "secure_required"
        else "blocked"
        if blocked
        else "not_found"
        if code == "not_found"
        else "error",
        "as_of": datetime.now(UTC).isoformat(),
        "source": "switchboard",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            **({"hint": "use the library path"} if code == "compress_path_required" else {}),
        },
    }


def pick(value, keys):
    return {
        key: sanitize_text(item)
        if isinstance(item, str) and key != "id" and not key.endswith("_id")
        else item
        for key in keys
        if (item := value.get(key)) is not None
        and type(item) in (str, int, float, bool)
        and (not isinstance(item, float) or math.isfinite(item))
    }


def array(items=None):
    return {"type": "array", "items": items or {"type": "object"}}


def selected(fields):
    """Schema for the finite scalar fields selected by pick()."""
    return obj({key: {"type": ["string", "number", "boolean"]} for key in fields})


def output(**properties):
    return obj({"status": {"type": "string"}, **properties}, ("status",))
