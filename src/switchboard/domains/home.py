"""HomeRelay reads and policy-gated controls with separate credentials."""

from urllib.parse import quote

from ..errors import CapabilityError
from ..memory_subjects import MemorySubject
from ..schema import obj, string
from .common import array, output, pick, require_one, selected

SAFE_TYPES = {
    "lightbulb",
    "switch",
    "outlet",
    "fan",
    "thermostat",
    "heaterCooler",
    "heater",
    "airConditioner",
    "windowCovering",
    "airPurifier",
    "humidifier",
    "dehumidifier",
}
SAFE_CHARACTERISTICS = {
    "on",
    "active",
    "brightness",
    "hue",
    "saturation",
    "colorTemperature",
    "rotationSpeed",
    "rotationDirection",
    "fanMode",
    "targetTemperature",
    "targetHeatingCooling",
    "targetHumidity",
    "targetPosition",
    "targetHorizontalTiltAngle",
    "targetVerticalTiltAngle",
    "holdPosition",
    "purifierMode",
    "targetHeaterCoolerState",
    "targetHumidifierDehumidifierState",
    "coolingThresholdTemperature",
    "heatingThresholdTemperature",
    "relativeHumidityDehumidifierThreshold",
    "relativeHumidityHumidifierThreshold",
}
SECURE_CHARACTERISTICS = {
    "lockTarget",
    "doorTarget",
    "securitySystemTargetState",
    "targetSecuritySystemState",
}


def path_id(identifier):
    if identifier in (".", "..") or "/" in identifier:
        raise CapabilityError("invalid_arguments", "Use a stable HomeRelay ID")
    return quote(identifier, safe="")


def classify(row):
    services = row.get("services")
    if (
        not isinstance(services, list)
        or not services
        or not isinstance(row.get("category"), str)
        or not all(
            isinstance(service, dict)
            and isinstance(service.get("type"), str)
            and isinstance(service.get("characteristics"), list)
            for service in services
        )
    ):
        raise CapabilityError("backend_invalid", "home: full service details are required")
    types = {row.get("category", ""), *(service["type"] for service in services)}
    sensitive = any(
        any(word in kind.casefold() for word in ("lock", "garage", "security", "alarm"))
        or kind in {"door", "window", "smokeSensor", "carbonMonoxideSensor"}
        for kind in types
    )
    if not sensitive and not (
        row["category"] in SAFE_TYPES
        or (row["category"] == "other" and types.intersection(SAFE_TYPES))
    ):
        raise CapabilityError(
            "unsupported_accessory", "Accessory type is not supported for control"
        )
    return sensitive


async def read_accessory(core, identifier):
    row = await core.backends.get("home", "/accessories/" + path_id(identifier))
    if not isinstance(row, dict) or row.get("id") != identifier:
        raise CapabilityError(
            "invalid_arguments", "Use a stable accessory ID from home_list/home_get"
        )
    return row


def check_accessory(core, row, secure):
    sensitive = classify(row)
    if sensitive and not secure:
        raise CapabilityError("secure_required", "Sensitive accessory; use home_set_secure")
    core.policy.require_home("accessories", row["id"], secure=secure)


def set_preflight(core, client, args):
    action = args["action"]
    if action == "set" and not {"characteristic", "value"} <= args.keys():
        raise CapabilityError("invalid_arguments", "Invalid arguments: schema constraint")
    if action != "set" and ("characteristic" in args or "value" in args):
        raise CapabilityError(
            "invalid_arguments", "Only action set requires characteristic and value"
        )


async def set_accessory(core, client, args, secure):
    set_preflight(core, client, args)
    action = args["action"]
    row = await read_accessory(core, args["accessory_id"])
    check_accessory(core, row, secure)
    path = "/accessories/" + path_id(row["id"])
    if action == "set":
        matches = [
            characteristic
            for service in row["services"]
            for characteristic in service["characteristics"]
            if isinstance(characteristic, dict)
            and characteristic.get("writable") is True
            and (service["type"] in SAFE_TYPES or secure)
            and characteristic.get("type")
            in (SAFE_CHARACTERISTICS | SECURE_CHARACTERISTICS if secure else SAFE_CHARACTERISTICS)
            and args["characteristic"] in (characteristic.get("id"), characteristic.get("type"))
        ]
        if len(matches) != 1:
            raise CapabilityError(
                "invalid_arguments", "Choose one supported writable characteristic type or ID"
            )
        # Bind low-level writes to a characteristic proven to belong to this accessory.
        path = "/characteristics/" + path_id(matches[0]["id"])
        payload = {"value": args["value"]}
        method = "PUT"
    else:
        if not secure and any(
            char.get("type") in {"on", "active"} and char.get("writable") is True
            for service in row["services"]
            if service["type"] not in SAFE_TYPES and service["type"] != "accessoryInformation"
            for char in service["characteristics"]
            if isinstance(char, dict)
        ):
            raise CapabilityError(
                "unsupported_accessory", "Power control spans unsupported services"
            )
        controls = {
            char.get("type")
            for service in row["services"]
            for char in service["characteristics"]
            if isinstance(char, dict)
            and char.get("writable") is True
            and (service["type"] in SAFE_TYPES or secure)
        }
        if not controls.intersection({"on", "active"} | ({"lockTarget"} if secure else set())):
            raise CapabilityError(
                "invalid_arguments", "Accessory has no supported power/toggle control"
            )
        if action != "toggle" and not controls.intersection({"on", "active"}):
            raise CapabilityError(
                "invalid_arguments", "Use set with lockTarget/doorTarget for secure control"
            )
        if action == "toggle":
            path += "/toggle"
        payload = {} if action == "toggle" else {"on": action == "on"}
        method = "POST"
    cap = "home_set_secure" if secure else "home_set"
    core.policy.require(client, core.registry.capabilities[cap])
    check_accessory(core, row, secure)
    data = await core.backends.request("home", path, payload=payload, method=method)
    if action == "set":
        if not isinstance(data, dict) or data.get("id") != matches[0]["id"]:
            raise CapabilityError("backend_invalid", "home: invalid characteristic receipt")
        # PUT returns a characteristic, not an accessory. Do not claim device readback.
        return {"status": "ok", "accessory_id": row["id"], "action": action}
    if not isinstance(data, dict) or data.get("id") != row["id"]:
        raise CapabilityError("backend_invalid", "home: invalid control receipt")
    return {
        "status": "ok",
        "accessory_id": row["id"],
        "action": action,
        "accessory": accessory(data),
    }


async def run_scene(core, client, args, secure):
    identifier = args["scene_id"]
    path_id(identifier)
    scenes = await core.backends.get("home", "/scenes")
    if not isinstance(scenes, list):
        raise CapabilityError("backend_invalid", "home: invalid scenes response")
    matches = [row for row in scenes if isinstance(row, dict) and row.get("id") == identifier]
    if len(matches) != 1:
        raise CapabilityError("invalid_arguments", "Use a stable scene ID from home_list")
    scene = matches[0]
    actions = scene.get("actions")
    targets = []
    if "actions" in scene:
        if not isinstance(actions, list):
            raise CapabilityError("backend_invalid", "home: invalid scene actions")
        for action in actions:
            target = (
                action.get("accessory_id", action.get("accessoryId"))
                if isinstance(action, dict)
                else None
            )
            if not isinstance(target, str) or not target:
                raise CapabilityError(
                    "backend_invalid", "home: scene action has no stable accessory ID"
                )
            row = await read_accessory(core, target)
            if classify(row) and not secure:
                raise CapabilityError(
                    "secure_required", "Sensitive scene; use home_run_scene_secure"
                )
            targets.append(row)
    cap = "home_run_scene_secure" if secure else "home_run_scene"
    core.policy.require(client, core.registry.capabilities[cap])
    core.policy.require_home("scenes", identifier, secure=secure, opaque="actions" not in scene)
    for row in targets:
        check_accessory(core, row, secure)
    data = await core.backends.request(
        "home", "/scenes/" + path_id(identifier) + "/run", payload={}
    )
    if not isinstance(data, dict) or data.get("id") != identifier:
        raise CapabilityError("backend_invalid", "home: invalid scene receipt")
    return {
        "status": "ok",
        "scene": pick(data, ("id", "name", "home", "kind")),
        "actions_exposed": actions is not None,
        "limitations": []
        if actions is not None
        else ["HomeRelay does not expose scene contents; sensitive targets cannot be detected"],
    }


def accessory(row):
    result = pick(row, ("id", "name", "home", "room", "category", "reachable"))
    # Fixed public state names; omit vendor-defined or unexpected payloads.
    result["state"] = pick(
        row.get("state", {}),
        (
            "on",
            "active",
            "brightness",
            "hue",
            "saturation",
            "colorTemperature",
            "currentTemperature",
            "targetTemperature",
            "currentHeatingCooling",
            "targetHeatingCooling",
            "humidity",
            "lockState",
            "lockTarget",
            "rotationSpeed",
            "position",
            "targetPosition",
            "motion",
            "occupancy",
            "contact",
            "batteryLevel",
            "lowBattery",
            "inUse",
            "doorState",
            "doorTarget",
        ),
    )
    return result


SCENE = selected(("id", "name", "home", "kind"))
ACCESSORY = obj(
    {
        **selected(("id", "name", "home", "room", "category", "reachable"))["properties"],
        "state": selected(
            (
                "on",
                "active",
                "brightness",
                "hue",
                "saturation",
                "colorTemperature",
                "currentTemperature",
                "targetTemperature",
                "currentHeatingCooling",
                "targetHeatingCooling",
                "humidity",
                "lockState",
                "lockTarget",
                "rotationSpeed",
                "position",
                "targetPosition",
                "motion",
                "occupancy",
                "contact",
                "batteryLevel",
                "lowBattery",
                "inUse",
                "doorState",
                "doorTarget",
            )
        ),
    }
)
HOME = obj(
    {
        **selected(("id", "name"))["properties"],
        "counts": obj({key: {"type": "integer"} for key in ("rooms", "accessories", "scenes")}),
        "rooms": array(selected(("id", "name", "home"))),
        "accessories": array(ACCESSORY),
        "scenes": array(SCENE),
    }
)


def register(registry):
    set_schema = obj(
        {
            "accessory_id": string(200),
            "action": {"type": "string", "enum": ["on", "off", "toggle", "set"]},
            "characteristic": string(200),
            "value": {"oneOf": [{"type": "boolean"}, {"type": "number"}, string(100)]},
        },
        ("accessory_id", "action"),
    )
    # No top-level oneOf: MCP connectors that flatten unions dropped the shared
    # accessory_id. set_accessory enforces "set needs characteristic and value".
    set_output = output(accessory_id=string(200), action=string(20), accessory=ACCESSORY)

    @registry.declare(
        "home_set",
        "Control accessory",
        "Apple Home lights and safe accessory control. Use when turning lights on or off or "
        "adjusting a safe accessory. Requires a discovered stable ID and control allowlisting. "
        "Returns HomeRelay action receipt; sensitive devices require home_set_secure.",
        set_schema,
        scope="home:control",
        tier="write",
        service="home",
        idempotent=False,
        preflight=set_preflight,
        output_schema=set_output,
        memory=MemorySubject(
            "home",
            arg_keys=("accessory_id", "name"),
            result_paths=("accessory.id", "accessory.name"),
        ),
    )
    async def set_home(core, client, args):
        return await set_accessory(core, client, args, False)

    @registry.declare(
        "home_set_secure",
        "Control sensitive accessory",
        "Apple Home sensitive accessory control with approval. Use when controlling a sensitive "
        "Home accessory. Requires secure allowlisting and exact human approval. Returns "
        "HomeRelay action receipt; first call requests an expiring grant.",
        set_schema,
        scope="home:secure",
        tier="approval",
        service="home",
        idempotent=False,
        preflight=set_preflight,
        output_schema=set_output,
        memory=MemorySubject(
            "home",
            arg_keys=("accessory_id", "name"),
            result_paths=("accessory.id", "accessory.name"),
        ),
    )
    async def set_secure(core, client, args):
        return await set_accessory(core, client, args, True)

    scene_schema = obj({"scene_id": string(200)}, ("scene_id",))
    scene_output = output(
        scene=SCENE,
        actions_exposed={"type": "boolean"},
        limitations={"type": "array", "items": {"type": "string"}},
    )

    @registry.declare(
        "home_run_scene",
        "Run home scene",
        "Apple Home scene control. Use when running a Home scene. Requires a discovered scene "
        "ID and scene allowlisting. Returns HomeRelay receipt; opaque scene contents require "
        "prior review.",
        scene_schema,
        scope="home:control",
        tier="write",
        service="home",
        idempotent=False,
        output_schema=scene_output,
        memory=MemorySubject(
            "home", arg_keys=("scene_id",), result_paths=("scene.id", "scene.name")
        ),
    )
    async def scene_home(core, client, args):
        return await run_scene(core, client, args, False)

    @registry.declare(
        "home_run_scene_secure",
        "Run sensitive home scene",
        "Apple Home sensitive scene control with approval. Use when running a sensitive Home "
        "scene. Requires secure scene allowlisting and exact human approval. Returns HomeRelay "
        "receipt; first call requests an expiring grant.",
        scene_schema,
        scope="home:secure",
        tier="approval",
        service="home",
        idempotent=False,
        output_schema=scene_output,
        memory=MemorySubject(
            "home", arg_keys=("scene_id",), result_paths=("scene.id", "scene.name")
        ),
    )
    async def scene_secure(core, client, args):
        return await run_scene(core, client, args, True)

    @registry.declare(
        "home_list",
        "List home",
        "Apple Home homes, rooms, accessories and scenes. Use when finding lights, rooms, "
        "accessories or scenes. Requires Home read access. Returns bounded HomeRelay names and "
        "stable IDs.",
        scope="home:read",
        service="home",
        output_schema=output(
            homes=array(HOME), total_homes={"type": "integer"}, truncated={"type": "boolean"}
        ),
        memory=MemorySubject(
            "home",
            result_paths=("homes[].name", "homes[].accessories[].id", "homes[].accessories[].name"),
        ),
    )
    async def list_home(core, client, args):
        homes = await core.backends.get("home", "/snapshot", params={"compact": "1"})
        result = []
        truncated = len(homes) > 5
        for row in homes[:5]:
            item = pick(row, ("id", "name"))
            item["counts"] = {
                key: len(row.get(key, [])) for key in ("rooms", "accessories", "scenes")
            }
            item["rooms"] = [
                pick(room, ("id", "name", "home")) for room in row.get("rooms", [])[:30]
            ]
            item["accessories"] = [
                pick(a, ("id", "name", "room", "category", "reachable"))
                for a in row.get("accessories", [])[:50]
            ]
            item["scenes"] = [
                pick(scene, ("id", "name", "home", "kind")) for scene in row.get("scenes", [])[:30]
            ]
            truncated |= any(
                item["counts"][key] > limit
                for key, limit in (("rooms", 30), ("accessories", 50), ("scenes", 30))
            )
            result.append(item)
        return {"status": "ok", "homes": result, "total_homes": len(homes), "truncated": truncated}

    @registry.declare(
        "home_get",
        "Get accessory",
        "Apple Home light and accessory state. Use when checking a light or other accessory "
        "state. Requires a discovered ID or Room/Name. Returns current compact HomeRelay state.",
        obj({"accessory_id": string(200), "name": string(200)}),
        scope="home:read",
        service="home",
        output_schema=output(accessory=ACCESSORY),
        memory=MemorySubject(
            "home",
            arg_keys=("accessory_id", "name"),
            result_paths=("accessory.id", "accessory.name"),
        ),
    )
    async def get_home(core, client, args):
        require_one(args, "accessory_id", "name")
        ref = args.get("accessory_id") or args["name"]
        if ref in (".", ".."):
            raise CapabilityError("invalid_arguments", "Use an accessory ID or name")
        data = await core.backends.get(
            "home", "/accessories/" + quote(ref, safe=""), params={"compact": "1"}
        )
        return {"status": "ok", "accessory": accessory(data)}

    def check_pause_state(client, ref):
        """ "Routines paused" is the owner's off switch for every routine (from the
        Home app); agents and routines can't flip or recreate it."""
        key = ref.removeprefix("state:").strip().casefold().replace(" ", "-")
        if key == "routines-paused" and client != "owner":
            raise CapabilityError("owner_required", "Only the owner changes Routines paused")

    state_row = obj(
        {
            "id": string(64),
            "name": string(64),
            "kind": string(20),
            "on": {"type": "boolean"},
            "resetAfter": {"type": ["integer", "null"]},
            "updatedAt": {"type": "string"},
            "updatedBy": {"type": ["string", "null"]},
        },
        ("id", "name", "kind", "on"),
    )

    def state(row):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise CapabilityError("backend_invalid", "home: invalid State")
        return {
            "id": row["id"],
            "name": str(row.get("name", ""))[:64],
            "kind": str(row.get("kind", "switch"))[:20],
            "on": row.get("on") is True,
            "resetAfter": row.get("resetAfter") if isinstance(row.get("resetAfter"), int) else None,
            "updatedAt": str(row.get("updatedAt", "")),
            "updatedBy": row.get("updatedBy") if isinstance(row.get("updatedBy"), str) else None,
        }

    @registry.declare(
        "home_states",
        "List States",
        "Apple Home virtual switches and sensors (HomeRelay States). "
        "Use when checking HomeRelay's virtual switches and sensors (States) such as Guest "
        "mode. Requires Home read access. Returns each State's id, name, kind and value.",
        scope="home:read",
        service="home",
        output_schema=output(states=array(state_row)),
        memory=MemorySubject("home", result_paths=("states[].name", "states[].id")),
    )
    async def list_states(core, client, args):
        rows = await core.backends.get("home", "/states")
        if not isinstance(rows, list):
            raise CapabilityError("backend_invalid", "home: invalid States list")
        return {"status": "ok", "states": [state(r) for r in rows[:149]]}

    @registry.declare(
        "home_state_set",
        "Set a State",
        "Turn a HomeRelay State (virtual switch or sensor) on or off. "
        "Use when turning a virtual switch or sensor (a State such as Guest mode) on or off. "
        "Requires the State's id or name. Returns the State afterwards.",
        obj({"state": string(64), "on": {"type": "boolean"}}, ("state", "on")),
        scope="home:control",
        tier="write",
        service="home",
        output_schema=output(state=state_row),
        memory=MemorySubject(
            "home", arg_keys=("state", "name"), result_paths=("state.id", "state.name")
        ),
    )
    async def set_state(core, client, args):
        check_pause_state(client, args["state"])
        data = await core.backends.request(
            "home", "/states/" + path_id(args["state"]), payload={"on": args["on"]}, method="PUT"
        )
        return {"status": "ok", "state": state(data)}

    @registry.declare(
        "home_state_create",
        "Create a State",
        "Create a HomeRelay State: a virtual switch or sensor for routines. "
        "Use when the owner wants a new virtual switch or sensor for routines and the Home app. "
        "Requires a name and kind (switch, contact, motion, occupancy, leak). Returns the State.",
        obj(
            {
                "name": string(64),
                "kind": {
                    "type": "string",
                    "enum": ["switch", "contact", "motion", "occupancy", "leak"],
                },
                "reset_after_s": {"type": "integer", "minimum": 1, "maximum": 86400},
            },
            ("name", "kind"),
        ),
        scope="home:control",
        tier="write",
        service="home",
        idempotent=False,
        output_schema=output(state=state_row),
        memory=MemorySubject(
            "home", arg_keys=("state", "name"), result_paths=("state.id", "state.name")
        ),
    )
    async def create_state(core, client, args):
        check_pause_state(client, args["name"])
        payload = {"name": args["name"], "kind": args["kind"]}
        if "reset_after_s" in args:
            payload["resetAfter"] = args["reset_after_s"]
        data = await core.backends.request("home", "/states", payload=payload, method="POST")
        return {"status": "ok", "state": state(data)}
