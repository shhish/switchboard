"""Things is the owner's task manager; every write checks its current destination."""

from datetime import date

from ..errors import CapabilityError
from ..schema import obj, string, validate
from .common import array, output, sanitize_external
from .eventkit_common import TIME, metadata

LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}
NULL_ID = {"type": ["string", "null"], "minLength": 1, "maxLength": 1000}
TODO = obj(
    {
        "id": string(1000),
        # Things allows untitled to-dos and projects.
        "name": {"type": "string", "maxLength": 2000},
        "notes": {"type": "string", "maxLength": 2000},
        "status": {"enum": ["open", "completed", "canceled"], "type": "string"},
        "due": {"type": ["string", "null"], "maxLength": 100},
        "deadline": {"type": ["string", "null"], "maxLength": 100},
        "activation_date": {"type": ["string", "null"], "maxLength": 100},
        "project_id": NULL_ID,
        "project_name": {"type": ["string", "null"], "maxLength": 2000},
        "area_id": NULL_ID,
        "area_name": {"type": ["string", "null"], "maxLength": 2000},
        "tags": {"type": "array", "items": string(1000), "maxItems": 200},
    },
    ("id", "name", "status", "project_id", "area_id"),
)
AREA = obj({"id": string(1000), "name": {"type": "string", "maxLength": 2000}}, ("id", "name"))


def resource(value, schema=TODO):
    if not isinstance(value, dict):
        raise CapabilityError("backend_invalid", "things: invalid resource")
    result = sanitize_external({key: value[key] for key in schema["properties"] if key in value})
    try:
        validate(result, schema)
    except CapabilityError:
        raise CapabilityError("backend_invalid", "things: invalid resource") from None
    return result


def rows(data, key, limit, schema=TODO):
    values = data.get(key)
    if not isinstance(values, list) or len(values) > limit:
        raise CapabilityError("backend_invalid", "things: invalid resource list")
    return [resource(row, schema) for row in values]


def validate_add(args, *, update=False):
    if "project_id" in args and "area_id" in args:
        raise CapabilityError("invalid_arguments", "Choose a project or an area, not both")
    for key in ("when", "deadline"):
        value = args.get(key)
        if value is None or (
            key == "when"
            and value in {"today", "tonight", "evening", "tomorrow", "anytime", "someday"}
        ):
            continue
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise CapabilityError("invalid_arguments", "Things dates must be YYYY-MM-DD") from None
    for key in ("title", "notes", "project_id", "area_id"):
        if update and key == "notes" and args.get(key) == "":
            continue
        if key in args and not args[key].strip():
            raise CapabilityError("invalid_arguments", "Things text must not be blank")
    for key in ("tags", "checklist"):
        if any(not item.strip() or "\n" in item or "\r" in item for item in args.get(key, [])):
            raise CapabilityError("invalid_arguments", "Things items must be nonblank single lines")
    if any("," in tag for tag in args.get("tags", [])):
        raise CapabilityError("invalid_arguments", "Things tag names cannot contain commas")


def register(registry):
    for name, list_name in (
        ("today", "Today"),
        ("inbox", "Inbox"),
        ("upcoming", "Upcoming"),
        ("anytime", "Anytime"),
    ):

        def declare_list(name, list_name):
            @registry.declare(
                "tasks_" + name,
                "Things " + list_name,
                f"Things {list_name} to-dos. Use when reading Things {list_name} to-dos. "
                "Requires authorized Things read access. Returns bounded to-dos; honor truncation.",
                obj({"limit": LIMIT}),
                scope="tasks:read",
                service="things",
                output_schema=output(todos=array(TODO), truncated={"type": "boolean"}, **TIME),
            )
            async def read(core, client, args):
                limit = args.get("limit", 50)
                data = await core.backends.things.call(
                    "things.lists", {"list": list_name, "limit": limit}
                )
                return {
                    "status": "ok",
                    "todos": rows(data, "todos", limit),
                    "limit": limit,
                    "truncated": bool(data.get("truncated", False)),
                    **metadata(data),
                }

        declare_list(name, list_name)

    @registry.declare(
        "tasks_search",
        "Search Things",
        "Things to-dos: search by name. Use when finding a Things to-do by name. Requires a "
        "nonblank query and authorized Things access. Returns bounded matches; resolve "
        "ambiguity before completing a discovered ID.",
        obj({"query": string(200), "limit": {**LIMIT, "maximum": 50}}, ("query",)),
        scope="tasks:read",
        service="things",
        output_schema=output(todos=array(TODO), truncated={"type": "boolean"}, **TIME),
    )
    async def search(core, client, args):
        if not args["query"].strip():
            raise CapabilityError("invalid_arguments", "Search query must not be blank")
        limit = args.get("limit", 20)
        data = await core.backends.things.call("things.search", {**args, "limit": limit})
        return {
            "status": "ok",
            "todos": rows(data, "todos", limit),
            "limit": limit,
            "truncated": bool(data.get("truncated", False)),
            **metadata(data),
        }

    @registry.declare(
        "tasks_list_projects",
        "List Things projects",
        "Things projects and parent areas. Use when choosing a Things project. Requires "
        "authorized Things read access. Returns project IDs and parent areas.",
        scope="tasks:read",
        service="things",
        output_schema=output(projects=array(TODO), truncated={"type": "boolean"}, **TIME),
    )
    async def projects(core, client, args):
        data = await core.backends.things.call("things.projects")
        return {
            "status": "ok",
            "projects": rows(data, "projects", 200),
            "truncated": bool(data.get("truncated", False)),
            **metadata(data),
        }

    @registry.declare(
        "tasks_list_areas",
        "List Things areas",
        "Things areas and stable IDs. Use when choosing a Things area. Requires authorized "
        "Things read access. Returns area names and stable IDs.",
        scope="tasks:read",
        service="things",
        output_schema=output(areas=array(AREA), truncated={"type": "boolean"}, **TIME),
    )
    async def areas(core, client, args):
        data = await core.backends.things.call("things.areas")
        return {
            "status": "ok",
            "areas": rows(data, "areas", 200, AREA),
            "truncated": bool(data.get("truncated", False)),
            **metadata(data),
        }

    @registry.declare(
        "tasks_add",
        "Add Things to-do",
        "Things to-do creation. Use when adding an owner task to Things. Requires confirmed "
        "dates and an allowed destination; prefer Inbox when unsure. Returns created to-do; use "
        "existing tags and no nonempty checklist.",
        obj(
            {
                "title": string(200),
                "notes": string(2000),
                "when": {
                    **string(10),
                    "description": "Start day or today/tomorrow/anytime/someday; no Evening.",
                },
                "deadline": string(10),
                "project_id": string(1000),
                "area_id": string(1000),
                "tags": {"type": "array", "items": string(200), "maxItems": 10},
                "checklist": {
                    "type": "array",
                    "items": string(200),
                    "maxItems": 20,
                    "description": "Reserved; nonempty returns unsupported before creation.",
                },
            },
            ("title",),
        ),
        scope="tasks:write",
        tier="write",
        idempotent=False,
        service="things",
        output_schema=output(todo=TODO, **TIME),
    )
    async def add(core, client, args):
        validate_add(args)
        rules = core.policy.task_rules()
        # Project parent areas are resolved and checked inside the helper before make.
        if "project_id" not in args:
            core.policy.require_task(rules, area_id=args.get("area_id"))
        elif args["project_id"] in rules["deny_projects"]:
            raise CapabilityError("resource_denied", "Things project denied by policy")
        data = await core.backends.things.call("things.add", {**args, **rules})
        return {"status": "ok", "todo": resource(data.get("todo")), **metadata(data)}

    @registry.declare(
        "tasks_complete",
        "Complete Things to-do",
        "Things to-do completion. Use when completing one Things to-do. Requires a discovered "
        "ID and allowed current project and area. Returns updated to-do; never mass-complete.",
        obj({"todo_id": string(1000)}, ("todo_id",)),
        scope="tasks:write",
        tier="write",
        service="things",
        output_schema=output(todo=TODO, **TIME),
    )
    async def complete(core, client, args):
        data = await core.backends.things.call(
            "things.complete", {**args, **core.policy.task_rules()}
        )
        return {"status": "ok", "todo": resource(data.get("todo")), **metadata(data)}

    @registry.declare(
        "tasks_update",
        "Update Things to-do",
        "Things open to-do edits. Use when editing one discovered open Things to-do after "
        "confirming the exact change with the human. Requires allowed current and new "
        "destinations. Returns updated to-do; projects and completed/canceled to-dos cannot be "
        "edited. Tags must already exist.",
        obj(
            {
                "todo_id": string(1000),
                "title": string(200),
                "notes": {"type": "string", "maxLength": 2000},
                "when": {
                    **string(10),
                    "pattern": r"^(today|tomorrow|anytime|someday|\d{4}-\d{2}-\d{2})$",
                },
                "deadline": {"type": ["string", "null"], "minLength": 10, "maxLength": 10},
                "project_id": string(1000),
                "area_id": string(1000),
                "tags": {"type": "array", "items": string(200), "maxItems": 10},
            },
            ("todo_id",),
        ),
        scope="tasks:write",
        tier="write",
        service="things",
        output_schema=output(todo=TODO, **TIME),
    )
    async def update(core, client, args):
        if len(args) == 1:
            raise CapabilityError("invalid_arguments", "At least one change is required")
        validate_add(args, update=True)
        rules = core.policy.task_rules()
        # The helper resolves both current and new project parent areas before mutation.
        if "area_id" in args:
            core.policy.require_task(rules, area_id=args["area_id"])
        elif args.get("project_id") in rules["deny_projects"]:
            raise CapabilityError("resource_denied", "Things project denied by policy")
        data = await core.backends.things.call("things.update", {**args, **rules})
        return {"status": "ok", "todo": resource(data.get("todo")), **metadata(data)}
