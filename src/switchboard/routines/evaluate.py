"""The live picture of the home, and three-valued condition evaluation.

`HomeIndex` mirrors HomeRelay: accessories, their characteristics, current
values and when each last changed. HomeRelay's event stream keeps it fresh.

Conditions evaluate to True, False or None (unknown). Unknown means a device
is unreachable or its value hasn't been seen; it never passes `all`, never
counts as `not` true, and the run log says which device was unknown.
"""

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .language import TYPES, WORDS


def local_zone():
    """The Mac's own time zone, as a ZoneInfo (DST-aware)."""
    if name := os.environ.get("TZ"):
        try:
            return ZoneInfo(name)
        except (KeyError, ValueError):
            pass
    try:
        target = os.path.realpath("/etc/localtime")
        return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (IndexError, KeyError, ValueError, OSError):
        return ZoneInfo("UTC")


class Unresolved(Exception):
    pass


class HomeIndex:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.accessories = {}
        self.chars = {}
        self.loaded_ms = None

    @staticmethod
    def state_rows(states):
        """HomeRelay States as accessories in a room called States, so a routine
        can say `device: States/Guest mode`."""
        rows = []
        for s in states if isinstance(states, list) else []:
            if not isinstance(s, dict) or not isinstance(s.get("id"), str):
                continue
            sid = "state:" + s["id"]
            rows.append(
                {
                    "id": sid,
                    "name": s.get("name") or s["id"],
                    "room": "States",
                    "reachable": True,
                    "services": [
                        {
                            "type": "state",
                            "primary": True,
                            "characteristics": [
                                {"id": sid + ":on", "type": "on", "value": s.get("on") is True},
                            ],
                        }
                    ],
                }
            )
        return rows

    def load(self, rows):
        """Replace everything from a full `/accessories` listing."""
        now = int(self.clock() * 1000)
        previous = self.chars
        self.accessories, self.chars = {}, {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            aid = row["id"]
            self.accessories[aid] = {
                "id": aid,
                "name": row.get("name") or "",
                "room": row.get("room") or "",
                "reachable": row.get("reachable") is not False,
                "services": [],
            }
            for service in row.get("services") or []:
                if not isinstance(service, dict) or service.get("type") == "accessoryInformation":
                    continue
                types = []
                for c in service.get("characteristics") or []:
                    if not isinstance(c, dict) or not isinstance(c.get("id"), str):
                        continue
                    old = previous.get(c["id"])
                    value = c.get("value")
                    self.chars[c["id"]] = {
                        "id": c["id"],
                        "accessory_id": aid,
                        "service_type": service.get("type"),
                        "primary": service.get("primary") is True,
                        "type": c.get("type"),
                        "value": value,
                        "writable": c.get("writable") is True,
                        # Keep when it last changed across reloads, when the value is unchanged.
                        "since_ms": old["since_ms"] if old and old["value"] == value else now,
                        "seen": value is not None,
                    }
                    types.append(c.get("type"))
                self.accessories[aid]["services"].append(
                    {
                        "type": service.get("type"),
                        "primary": service.get("primary") is True,
                        "types": types,
                    }
                )
        self.loaded_ms = now

    def apply(self, event, ts_ms=None):
        """Updates from one HomeRelay event. Returns what changed:
        `("value", cid, old, new)`, `("repeat", cid, value)` or `("reachable", aid, old, new)`."""
        now = ts_ms or int(self.clock() * 1000)
        kind = event.get("type")
        if kind == "characteristic":
            c = self.chars.get(event.get("characteristicId"))
            if c is None or event.get("origin") == "sample":
                return []
            new = event.get("value")
            old = c["value"]
            c["seen"] = True
            if old == new and c["type"] != "buttonEvent":
                return [("repeat", c["id"], new)]
            c["value"] = new
            c["since_ms"] = now
            return [("value", c["id"], old, new)]
        if kind == "reachability":
            a = self.accessories.get(event.get("accessoryId"))
            if a is None or not isinstance(event.get("reachable"), bool):
                return []
            old, a["reachable"] = a["reachable"], event["reachable"]
            return [("reachable", a["id"], old, a["reachable"])] if old != a["reachable"] else []
        return []

    # MARK: - Resolving devices

    def accessories_for(self, sel):
        """Accessory ids a selector names. Raises Unresolved when none (or a
        single-device reference is ambiguous)."""
        rows = list(self.accessories.values())
        if "ref" in sel:
            ref = sel["ref"].strip()
            exact = [a for a in rows if a["id"].casefold() == ref.casefold()]
            if exact:
                return [exact[0]["id"]]
            if "/" in ref:
                room, name = (part.strip().casefold() for part in ref.split("/", 1))
                found = [
                    a for a in rows if a["room"].casefold() == room and a["name"].casefold() == name
                ]
            else:
                found = [a for a in rows if a["name"].casefold() == ref.casefold()]
            if len(found) > 1:
                raise Unresolved(
                    f"'{ref}' matches {len(found)} accessories; use Room/Name or the id"
                )
            if not found:
                raise Unresolved(f"No accessory '{ref}'")
            return [found[0]["id"]]
        found = rows
        if "id" in sel:
            found = [a for a in found if a["id"] == sel["id"]]
        if "name" in sel:
            found = [a for a in found if a["name"].casefold() == sel["name"].casefold()]
        if "room" in sel:
            found = [a for a in found if a["room"].casefold() == sel["room"].casefold()]
        if "type" in sel:
            service = TYPES[sel["type"]][0]
            found = [a for a in found if any(s["type"] == service for s in a["services"])]
        if not found:
            raise Unresolved(f"No accessory matches {describe(sel)}")
        return [a["id"] for a in found]

    def characteristic_for(self, aid, sel):
        """The characteristic a selector watches on one accessory."""
        a = self.accessories[aid]
        mine = [c for c in self.chars.values() if c["accessory_id"] == aid]
        if sel.get("characteristic"):
            want = sel["characteristic"]
            hits = [c for c in mine if want in (c["id"], c["type"])]
        elif sel.get("type"):
            service, ctype = TYPES[sel["type"]]
            hits = [c for c in mine if c["service_type"] == service and c["type"] == ctype]
        else:
            # The primary service's main value, else the first value we know how to watch.
            known = {ctype: rank for rank, (_, ctype) in enumerate(TYPES.values())}
            hits = sorted(
                (c for c in mine if c["type"] in known),
                key=lambda c: (not c["primary"], known[c["type"]]),
            )[:1]
        if not hits:
            raise Unresolved(
                f"{a['room']}/{a['name']} has no "
                f"{sel.get('characteristic') or sel.get('type') or 'watchable value'}"
            )
        return hits[0]["id"]

    def resolve(self, sel):
        """`[(accessory_id, characteristic_id), …]` for a selector."""
        return [(aid, self.characteristic_for(aid, sel)) for aid in self.accessories_for(sel)]

    def label(self, aid):
        a = self.accessories.get(aid)
        return f"{a['room']}/{a['name']}" if a else aid


def describe(sel):
    if "ref" in sel:
        return sel["ref"]
    return ", ".join(f"{k} {v}" for k, v in sel.items())


def word_value(ctype, value):
    """`open` → 1 for a contact sensor; numbers and booleans pass through."""
    if isinstance(value, str):
        words = WORDS.get(ctype, {})
        key = value.casefold()
        if key in words:
            return words[key]
        if key in ("true", "on", "yes"):
            return True
        if key in ("false", "off", "no"):
            return False
    return value


def same(a, b):
    """Equality across HomeKit's mix of booleans, integers and floats."""
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a.casefold() == b.casefold()
    if isinstance(a, bool | int | float) and isinstance(b, bool | int | float):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def check_value(ctype, value, test):
    """True/False for a comparison, None when the value is unknown."""
    if value is None:
        return None
    result = True
    if "is" in test:
        result = result and same(value, word_value(ctype, test["is"]))
    for key, op in (("above", lambda v, t: v > t), ("below", lambda v, t: v < t)):
        if key in test:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return False
            result = result and op(value, test[key])
    return result


def crossed(ctype, old, new, spec):
    """Whether a change matches a device trigger's to/from/above/below."""
    if "to" in spec and not same(new, word_value(ctype, spec["to"])):
        return False
    if "from" in spec and (old is None or not same(old, word_value(ctype, spec["from"]))):
        return False
    for key, op in (("above", lambda v, t: v > t), ("below", lambda v, t: v < t)):
        if key in spec:
            # A crossing: now past the threshold, and wasn't before.
            if not isinstance(new, int | float) or isinstance(new, bool) or not op(new, spec[key]):
                return False
            if isinstance(old, int | float) and not isinstance(old, bool) and op(old, spec[key]):
                return False
    return True


def tri_all(values):
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    return True


def tri_any(values):
    if any(v is True for v in values):
        return True
    if any(v is None for v in values):
        return None
    return False


class Evaluator:
    """Evaluates conditions against the index, the clock, the sun and history."""

    def __init__(self, index, *, location, zone, history=None, tools=None, clock=time.time):
        self.index = index
        # async (author, tool, args) -> result: reads like weather_now as the routine's author.
        self.tools = tools
        self.location = location  # (lat, lon) or None
        self.zone = zone
        self.history = history  # async (characteristic_id, window_s) -> summary dict
        self.clock = clock

    async def evaluate(self, cond, run=None, notes=None):
        """True, False or None. `notes` collects why, for run logs and plans."""
        notes = notes if notes is not None else []
        if cond is None:
            return True
        kind = cond["kind"]
        if kind == "all":
            return tri_all([await self.evaluate(c, run, notes) for c in cond["items"]])
        if kind == "any":
            return tri_any([await self.evaluate(c, run, notes) for c in cond["items"]])
        if kind == "not":
            inner = await self.evaluate(cond["item"], run, notes)
            return None if inner is None else not inner
        if kind == "device":
            return self.device(cond, notes)
        if kind == "reachable":
            try:
                aids = self.index.accessories_for(cond["device"])
            except Exception as exc:
                notes.append(f"unknown: {exc}")
                return None
            ok = all(self.index.accessories[a]["reachable"] for a in aids)
            return ok if cond["is"] else not ok
        if kind == "time":
            return self.time(cond)
        if kind == "sun":
            return self.sun(cond, notes)
        if kind == "history":
            return await self.history_condition(cond, notes)
        if kind == "trigger":
            return bool(run and run.get("trigger", {}).get("id") == cond["id"])
        if kind in ("weather", "calendar"):
            author = (run or {}).get("author")
            if self.tools is None or author is None:
                notes.append(f"unknown: {kind} needs a routine to run as")
                return None
            return await (self.weather if kind == "weather" else self.calendar)(cond, author, notes)
        notes.append(f"unknown condition {kind}")
        return None

    def device(self, cond, notes):
        try:
            pairs = self.index.resolve(cond["device"])
        except Exception as exc:
            notes.append(f"unknown: {exc}")
            return None
        now = int(self.clock() * 1000)
        results = []
        for aid, cid in pairs:
            a = self.index.accessories[aid]
            c = self.index.chars[cid]
            if not a["reachable"] or not c["seen"]:
                notes.append(
                    f"unknown: {self.index.label(aid)} is "
                    f"{'unreachable' if not a['reachable'] else 'not yet seen'}"
                )
                results.append(None)
                continue
            result = check_value(c["type"], c["value"], cond["test"])
            if result and "for_s" in cond and now - c["since_ms"] < cond["for_s"] * 1000:
                result = False
            results.append(result)
        match = cond["device"].get("match", "all")
        return tri_any(results) if match == "any" else tri_all(results)

    def local_now(self):
        return datetime.fromtimestamp(self.clock(), self.zone)

    def time(self, cond):
        now = self.local_now()
        if now.weekday() not in cond["days"]:
            return False
        minutes = now.hour * 60 + now.minute

        def m(clock):
            h, mm = clock.split(":")
            return int(h) * 60 + int(mm)

        after = m(cond["after"]) if "after" in cond else None
        before = m(cond["before"]) if "before" in cond else None
        if after is not None and before is not None:
            # Inclusive start, exclusive end; a window past midnight wraps.
            return (
                after <= minutes < before
                if after <= before
                else minutes >= after or minutes < before
            )
        if after is not None:
            return minutes >= after
        if before is not None:
            return minutes < before
        return True

    def elevation(self):
        from .sun import elevation

        lat, lon = self.location
        return elevation(datetime.fromtimestamp(self.clock(), self.zone), lat, lon)

    def sun(self, cond, notes):
        if not self.location:
            notes.append("unknown: no location for sun conditions")
            return None
        degrees = self.elevation()
        if "is" in cond:
            day = degrees > -0.833
            return day if cond["is"] == "day" else not day
        result = True
        if "elevation_above" in cond:
            result = result and degrees > cond["elevation_above"]
        if "elevation_below" in cond:
            result = result and degrees < cond["elevation_below"]
        return result

    async def weather(self, cond, author, notes):
        now = await self.tools(author, "weather_now", {})
        if now.get("status") != "ok" or now.get("stale"):
            notes.append("unknown: no current weather")
            return None
        results = []
        for field in ("temp_f", "temp_c", "humidity_percent", "wind_mph"):
            if field in cond:
                results.append(check_value(None, now.get(field), cond[field]))
        if "conditions" in cond:
            text = now.get("conditions")
            results.append(
                None
                if not isinstance(text, str)
                else cond["conditions"].casefold() in text.casefold()
            )
        if "alert" in cond:
            alerts = await self.tools(author, "weather_alerts", {})
            if alerts.get("status") != "ok":
                results.append(None)
            else:
                results.append(bool(alerts.get("alerts")) == cond["alert"])
        if any(r is None for r in results):
            notes.append("unknown: a weather value is missing")
        return tri_all(results)

    async def calendar(self, cond, author, notes):
        """Busy: inside a timed (not all-day) event on the owner's calendars now."""
        data = await self.tools(author, "calendar_upcoming", {"days": 1})
        if data.get("status") != "ok":
            notes.append("unknown: calendar unavailable")
            return None
        now = datetime.fromtimestamp(self.clock(), self.zone)
        busy = False
        for event in data.get("events") or []:
            if event.get("allDay") or event.get("all_day"):
                continue
            try:
                start = datetime.fromisoformat(str(event["start"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(event["end"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if start <= now < end:
                busy = True
                break
        return busy if cond["is"] == "busy" else not busy

    async def history_condition(self, cond, notes):
        if self.history is None:
            notes.append("unknown: history unavailable")
            return None
        try:
            pairs = self.index.resolve(cond["device"])
        except Exception as exc:
            notes.append(f"unknown: {exc}")
            return None
        field = {"mean": "mean", "min": "min", "max": "max", "on": "on", "changes": "transitions"}[
            cond["metric"]
        ]
        results = []
        for aid, cid in pairs:
            try:
                summary = await self.history(cid, int(cond["window_s"]))
            except Exception:
                notes.append(f"unknown: no history for {self.index.label(aid)}")
                results.append(None)
                continue
            value = (summary.get("summary") or {}).get(field)
            if value is None and field in ("on", "transitions"):
                value = 0
            results.append(check_value(None, value, cond["test"]))
        match = cond["device"].get("match", "all")
        return tri_any(results) if match == "any" else tri_all(results)
