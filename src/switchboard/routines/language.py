"""The routine language: a closed YAML schema, parsed into plain dicts.

A routine is data, never code. Parsing checks every key against a closed
vocabulary; `validate` (in `validator.py`) then checks devices and tools
against the live system. The canonical form hashes stably, so the owner's
approval binds to exactly what will run.
"""

import hashlib
import json
import re

import yaml

MODES = ("single", "restart", "queued", "parallel")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SUN_EVENTS = ("sunrise", "sunset", "dawn", "dusk")
NAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
CLOCK = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
DURATION = re.compile(r"(\d+)\s*(s|m|h|d)")
MAX_STEPS = 60
TRIGGER_KINDS = ("device", "held", "reachability", "time", "every", "sun", "manual", "event")
# Switchboard events a routine can trigger on.
EVENTS = ("service.down", "service.recovered")
# Weather fields a condition can compare (from weather_now).
WEATHER_FIELDS = ("temp_f", "temp_c", "humidity_percent", "wind_mph")
MAX_DEPTH = 4

# Friendly device types: the service they select and the value they watch,
# in HomeRelay's names.
TYPES = {
    "motion": ("motionSensor", "motion"),
    "occupancy": ("occupancySensor", "occupancy"),
    "contact": ("contactSensor", "contact"),
    "garage": ("garageDoor", "doorState"),
    "lock": ("lock", "lockState"),
    "light": ("lightbulb", "on"),
    "switch": ("switch", "on"),
    "outlet": ("outlet", "on"),
    "fan": ("fan", "active"),
    "temperature": ("temperatureSensor", "currentTemperature"),
    "humidity": ("humiditySensor", "humidity"),
    "light_level": ("lightSensor", "lightLevel"),
    "leak": ("leakSensor", "leakDetected"),
    "smoke": ("smokeSensor", "smoke"),
    "air_quality": ("airQualitySensor", "airQuality"),
    "thermostat": ("thermostat", "currentTemperature"),
    "purifier": ("airPurifier", "active"),
    "button": ("button", "buttonEvent"),
}

# Words for values, per characteristic (HomeKit's numbers).
WORDS = {
    "contact": {"closed": 0, "open": 1},
    "doorState": {"open": 0, "closed": 1, "opening": 2, "closing": 3, "stopped": 4},
    "doorTarget": {"open": 0, "closed": 1},
    "lockState": {"unlocked": 0, "locked": 1, "jammed": 2, "unknown": 3},
    "lockTarget": {"unlocked": 0, "locked": 1},
    "motion": {"detected": True, "clear": False},
    "occupancy": {"detected": 1, "clear": 0},
    "leakDetected": {"detected": 1, "clear": 0},
    "smoke": {"detected": 1, "clear": 0},
    "on": {"on": True, "off": False},
    "active": {"on": 1, "off": 0},
    "inUse": {"in_use": 1, "idle": 0},
    "buttonEvent": {"single": 0, "double": 1, "long": 2},
    "airQuality": {"unknown": 0, "excellent": 1, "good": 2, "fair": 3, "inferior": 4, "poor": 5},
}
SECURE_TARGETS = {
    "open": ("doorTarget", 0),
    "closed": ("doorTarget", 1),
    "close": ("doorTarget", 1),
    "unlocked": ("lockTarget", 0),
    "unlock": ("lockTarget", 0),
    "locked": ("lockTarget", 1),
    "lock": ("lockTarget", 1),
}


class Loader(yaml.SafeLoader):
    """YAML 1.2 booleans: only true and false. Under YAML 1.1 rules `on:` and
    `off` would become booleans, and `on: true` would lose its key."""


Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


class RoutineError(Exception):
    """Every problem found, each with a path into the definition."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__("; ".join(problems[:5]))


def parse_duration(value, path, problems, *, minimum=0, maximum=7 * 86400):
    """`90s`, `15m`, `2h`, `1h30m` or a number of seconds."""
    if isinstance(value, bool):
        problems.append(f"{path}: expected a duration like 15m")
        return 0
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str) and (parts := DURATION.findall(value.replace(" ", ""))):
        if "".join(n + u for n, u in parts) != value.replace(" ", ""):
            problems.append(f"{path}: expected a duration like 15m")
            return 0
        seconds = sum(int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u] for n, u in parts)
    else:
        problems.append(f"{path}: expected a duration like 15m")
        return 0
    if not minimum <= seconds <= maximum:
        problems.append(f"{path}: must be between {minimum}s and {int(maximum)}s")
    return seconds


def format_duration(seconds):
    seconds = int(seconds)
    if seconds % 3600 == 0 and seconds:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0 and seconds:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def closed(value, path, problems, allowed, required=()):
    if not isinstance(value, dict):
        problems.append(f"{path}: expected a mapping")
        return {}
    for key in value:
        if key not in allowed:
            problems.append(f"{path}: unknown key '{key}'")
    for key in required:
        if key not in value:
            problems.append(f"{path}: '{key}' is required")
    return value


def text(value, path, problems, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        problems.append(f"{path}: expected text up to {limit} characters")
        return ""
    return value.strip()


# MARK: - Devices


def selector(value, path, problems):
    """A device reference: `"Room/Name"`, an id, or `{room, type, name, id, characteristic}`.
    When it names several devices, conditions need all of them (`match: any` for any)."""
    if isinstance(value, str):
        return {"ref": text(value, path, problems)}
    spec = closed(value, path, problems, ("id", "name", "room", "type", "characteristic", "match"))
    out = {}
    if "match" in spec:
        if spec["match"] not in ("any", "all"):
            problems.append(f"{path}.match: any or all")
        out["match"] = spec["match"]
    for key in ("id", "name", "room", "characteristic"):
        if key in spec:
            out[key] = text(spec[key], f"{path}.{key}", problems)
    if "type" in spec:
        if spec["type"] not in TYPES:
            problems.append(f"{path}.type: one of {', '.join(sorted(TYPES))}")
        else:
            out["type"] = spec["type"]
    if not set(out) & {"id", "name", "room", "type"}:
        problems.append(f"{path}: name a device by id, name, room or type")
    return out


def comparison(spec, path, problems, allowed=("is", "above", "below")):
    """`is` (a value or a word), `above`, `below`: at least one."""
    out = {}
    for key in allowed:
        if key not in spec:
            continue
        value = spec[key]
        if key == "is":
            if not isinstance(value, bool | int | float | str) or (
                isinstance(value, str) and not value
            ):
                problems.append(f"{path}.is: expected a value")
            out["is"] = value
        else:
            if isinstance(value, bool) or not isinstance(value, int | float):
                problems.append(f"{path}.{key}: expected a number")
            out[key] = value
    if not out:
        problems.append(f"{path}: give is, above or below")
    return out


# MARK: - Triggers


def trigger(value, path, problems):
    if value == "manual":
        return {"kind": "manual"}
    spec = closed(
        value,
        path,
        problems,
        ("id", *TRIGGER_KINDS),
    )
    kinds = [k for k in TRIGGER_KINDS if k in spec]
    if len(kinds) != 1:
        problems.append(f"{path}: exactly one of {', '.join(TRIGGER_KINDS)}")
        return {"kind": "invalid"}
    kind = kinds[0]
    body = spec[kind]
    out = {"kind": kind}
    if "id" in spec:
        out["id"] = text(spec["id"], f"{path}.id", problems, 40)
    p = f"{path}.{kind}"
    if kind == "manual":
        return out
    if kind == "event":
        if isinstance(body, str):
            b = {"name": body}
        else:
            b = closed(body, p, problems, ("name", "service"), ("name",))
        if b.get("name") not in EVENTS:
            problems.append(f"{p}.name: one of {', '.join(EVENTS)}")
        out["name"] = b.get("name")
        if "service" in b:
            out["service"] = text(b["service"], f"{p}.service", problems, 40)
        return out
    if kind == "device":
        b = closed(body, p, problems, ("device", "to", "from", "above", "below"), ("device",))
        out["device"] = selector(b.get("device"), f"{p}.device", problems)
        for key in ("to", "from"):
            if key in b:
                out[key] = b[key]
        for key in ("above", "below"):
            if key in b:
                if isinstance(b[key], bool) or not isinstance(b[key], int | float):
                    problems.append(f"{p}.{key}: expected a number")
                out[key] = b[key]
    elif kind == "held":
        b = closed(body, p, problems, ("device", "is", "above", "below", "for"), ("device", "for"))
        out["device"] = selector(b.get("device"), f"{p}.device", problems)
        out["test"] = comparison(b, p, problems)
        out["for_s"] = parse_duration(b.get("for"), f"{p}.for", problems, minimum=1)
    elif kind == "reachability":
        b = closed(body, p, problems, ("device", "reachable", "for"), ("device", "reachable"))
        out["device"] = selector(b.get("device"), f"{p}.device", problems)
        if not isinstance(b.get("reachable"), bool):
            problems.append(f"{p}.reachable: true or false")
        out["reachable"] = bool(b.get("reachable"))
        out["for_s"] = parse_duration(b.get("for", 0), f"{p}.for", problems)
    elif kind == "time":
        b = (
            {"at": body}
            if isinstance(body, str)
            else closed(body, p, problems, ("at", "days"), ("at",))
        )
        at = b.get("at")
        if not isinstance(at, str) or not CLOCK.fullmatch(at):
            problems.append(f"{p}.at: a 24-hour time like 07:00")
        out["at"] = at
        out["days"] = days(b.get("days"), f"{p}.days", problems)
    elif kind == "every":
        out["every_s"] = parse_duration(body, p, problems, minimum=60, maximum=86400)
    elif kind == "sun":
        b = (
            {"event": body}
            if isinstance(body, str)
            else closed(body, p, problems, ("event", "offset", "days"), ("event",))
        )
        if b.get("event") not in SUN_EVENTS:
            problems.append(f"{p}.event: one of {', '.join(SUN_EVENTS)}")
        out["event"] = b.get("event")
        offset = b.get("offset", 0)
        sign = -1 if isinstance(offset, str) and offset.startswith("-") else 1
        raw = offset[1:] if isinstance(offset, str) and offset[:1] in "+-" else offset
        out["offset_s"] = sign * parse_duration(raw, f"{p}.offset", problems, maximum=4 * 3600)
        out["days"] = days(b.get("days"), f"{p}.days", problems)
    return out


def days(value, path, problems):
    if value is None:
        return list(range(7))
    if value == "weekdays":
        return [0, 1, 2, 3, 4]
    if value == "weekends":
        return [5, 6]
    if not isinstance(value, list) or not value or not all(v in DAYS for v in value):
        problems.append(f"{path}: weekdays, weekends, or a list of {', '.join(DAYS)}")
        return list(range(7))
    return sorted({DAYS.index(v) for v in value})


# MARK: - Conditions


def condition(value, path, problems, depth=0):
    if depth > MAX_DEPTH:
        problems.append(f"{path}: conditions nest too deep")
        return {"kind": "invalid"}
    if isinstance(value, list):
        return {
            "kind": "all",
            "items": [
                condition(v, f"{path}[{i}]", problems, depth + 1) for i, v in enumerate(value)
            ],
        }
    keys = (
        "all",
        "any",
        "not",
        "device",
        "reachable",
        "time",
        "sun",
        "history",
        "trigger",
        "weather",
        "calendar",
    )
    spec = closed(
        value,
        path,
        problems,
        keys + ("is", "above", "below", "for", "window", "mean", "min", "max", "on", "changes"),
    )
    kinds = [k for k in keys if k in spec]
    if len(kinds) != 1:
        problems.append(f"{path}: exactly one of {', '.join(keys)}")
        return {"kind": "invalid"}
    kind = kinds[0]
    body = spec[kind]
    p = f"{path}.{kind}"
    extra = set(spec) - {kind}
    if kind in ("all", "any"):
        if extra or not isinstance(body, list) or not body:
            problems.append(f"{p}: a non-empty list of conditions")
            return {"kind": kind, "items": []}
        return {
            "kind": kind,
            "items": [condition(v, f"{p}[{i}]", problems, depth + 1) for i, v in enumerate(body)],
        }
    if kind == "not":
        if extra:
            problems.append(f"{p}: takes one condition")
        return {"kind": "not", "item": condition(body, p, problems, depth + 1)}
    if kind == "device":
        if extra - {"is", "above", "below", "for"}:
            problems.append(f"{path}: device takes is, above, below, for")
        out = {
            "kind": "device",
            "device": selector(body, p, problems),
            "test": comparison(spec, path, problems),
        }
        if "for" in spec:
            out["for_s"] = parse_duration(spec["for"], f"{path}.for", problems, minimum=1)
        return out
    if kind == "reachable":
        if extra - {"is"}:
            problems.append(f"{path}: reachable takes is")
        return {
            "kind": "reachable",
            "device": selector(body, p, problems),
            "is": spec.get("is", True) is not False,
        }
    if kind == "time":
        if extra:
            problems.append(f"{path}: time takes after, before, days")
        b = closed(body, p, problems, ("after", "before", "days"))
        out = {"kind": "time", "days": days(b.get("days"), f"{p}.days", problems)}
        for key in ("after", "before"):
            if key in b:
                if not isinstance(b[key], str) or not CLOCK.fullmatch(b[key]):
                    problems.append(f"{p}.{key}: a 24-hour time like 22:00")
                out[key] = b[key]
        return out
    if kind == "sun":
        if extra:
            problems.append(f"{path}: sun takes day, night or a mapping")
        if body in ("day", "night"):
            return {"kind": "sun", "is": body}
        b = closed(body, p, problems, ("elevation_above", "elevation_below"))
        out = {"kind": "sun"}
        for key in ("elevation_above", "elevation_below"):
            if key in b:
                if isinstance(b[key], bool) or not isinstance(b[key], int | float):
                    problems.append(f"{p}.{key}: degrees")
                out[key] = b[key]
        if len(out) == 1:
            problems.append(f"{p}: day, night, elevation_above or elevation_below")
        return out
    if kind == "history":
        if extra - {"window", "mean", "min", "max", "on", "changes"}:
            problems.append(f"{path}: history takes window and one of mean, min, max, on, changes")
        out = {
            "kind": "history",
            "device": selector(body, p, problems),
            "window_s": parse_duration(
                spec.get("window", "1h"), f"{path}.window", problems, minimum=60, maximum=14 * 86400
            ),
        }
        metrics = [m for m in ("mean", "min", "max", "on", "changes") if m in spec]
        if len(metrics) != 1:
            problems.append(f"{path}: one of mean, min, max, on (seconds on), changes")
            return out
        metric = metrics[0]
        test = spec[metric]
        if not isinstance(test, dict):
            problems.append(f"{path}.{metric}: a mapping with above or below")
            return out
        out["metric"] = metric
        out["test"] = comparison(
            closed(test, f"{path}.{metric}", problems, ("above", "below")),
            f"{path}.{metric}",
            problems,
            ("above", "below"),
        )
        if metric == "on":
            # Durations read naturally here: on: {above: 10m}.
            for key in ("above", "below"):
                if isinstance(test.get(key), str):
                    out["test"][key] = parse_duration(test[key], f"{path}.on.{key}", problems)
        return out
    if kind == "weather":
        return weather_condition(body, p, problems, extra)
    if kind == "calendar":
        if extra or body not in ("busy", "free"):
            problems.append(f"{path}: calendar: busy or free")
        return {"kind": "calendar", "is": body}
    # trigger
    if extra:
        problems.append(f"{path}: trigger takes a trigger id")
    return {"kind": "trigger", "id": text(body, p, problems, 40)}


def weather_condition(body, p, problems, extra):
    """`weather: {temp_f: {below: 32}, conditions: rain, alert: true}`."""
    if extra:
        problems.append(f"{p}: weather takes one mapping")
    b = closed(body, p, problems, (*WEATHER_FIELDS, "conditions", "alert"))
    out = {"kind": "weather"}
    for field in WEATHER_FIELDS:
        if field not in b:
            continue
        if not isinstance(b[field], dict):
            problems.append(f"{p}.{field}: a mapping with above or below")
            continue
        test = closed(b[field], f"{p}.{field}", problems, ("above", "below"))
        out[field] = comparison(test, f"{p}.{field}", problems, ("above", "below"))
    if "conditions" in b:
        out["conditions"] = text(b["conditions"], f"{p}.conditions", problems, 40)
    if "alert" in b:
        if not isinstance(b["alert"], bool):
            problems.append(f"{p}.alert: true or false")
        out["alert"] = b["alert"] is True
    if len(out) == 1:
        problems.append(f"{p}: give {', '.join(WEATHER_FIELDS)}, conditions or alert")
    return out


# MARK: - Steps


STEP_KINDS = (
    "home.set",
    "home.set_secure",
    "home.scene",
    "state.set",
    "call",
    "notify",
    "wait",
    "wait_until",
    "if",
    "choose",
    "repeat",
    "stop",
)


def steps(value, path, problems, counter, depth=0):
    if not isinstance(value, list) or not value:
        problems.append(f"{path}: a non-empty list of steps")
        return []
    if depth > MAX_DEPTH:
        problems.append(f"{path}: steps nest too deep")
        return []
    return [step(v, f"{path}[{i}]", problems, counter, depth) for i, v in enumerate(value)]


def step(value, path, problems, counter, depth):
    counter[0] += 1
    if counter[0] > MAX_STEPS:
        if counter[0] == MAX_STEPS + 1:
            problems.append(f"{path}: at most {MAX_STEPS} steps")
        return {"kind": "invalid"}
    if value == "stop":
        return {"kind": "stop"}
    if not isinstance(value, dict):
        problems.append(f"{path}: expected a step mapping")
        return {"kind": "invalid"}
    kinds = [k for k in STEP_KINDS if k in value]
    if len(kinds) != 1:
        problems.append(f"{path}: exactly one of {', '.join(STEP_KINDS)}")
        return {"kind": "invalid"}
    kind = kinds[0]
    body = value[kind]
    p = f"{path}.{kind}"
    others = set(value) - {kind}
    if kind == "if":
        if others - {"then", "else"} or "then" not in value:
            problems.append(f"{path}: if needs then (and optionally else)")
        out = {
            "kind": "if",
            "condition": condition(body, p, problems),
            "then": steps(value.get("then"), f"{path}.then", problems, counter, depth + 1),
        }
        if "else" in value:
            out["else"] = steps(value["else"], f"{path}.else", problems, counter, depth + 1)
        return out
    if kind == "repeat":
        if others - {"steps"}:
            problems.append(f"{path}: repeat takes count and steps")
        b = body if isinstance(body, dict) else {"count": body}
        count = b.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            problems.append(f"{p}.count: 1 to 20")
        return {
            "kind": "repeat",
            "count": count if isinstance(count, int) else 1,
            "steps": steps(value.get("steps"), f"{path}.steps", problems, counter, depth + 1),
        }
    if others:
        problems.append(f"{path}: unexpected keys {', '.join(sorted(others))}")
    if kind == "stop":
        return {"kind": "stop"}
    if kind in ("home.set", "home.set_secure"):
        if not isinstance(body, dict):
            problems.append(f"{p}: expected a mapping with device and values")
            body = {}
        b = body
        if "device" not in b:
            problems.append(f"{p}: device is required")
        values = {k: v for k, v in b.items() if k != "device"}
        if not values:
            problems.append(f"{p}: give at least one value, e.g. on: true")
        out = {
            "kind": kind,
            "device": selector(b.get("device"), f"{p}.device", problems),
            "values": {},
        }
        for key, v in values.items():
            if kind == "home.set_secure":
                if key != "target" or v not in SECURE_TARGETS:
                    problems.append(f"{p}: target: open, closed, locked or unlocked")
                out["values"]["target"] = v
            elif key == "on" and not isinstance(v, bool):
                problems.append(f"{p}.on: true or false")
            elif not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{0,40}", key) or not isinstance(
                v, bool | int | float | str
            ):
                problems.append(f"{p}.{key}: a HomeRelay value name and a value")
            else:
                out["values"][key] = v
        return out
    if kind == "state.set":
        b = closed(body, p, problems, ("state", "on"), ("state", "on"))
        if not isinstance(b.get("on"), bool):
            problems.append(f"{p}.on: true or false")
        return {
            "kind": kind,
            "state": text(b.get("state"), f"{p}.state", problems, 64),
            "on": b.get("on") is True,
        }
    if kind == "home.scene":
        return {
            "kind": kind,
            "scene": text(
                body if isinstance(body, str) else (body or {}).get("scene"), p, problems
            ),
        }
    if kind == "call":
        b = closed(body, p, problems, ("tool", "args"), ("tool",))
        args = b.get("args", {})
        if not isinstance(args, dict):
            problems.append(f"{p}.args: a mapping")
            args = {}
        return {
            "kind": "call",
            "tool": text(b.get("tool"), f"{p}.tool", problems, 64),
            "args": args,
        }
    if kind == "notify":
        b = (
            {"message": body}
            if isinstance(body, str)
            else closed(body, p, problems, ("title", "message"), ("message",))
        )
        return {
            "kind": "notify",
            "title": text(b.get("title", "Routine"), f"{p}.title", problems, 80),
            "message": text(b.get("message"), f"{p}.message", problems, 500),
        }
    if kind == "wait":
        return {
            "kind": "wait",
            "seconds": parse_duration(body, p, problems, minimum=1, maximum=86400),
        }
    # wait_until
    b = closed(
        body, p, problems, ("condition", "for", "timeout", "on_timeout"), ("condition", "timeout")
    )
    out = {
        "kind": "wait_until",
        "condition": condition(b.get("condition"), f"{p}.condition", problems),
        "for_s": parse_duration(b.get("for", 0), f"{p}.for", problems),
        "timeout_s": parse_duration(
            b.get("timeout"), f"{p}.timeout", problems, minimum=1, maximum=86400
        ),
        "on_timeout": b.get("on_timeout", "continue"),
    }
    if out["on_timeout"] not in ("continue", "stop"):
        problems.append(f"{p}.on_timeout: continue or stop")
    return out


# MARK: - Routines


def parse(source):
    """YAML text to a canonical definition. Raises RoutineError listing every problem."""
    problems = []
    try:
        data = yaml.load(source, Loader=Loader) if isinstance(source, str) else source
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        raise RoutineError([f"Not valid YAML{where}"]) from None
    if isinstance(source, str) and len(source) > 20_000:
        problems.append("routine: at most 20,000 characters")
    spec = closed(
        data,
        "routine",
        problems,
        (
            "routine",
            "description",
            "mode",
            "max_runs",
            "cooldown",
            "triggers",
            "conditions",
            "steps",
            "until",
        ),
        ("routine", "triggers", "steps"),
    )
    name = spec.get("routine")
    if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > 64:
        problems.append("routine: a dotted lowercase name like home.porch_light")
    out = {"routine": name, "mode": spec.get("mode", "single")}
    if "description" in spec:
        out["description"] = text(spec["description"], "description", problems, 300)
    if out["mode"] not in MODES:
        problems.append(f"mode: one of {', '.join(MODES)}")
    max_runs = spec.get(
        "max_runs", 5 if out["mode"] == "parallel" else 10 if out["mode"] == "queued" else 1
    )
    if isinstance(max_runs, bool) or not isinstance(max_runs, int) or not 1 <= max_runs <= 10:
        problems.append("max_runs: 1 to 10")
    out["max_runs"] = max_runs
    out["cooldown_s"] = parse_duration(spec.get("cooldown", 0), "cooldown", problems, maximum=86400)
    triggers = spec.get("triggers")
    if not isinstance(triggers, list) or not triggers or len(triggers) > 10:
        problems.append("triggers: a list of 1 to 10 triggers")
        triggers = []
    out["triggers"] = [trigger(t, f"triggers[{i}]", problems) for i, t in enumerate(triggers)]
    ids = [t["id"] for t in out["triggers"] if "id" in t]
    if len(ids) != len(set(ids)):
        problems.append("triggers: ids must be unique")
    if "conditions" in spec:
        out["conditions"] = condition(spec["conditions"], "conditions", problems)
    if "until" in spec:
        out["until"] = condition(spec["until"], "until", problems)
    out["steps"] = steps(spec.get("steps"), "steps", problems, [0])
    if problems:
        raise RoutineError(problems)
    return out


def canonical(definition):
    return json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def definition_hash(definition, scopes):
    """What the owner approves: the definition and the scopes it may use."""
    body = canonical({"definition": definition, "scopes": sorted(scopes)})
    return hashlib.sha256(body.encode()).hexdigest()


def walk_steps(items, prefix=""):
    """Every step with a stable path (`2`, `3.then.0`), depth first."""
    for i, s in enumerate(items):
        path = f"{prefix}{i}"
        yield path, s
        for branch in ("then", "else"):
            if branch in s:
                yield from walk_steps(s[branch], f"{path}.{branch}.")
        if "steps" in s:
            # The engine journals each pass as `<path>#<pass>.<i>`; plans show the first.
            yield from walk_steps(s["steps"], f"{path}#0.")


def walk_conditions(cond):
    if not cond:
        return
    yield cond
    for item in cond.get("items", []):
        yield from walk_conditions(item)
    if "item" in cond:
        yield from walk_conditions(cond["item"])
