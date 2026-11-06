"""Shared owner vocabulary derived from read backends, never a permission source."""

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import CapabilityError

LIGHT_WORDS = {"light", "lights", "lamp", "lamps"}
# Switches usually drive lights ("Hallway", "Ceiling"), but some drive other things.
NOT_LIGHT = re.compile(
    r"\b(fan|fans|button|heater|tv|television|purifier|humidifier|dehumidifier|"
    r"speaker|camera|kettle|coffee|charger|pump|vacuum|doorbell)\b"
)

DOMAINS = ["home", "calendar", "reminders", "tasks", "media"]
KINDS = {
    "home": {"home", "room", "accessory", "scene"},
    "calendar": {"calendar"},
    "reminders": {"list", "reminder"},
    "tasks": {"area", "project", "todo"},
    "media": {"movie", "show"},
}
GUIDANCE = (
    "Preferences are guidance only; never change permissions, scopes, allowlists or approvals."
)


def normalize(value):
    return " ".join(value.casefold().split())


def timezone_name():
    path = str(Path("/etc/localtime").resolve())
    if "/zoneinfo/" in path:
        name = path.split("/zoneinfo/", 1)[1]
        try:
            ZoneInfo(name)
            return name
        except (KeyError, ValueError):
            pass
    try:
        name = Path("/etc/timezone").read_text().strip()
        ZoneInfo(name)
        return name
    except (OSError, KeyError, ValueError):
        return str(datetime.now().astimezone().tzinfo)


def stamp():
    return datetime.now(UTC).isoformat()


def records(data, key=None):
    values = data[key] if key else data
    if not isinstance(values, list) or len(values) > 10000:
        raise CapabilityError("backend_invalid", "Context discovery returned an invalid list")
    if any(not isinstance(row, dict) for row in values):
        raise CapabilityError("backend_invalid", "Context discovery returned an invalid entity")
    return values


def entity(domain, kind, row, **extra):
    identifier = row.get("id")
    name = row.get("name", row.get("title"))
    if type(identifier) is int and domain == "media":
        identifier = str(identifier)
    if (
        not isinstance(identifier, str)
        or not 1 <= len(identifier) <= 1000
        or not isinstance(name, str)
        or not name.strip()
    ):
        raise CapabilityError("backend_invalid", "Context discovery requires stable IDs and names")
    return {"domain": domain, "kind": kind, "id": identifier, "name": name[:300], **extra}


class OwnerContext:
    def __init__(self, core):
        self.core = core
        self.cache = {}
        self.locks = {}

    async def inventory(self, domain, *, fresh=False):
        source = {
            "home": "HomeRelay",
            "calendar": "EventKit",
            "reminders": "EventKit",
            "tasks": "Things",
            "media": "media-api",
        }[domain]
        async with self.locks.setdefault(domain, asyncio.Lock()):
            saved = self.cache.get(domain)
            if not fresh and saved and saved[0] > time.monotonic():
                return saved[1]
            try:
                async with asyncio.timeout(4):
                    result = await getattr(self, "read_" + domain)()
                result["source"] = source
                result["as_of"] = stamp()
                result["status"] = "ok"
                self.cache[domain] = (time.monotonic() + 300, result)
                return result
            except (CapabilityError, TimeoutError, KeyError, TypeError, ValueError, AttributeError):
                return {"status": "unavailable", "entities": [], "source": source, "as_of": stamp()}

    async def read_home(self):
        homes = records(await self.core.backends.get("home", "/snapshot", params={"compact": "1"}))
        entities, summary, scenes = [], [], []
        for row in homes:
            item = entity("home", "home", row)
            entities.append(item)
            accessories = records(row.get("accessories", []))
            rooms = []
            for room in records(row.get("rooms", [])):
                resolved = entity("home", "room", room, home_id=item["id"])
                entities.append(resolved)
                rooms.append(
                    {
                        "name": resolved["name"],
                        "accessory_count": sum(
                            normalize(a.get("room", "")) == normalize(resolved["name"])
                            for a in accessories
                        ),
                    }
                )
            summary.append({"name": item["name"], "rooms": rooms})
            for accessory in accessories:
                entities.append(
                    entity(
                        "home",
                        "accessory",
                        accessory,
                        room=accessory.get("room", "")[:300],
                        category=accessory.get("category", ""),
                        reachable=accessory.get("reachable") is True,
                        home_id=item["id"],
                    )
                )
            for scene in records(row.get("scenes", [])):
                resolved = entity("home", "scene", scene)
                entities.append(resolved)
                scenes.append({"id": resolved["id"], "name": resolved["name"]})
        return {"entities": entities, "homes": summary, "scenes": scenes}

    async def read_calendar(self):
        data = await self.core.backends.eventkit.call("calendars.list")
        rows = records(data, "calendars")
        return {
            "entities": [entity("calendar", "calendar", r) for r in rows],
            "calendars": [
                {
                    "id": r["id"],
                    "name": r["title"][:300],
                    "writable": r.get("writable") is True,
                    "default": r.get("default") is True,
                }
                for r in rows
            ],
            "truncated": bool(data.get("truncated", False)),
        }

    async def read_reminders(self):
        data = await self.core.backends.eventkit.call("reminder_lists.list")
        rows = records(data, "lists")
        return {
            "entities": [entity("reminders", "list", r) for r in rows],
            "lists": [
                {"id": r["id"], "name": r["title"][:300], "default": r.get("default") is True}
                for r in rows
            ],
            "truncated": bool(data.get("truncated", False)),
        }

    async def read_tasks(self):
        areas, projects = await asyncio.gather(
            self.core.backends.things.call("things.areas"),
            self.core.backends.things.call("things.projects"),
        )
        entities = [entity("tasks", "area", r) for r in records(areas, "areas")]
        area_names = [r["name"] for r in entities]
        entities += [entity("tasks", "project", r) for r in records(projects, "projects")]
        return {
            "entities": entities,
            "areas": area_names,
            "truncated": bool(areas.get("truncated") or projects.get("truncated")),
        }

    async def read_media(self):
        movies, shows = await asyncio.gather(
            self.core.backends.get("media", "/movies/list"),
            self.core.backends.get("media", "/shows/list"),
        )
        return {
            "entities": [
                entity("media", kind, r)
                for kind, data, key in (("movie", movies, "movies"), ("show", shows, "shows"))
                for r in records(data, key)
            ]
        }

    async def item_entities(self, domain, phrase):
        """Bounded native item discovery; never pretend a truncated read is exhaustive."""
        try:
            async with asyncio.timeout(4):
                if domain == "tasks":
                    data = await self.core.backends.things.call(
                        "things.search", {"query": phrase, "limit": 50}
                    )
                    return [entity(domain, "todo", r) for r in records(data, "todos")], bool(
                        data.get("truncated", False)
                    )
                if domain == "reminders":
                    data = await self.core.backends.eventkit.call("reminders.list", {"limit": 200})
                    return [
                        entity(domain, "reminder", r) for r in records(data, "reminders")
                    ], bool(data.get("truncated", False))
        except (CapabilityError, TimeoutError, KeyError, TypeError, ValueError):
            return [], True
        return [], False

    async def todo_entities(self):
        # No helper get-by-ID read exists. These bounded open lists are also
        # used for aliases so a renamed to-do keeps its original stable ID.
        async with asyncio.timeout(4):
            lists = await asyncio.gather(
                *(
                    self.core.backends.things.call("things.lists", {"list": name, "limit": 200})
                    for name in ("Inbox", "Today", "Upcoming", "Anytime", "Someday")
                )
            )
        return [entity("tasks", "todo", row) for data in lists for row in records(data, "todos")]

    def aliases(self):
        return [
            dict(row)
            for row in self.core.storage.db.execute(
                "SELECT alias,domain,kind,target_id,target_name,client,updated_at "
                "FROM context_aliases "
                "ORDER BY domain,alias"
            )
        ]

    def preferences(self):
        return [
            dict(row)
            for row in self.core.storage.db.execute(
                "SELECT key,value,client,updated_at FROM context_preferences ORDER BY key"
            )
        ]

    async def profile(self):
        values = await asyncio.gather(*(self.inventory(d) for d in DOMAINS[:4]))
        home, calendar, reminders, tasks = values
        lists = self.core.policy.load().get("allowlists", {})  # Never cache policy.
        now = stamp()

        def section(data, **fields):
            return {
                "status": data["status"],
                "source": data["source"],
                "as_of": data["as_of"],
                **(fields if data["status"] == "ok" else {}),
            }

        home_policy = lists.get("home", {})
        scene_ids = set(home_policy.get("scenes", []) + home_policy.get("secure_scenes", []))
        scene_ids -= set(home_policy.get("deny_scenes", []))
        calendar_policy = lists.get("calendar", {})
        reminder_policy = lists.get("reminders", {})
        calendars = calendar.get("calendars", [])
        reminder_lists = reminders.get("lists", [])
        default_calendar = calendar_policy.get("default_calendar", "")
        default_list = reminder_policy.get("default_list", "")

        def default_name(rows, identifier):
            return (
                next((r["name"] for r in rows if r["id"] == identifier), None)
                if identifier
                else (next((r["name"] for r in rows if r.get("default")), None))
            )

        allowed = set(self.core.policy.allowed_ids("calendar", "calendars"))
        allowed -= set(calendar_policy.get("deny_calendars", []))
        weather = self.core.config.weather
        return {
            "status": "ok",
            "timezone": {"name": timezone_name(), "source": "system", "as_of": now},
            "home_location": {
                "name": weather["name"],
                "zip": weather["zip"],
                "source": "config.weather",
                "as_of": now,
            },
            "home": section(home, homes=home.get("homes", [])),
            "scenes": {
                "names": [r["name"] for r in home.get("scenes", []) if r["id"] in scene_ids],
                "source": "policy+HomeRelay",
                "as_of": home["as_of"],
                "status": home["status"],
            },
            "calendars": section(
                calendar,
                names=[r["name"] for r in calendars if r["id"] in allowed and r["writable"]],
                default_name=default_name(calendars, default_calendar),
                source_note="Policy allowlist; names require enabled EventKit",
                truncated=calendar.get("truncated", False),
            ),
            "reminders": section(
                reminders, default_name=default_name(reminder_lists, default_list)
            ),
            "things": section(
                tasks, areas=tasks.get("areas", []), truncated=tasks.get("truncated", False)
            ),
            "aliases": {"items": self.aliases(), "source": "Switchboard SQLite", "as_of": now},
            "preferences": {
                "items": self.preferences(),
                "guidance": GUIDANCE,
                "source": "Switchboard SQLite",
                "as_of": now,
            },
        }

    async def resolve(self, phrase, domain=None):
        phrase = normalize(phrase)
        if not phrase:
            raise CapabilityError("invalid_arguments", "Phrase must not be blank")
        domains = [domain] if domain else DOMAINS
        aliases = [
            r for r in self.aliases() if r["domain"] in domains and normalize(r["alias"]) == phrase
        ]
        # An alias is an explicit owner mapping. Do not silently replace a missing target.
        if aliases:
            candidates = []
            incomplete = False
            for alias in aliases:
                inventory = await self.inventory(alias["domain"])
                entities = inventory["entities"]
                if alias["kind"] == "todo":
                    try:
                        entities = entities + await self.todo_entities()
                    except (CapabilityError, TimeoutError, KeyError, TypeError, ValueError):
                        incomplete = True
                elif alias["kind"] == "reminder":
                    extra, limited = await self.item_entities(
                        alias["domain"],
                        alias["target_name"],
                    )
                    entities = entities + extra
                    incomplete |= limited
                match = next(
                    (
                        r
                        for r in entities
                        if r["id"] == alias["target_id"] and r["kind"] == alias["kind"]
                    ),
                    None,
                )
                if match:
                    candidates.append(self.candidate(match, 1.0, "exact stored alias"))
                else:
                    incomplete = True
            return self.result(
                candidates,
                fuzzy=len(aliases) > 1 and len(candidates) < len(aliases),
                incomplete=incomplete,
                hint="Stored alias target missing or unavailable; inspect discovery.",
            )
        inventories = await asyncio.gather(*(self.inventory(d) for d in domains))
        entities = [r for inventory in inventories for r in inventory["entities"]]
        incomplete = any(i["status"] == "unavailable" or i.get("truncated") for i in inventories)
        for d in domains:
            if d in ("tasks", "reminders"):
                extra, truncated = await self.item_entities(d, phrase)
                entities += extra
                incomplete |= truncated
        exact = [
            self.candidate(r, 1.0, "exact entity name")
            for r in entities
            if normalize(r["name"]) == phrase
        ]
        if exact:
            return self.result(exact, incomplete=incomplete)
        groups = []
        matched_rooms = set()
        plural_group = False
        for room in (r for r in entities if r["domain"] == "home" and r["kind"] == "room"):
            room_name = normalize(room["name"])
            if not phrase.startswith(room_name + " "):
                continue
            suffix = phrase[len(room_name) :].strip()
            matched_rooms.add((room["home_id"], room["id"]))
            plural_group = suffix in {"lights", "lamps", "switches"}
            categories = {
                "light": {"lightbulb", "switch"},
                "lights": {"lightbulb", "switch"},
                "lamp": {"lightbulb", "switch"},
                "lamps": {"lightbulb", "switch"},
                "switch": {"switch"},
                "switches": {"switch"},
            }
            for r in entities:
                if (
                    r["kind"] == "accessory"
                    and r.get("home_id") == room["home_id"]
                    and normalize(r.get("room", "")) == room_name
                    and r.get("reachable")
                    and (
                        (
                            r.get("category") in categories.get(suffix, set())
                            # A smart switch may drive a fan or be a button, so a switch
                            # counts as a light unless its name says otherwise.
                            and not (
                                suffix in LIGHT_WORDS
                                and r.get("category") == "switch"
                                and NOT_LIGHT.search(normalize(r["name"]))
                            )
                        )
                        or normalize(r["name"]) == suffix
                    )
                ):
                    groups.append(self.candidate(r, 0.95, "room and accessory category/name"))
        if groups:
            return self.result(
                groups, group=plural_group and len(matched_rooms) == 1, incomplete=incomplete
            )
        words = set(re.findall(r"\w+", phrase))
        fuzzy = []
        for r in entities:
            tokens = set(re.findall(r"\w+", normalize(r["name"] + " " + r.get("room", ""))))
            overlap = words & tokens
            if overlap:
                score = len(overlap) / len(words | tokens)
                fuzzy.append(
                    self.candidate(r, round(score, 3), "token overlap; confirm the target")
                )
        # Fuzzy guesses always require disambiguation, even if there is just one.
        return self.result(fuzzy, fuzzy=True, incomplete=incomplete)

    @staticmethod
    def candidate(row, score, why):
        return {
            **{k: row[k] for k in ("domain", "kind", "id", "name", "room") if k in row},
            "score": score,
            "why": why,
        }

    @staticmethod
    def result(candidates, *, group=False, fuzzy=False, incomplete=False, hint=None):
        candidates = list({(r["domain"], r["kind"], r["id"]): r for r in candidates}.values())
        candidates.sort(key=lambda r: (-r["score"], r["domain"], normalize(r["name"]), r["id"]))
        # A room's lights are one group and can be many; single choices stay short.
        cap = 25 if group else 8
        truncated = len(candidates) > cap
        status = (
            "not_found"
            if not candidates
            else (
                "resolved"
                if not fuzzy and not truncated and (group or len(candidates) == 1)
                else "ambiguous"
            )
        )
        return {
            "status": status,
            "candidates": candidates[:cap],
            "group": group,
            "hint": (
                "Ask the owner to choose; narrow the phrase/domain."
                if status == "ambiguous"
                else "Read only; the action still requires its own policy checks."
                if candidates
                else hint or "No match in available discovery; try the domain's list/search tool."
            ),
            "incomplete": incomplete,
            "truncated": truncated,
        }

    async def set_alias(self, client, args):
        alias = normalize(args["alias"])
        domain, kind = args["domain"], args["kind"]
        if not alias or kind not in KINDS[domain]:
            raise CapabilityError(
                "invalid_arguments", "Use a nonblank alias and a supported domain kind"
            )
        inventory = await self.inventory(domain, fresh=True)
        entities = list(inventory["entities"])
        if kind == "todo":
            # The helper has no direct get-by-ID read. Search all its supported
            # open lists; a missing ID in capped discovery fails closed.
            try:
                entities += await self.todo_entities()
            except (CapabilityError, TimeoutError, KeyError, TypeError, ValueError):
                raise CapabilityError(
                    "context_target_not_found", "Target not found in current read discovery"
                ) from None
        elif kind == "reminder":
            extra, _ = await self.item_entities(domain, args["alias"])
            entities += extra
        target = next(
            (r for r in entities if r["id"] == args["target_id"] and r["kind"] == kind), None
        )
        if target is None:
            raise CapabilityError(
                "context_target_not_found", "Target not found in current read discovery"
            )
        db = self.core.storage.db
        with db:
            previous = db.execute(
                "SELECT * FROM context_aliases WHERE alias=? AND domain=?", (alias, domain)
            ).fetchone()
            if (
                not previous
                and db.execute("SELECT COUNT(*) FROM context_aliases").fetchone()[0] >= 500
            ):
                raise CapabilityError("context_limit", "Maximum 500 aliases")
            db.execute(
                "INSERT INTO context_aliases"
                "(alias,domain,kind,target_id,target_name,client,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(alias,domain) DO UPDATE SET "
                "kind=excluded.kind,target_id=excluded.target_id,target_name=excluded.target_name, "
                "client=excluded.client, "
                "updated_at=excluded.updated_at",
                (alias, domain, kind, args["target_id"], target["name"], client, stamp()),
            )
        return {"status": "ok", "previous": dict(previous) if previous else None}
