"""Owner activity projection: separate visibility from diagnostic audit access."""

from ..schema import obj, string
from .common import array

STATE = {"type": "string", "enum": ["up", "slow", "down", "unknown"]}
NULL_TEXT = {"type": ["string", "null"], "maxLength": 240}
EVENT_PROPERTIES = {
    "id": string(160),
    "at": string(80),
    "kind": {"type": "string", "enum": ["agent", "owner", "server"]},
    "actor": NULL_TEXT,
    "verb": string(40),
    "title": string(140),
    "detail": {"type": ["string", "null"], "maxLength": 200},
    "card": NULL_TEXT,
    "service": NULL_TEXT,
    "severity": {"type": "string", "enum": ["info", "good", "warning", "critical"]},
}
EVENT = obj(EVENT_PROPERTIES, EVENT_PROPERTIES)


def register(registry):
    @registry.declare(
        "activity_feed",
        "Owner activity feed",
        "Owner activity timeline: agent work, owner answers and service incidents. "
        "Use when showing Activity or what happened while away. Requires activity:read; "
        "grant only to owner surfaces because it reveals other agents' work. Returns up to "
        "200 sanitized events, newest first, from the last 14 days; default 24 hours.",
        obj(
            {
                "since": string(80),
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "kinds": {**array(EVENT_PROPERTIES["kind"]), "maxItems": 3},
            }
        ),
        scope="activity:read",
        tier="read",
        max_result_chars=300_000,
        output_schema=obj(
            {
                "status": string(),
                "since": string(80),
                "events": {**array(EVENT), "maxItems": 200},
                "truncated": {"type": "boolean"},
            },
            ("status", "since", "events"),
        ),
        memory=None,
        memory_optout="Owner timeline is local workflow history, not shared facts",
    )
    async def feed(core, client, args):
        from ..activity import feed

        return feed(core, args)
