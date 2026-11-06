"""Owner profile, read-only entity resolution and bounded shared vocabulary writes."""

import re

from ..errors import CapabilityError
from ..owner_context import DOMAINS, normalize, stamp
from ..schema import obj, string
from .common import array, output

DOMAIN = {"type": "string", "enum": DOMAINS}
NULL_TEXT = {"type": ["string", "null"]}
TEXT = {"type": "string"}
ALIAS = obj(
    {
        key: TEXT
        for key in ("alias", "domain", "kind", "target_id", "target_name", "client", "updated_at")
    }
)
PREFERENCE = obj({key: TEXT for key in ("key", "value", "client", "updated_at")})
ALIAS_PREVIOUS = {**ALIAS, "type": ["object", "null"]}
PREFERENCE_PREVIOUS = {**PREFERENCE, "type": ["object", "null"]}
SECTION = {"status": TEXT, "source": TEXT, "as_of": TEXT}
ROOM = obj({"name": TEXT, "accessory_count": {"type": "integer"}})
HOME = obj({"name": TEXT, "rooms": array(ROOM)})
PROFILE = {
    "timezone": obj({"name": TEXT, "source": TEXT, "as_of": TEXT}),
    "home_location": obj({"name": NULL_TEXT, "zip": NULL_TEXT, "source": TEXT, "as_of": TEXT}),
    "home": obj({**SECTION, "homes": array(HOME)}),
    "scenes": obj({**SECTION, "names": array(TEXT)}),
    "calendars": obj(
        {
            **SECTION,
            "names": array(TEXT),
            "default_name": NULL_TEXT,
            "source_note": TEXT,
            "truncated": {"type": "boolean"},
        }
    ),
    "reminders": obj({**SECTION, "default_name": NULL_TEXT}),
    "things": obj({**SECTION, "areas": array(TEXT), "truncated": {"type": "boolean"}}),
    "aliases": obj({"items": array(ALIAS), "source": TEXT, "as_of": TEXT}),
    "preferences": obj(
        {"items": array(PREFERENCE), "guidance": TEXT, "source": TEXT, "as_of": TEXT}
    ),
}
CANDIDATE = obj(
    {
        **{key: TEXT for key in ("domain", "kind", "id", "name", "room", "why")},
        "score": {"type": "number"},
    }
)


def preference_key(key):
    if not re.fullmatch(r"[a-z0-9_.]{1,64}", key):
        raise CapabilityError("invalid_arguments", "Preference key must match [a-z0-9_.]{1,64}")


def register(registry):
    @registry.declare(
        "context_get",
        "Owner context",
        "Use when checking owner defaults, aliases or preferences. Requires context read "
        "access. Returns shared profile and cached backend discovery; unavailable sections "
        "do not imply empty inventories.",
        scope="context:read",
        service="system+home+eventkit+things+sqlite",
        output_schema=output(**PROFILE),
    )
    async def get(core, client, args):
        return await core.context.profile()

    @registry.declare(
        "context_resolve",
        "Resolve owner phrase",
        "Use when resolving owner names for lights, tasks, calendars or media. Requires a "
        "phrase and optional domain. Returns ranked stable-ID candidates; ask on ambiguity "
        "and never act on a fuzzy guess.",
        obj({"phrase": string(120), "domain": DOMAIN}, ("phrase",)),
        scope="context:read",
        service="home+eventkit+things+media",
        timeout_s=30,
        output_schema=output(
            candidates={**array(CANDIDATE), "maxItems": 25},
            hint={"type": "string"},
            group={"type": "boolean"},
            incomplete={"type": "boolean"},
            truncated={"type": "boolean"},
        ),
    )
    async def resolve(core, client, args):
        return await core.context.resolve(**args)

    @registry.declare(
        "context_set_alias",
        "Save owner alias",
        "Use when saving an owner name for a known entity. Requires a target verified in "
        "fresh discovery. Returns the previous alias if replaced; aliases never grant "
        "permission.",
        obj(
            {"alias": string(80), "domain": DOMAIN, "kind": string(30), "target_id": string(1000)},
            ("alias", "domain", "kind", "target_id"),
        ),
        scope="context:write",
        tier="write",
        service="sqlite+read-backends",
        timeout_s=30,
        output_schema=output(previous=ALIAS_PREVIOUS),
    )
    async def set_alias(core, client, args):
        return await core.context.set_alias(client, args)

    @registry.declare(
        "context_remove_alias",
        "Remove owner alias",
        "Use when forgetting a shared entity alias. Requires an exact alias and domain. "
        "Returns the previous alias; target resources stay unchanged.",
        obj({"alias": string(80), "domain": DOMAIN}, ("alias", "domain")),
        scope="context:write",
        tier="write",
        service="sqlite",
        output_schema=output(previous=ALIAS_PREVIOUS),
    )
    async def remove_alias(core, client, args):
        alias = normalize(args["alias"])
        if not alias:
            raise CapabilityError("invalid_arguments", "Alias must not be blank")
        db = core.storage.db
        with db:
            previous = db.execute(
                "SELECT * FROM context_aliases WHERE alias=? AND domain=?", (alias, args["domain"])
            ).fetchone()
            db.execute(
                "DELETE FROM context_aliases WHERE alias=? AND domain=?", (alias, args["domain"])
            )
        return {"status": "ok", "previous": dict(previous) if previous else None}

    @registry.declare(
        "context_set_preference",
        "Save owner preference",
        "Use when saving shared owner guidance. Requires a valid preference key and "
        "nonsecret text. Returns the previous preference; preferences never change policy or "
        "approvals.",
        obj({"key": string(64), "value": string(500)}, ("key", "value")),
        scope="context:write",
        tier="write",
        service="sqlite",
        output_schema=output(previous=PREFERENCE_PREVIOUS),
    )
    async def set_preference(core, client, args):
        preference_key(args["key"])
        db = core.storage.db
        with db:
            previous = db.execute(
                "SELECT * FROM context_preferences WHERE key=?", (args["key"],)
            ).fetchone()
            if (
                not previous
                and db.execute("SELECT COUNT(*) FROM context_preferences").fetchone()[0] >= 200
            ):
                raise CapabilityError("context_limit", "Maximum 200 preferences")
            db.execute(
                "INSERT INTO context_preferences(key,value,client,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,client=excluded.client, "
                "updated_at=excluded.updated_at",
                (args["key"], args["value"], client, stamp()),
            )
        return {"status": "ok", "previous": dict(previous) if previous else None}

    @registry.declare(
        "context_remove_preference",
        "Remove owner preference",
        "Use when forgetting shared owner guidance. Requires an exact preference key. "
        "Returns the previous preference.",
        obj({"key": string(64)}, ("key",)),
        scope="context:write",
        tier="write",
        service="sqlite",
        output_schema=output(previous=PREFERENCE_PREVIOUS),
    )
    async def remove_preference(core, client, args):
        preference_key(args["key"])
        db = core.storage.db
        with db:
            previous = db.execute(
                "SELECT * FROM context_preferences WHERE key=?", (args["key"],)
            ).fetchone()
            db.execute("DELETE FROM context_preferences WHERE key=?", (args["key"],))
        return {"status": "ok", "previous": dict(previous) if previous else None}
