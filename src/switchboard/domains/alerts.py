"""Client-owned, one-off notifications delivered outside the chat."""

from ..schema import obj, string
from .common import array, output

WATCH_ARGS = {
    "kind": {"type": "string", "enum": ["media_available", "download_stalled"]},
    "tmdb_id": {"type": "integer", "minimum": 1},
    "media": {"type": "string", "enum": ["movie", "show"]},
    "season": {"type": "integer", "minimum": 0},
    "episode": {"type": "integer", "minimum": 1},
    "title_contains": string(200),
    "note": string(200),
    "expires_days": {"type": "integer", "minimum": 1, "maximum": 60},
}
WATCH = obj(
    {
        **WATCH_ARGS,
        "watch_id": string(64),
        "client": string(40),
        "created_at": {"type": "number"},
        "expires_at": {"type": "number"},
        "last_checked_at": {"type": ["number", "null"]},
        "fired_at": {"type": ["number", "null"]},
    },
    ("kind", "watch_id", "client", "created_at", "expires_at", "last_checked_at", "fired_at"),
)


def register(registry):
    @registry.declare(
        "alerts_watch_add",
        "Watch media or a download",
        "Use when asked to tell me when media arrives or let me know if a download gets stuck. "
        "Requires a resolved TMDB title or download title fragment. Returns a watch ID; "
        "one notification arrives by phone/email, not in chat. Defaults to 14 days.",
        obj(WATCH_ARGS, ("kind",)),
        scope="media:read",
        tier="write",
        idempotent=False,
        timeout_s=90,
        service="media+overseerr+plex",
        output_schema=output(watch_id=string(64)),
    )
    async def add(core, client, args):
        return await core.watches.add(client, args)

    @registry.declare(
        "alerts_watch_list",
        "List notification watches",
        "Use when checking one-off notification requests. Requires media read access. "
        "Returns your active and recently fired watches; owner sees all clients.",
        scope="media:read",
        output_schema=output(watches=array(WATCH)),
    )
    async def list_watches(core, client, args):
        return await core.watches.list(client)

    @registry.declare(
        "alerts_watch_remove",
        "Remove a notification watch",
        "Use when cancelling a one-off notification. Requires your watch ID and media "
        "read access. Returns the removed ID; owner may remove any client's watch.",
        obj({"watch_id": string(64)}, ("watch_id",)),
        scope="media:read",
        tier="write",
        output_schema=output(watch_id=string(64)),
    )
    async def remove(core, client, args):
        return await core.watches.remove(client, args["watch_id"])
