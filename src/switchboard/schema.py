"""Small JSON Schema subset used by this catalog; no implicit type coercion.

Supported: type, properties, required, additionalProperties, enum, oneOf, not,
items, pattern, length/item bounds, numeric bounds. Unsupported keywords fail at registration.
"""

import math
import re

from .errors import CapabilityError

KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "oneOf",
    "not",
    "items",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "description",
    "title",
}


def obj(properties=None, required=()):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


def string(max_length=256):
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def check_schema(schema):
    if set(schema) - KEYWORDS:
        raise ValueError("Unsupported schema keyword")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise ValueError("Schema pattern must be a string")
        try:
            re.compile(schema["pattern"])
        except re.error:
            raise ValueError("Invalid schema pattern") from None
    for child in schema.get("properties", {}).values():
        check_schema(child)
    if "items" in schema:
        check_schema(schema["items"])
    for child in schema.get("oneOf", []):
        check_schema(child)
    if "not" in schema:
        check_schema(schema["not"])


def validate(value, schema, path="arguments"):
    def fail():
        raise CapabilityError("invalid_arguments", f"Invalid {path}: schema constraint")

    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if "type" in schema:
        expected = schema["type"]
        choices = expected if isinstance(expected, list) else [expected]
        if not any(
            isinstance(value, types[t])
            and not (t in ("integer", "number") and isinstance(value, bool))
            for t in choices
        ):
            fail()
    if isinstance(value, float) and not math.isfinite(value):
        fail()
    if "enum" in schema and value not in schema["enum"]:
        fail()
    if "not" in schema:
        try:
            validate(value, schema["not"], path)
        except CapabilityError:
            pass
        else:
            fail()
    if "oneOf" in schema:
        matches = 0
        for choice in schema["oneOf"]:
            try:
                validate(value, choice, path)
                matches += 1
            except CapabilityError:
                pass
        if matches != 1:
            fail()
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - props.keys():
            fail()
        if any(key not in value for key in schema.get("required", [])):
            fail()
        for key, item in value.items():
            if key in props:
                validate(item, props[key], f"{path}.{key}")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            fail()
        for item in value:
            validate(item, schema.get("items", {}), path + "[]")
    if isinstance(value, str):
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            fail()
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            fail()
    if type(value) in (int, float):
        if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            fail()
