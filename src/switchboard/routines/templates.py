"""`{{ trigger.accessory }}`-style substitution: dotted paths only, no code."""

import re

FIELD = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\s*\}\}")


def has_template(value):
    if isinstance(value, str):
        return bool(FIELD.search(value))
    if isinstance(value, dict):
        return any(has_template(v) for v in value.values())
    if isinstance(value, list):
        return any(has_template(v) for v in value)
    return False


def lookup(context, path):
    value = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def render(value, context):
    """Strings get values substituted; a string that is exactly one field keeps
    the value's type (a number stays a number)."""
    if isinstance(value, str):
        whole = FIELD.fullmatch(value.strip())
        if whole:
            found = lookup(context, whole.group(1))
            return "" if found is None else found
        return FIELD.sub(
            lambda m: "" if (v := lookup(context, m.group(1))) is None else str(v), value
        )
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    return value
