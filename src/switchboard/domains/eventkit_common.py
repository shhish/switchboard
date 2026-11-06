"""Shared native-helper contracts; never return arbitrary backend fields."""

from ..errors import CapabilityError
from ..schema import string
from .common import pick

IDS = {"type": "array", "items": string(1000), "minItems": 1, "maxItems": 200}
DAYS = {"type": "integer", "minimum": 1, "maximum": 31}
TIME = {"timezone": string(100), "timezone_note": string(200)}


def metadata(data):
    return pick(data, ("timezone", "timezone_note"))


def resource(value, fields):
    if not isinstance(value, dict):
        raise CapabilityError("backend_invalid", "eventkit: invalid resource")
    result = pick(value, fields)
    # Stable identifiers must round-trip verbatim, rather than using pick's
    # display-text truncation. Invalid identifiers cannot become write targets.
    for key in fields:
        if key == "id" or key.endswith("_id"):
            identifier = value.get(key)
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 1000:
                raise CapabilityError("backend_invalid", "eventkit: invalid resource ID")
            result[key] = identifier
    return result


def rows(data, key, fields):
    values = data.get(key)
    if (
        not isinstance(values, list)
        or len(values) > 200
        or any(not isinstance(v, dict) for v in values)
    ):
        raise CapabilityError("backend_invalid", "eventkit: invalid resource list")
    return [resource(row, fields) for row in values]


async def native_read(core, entity, method, args):
    helper = core.backends.eventkit
    if not await helper.authorized(entity):
        return None
    try:
        return await helper.call(method, args)
    except CapabilityError as exc:
        if exc.code in {"backend_down", "eventkit_unauthorized"}:
            return None
        raise


def require_unfiltered(args, key):
    if key in args:
        raise CapabilityError(
            "backend_unavailable",
            "ID filters require the authorized EventKit helper; FrameServer cannot match IDs",
        )
