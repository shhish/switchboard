"""Checks a parsed routine against the live system, before the owner sees it.

Every device and scene must resolve, every `call` must name a real tool with
arguments its schema accepts, and the scopes the routine needs (which the
approval binds) must all be held by the author.
"""

from ..errors import CapabilityError
from ..schema import validate
from .evaluate import Unresolved
from .language import SECURE_TARGETS, walk_conditions, walk_steps
from .templates import has_template

# Tools a routine may never call: approvals and routine management stay with
# people, and nothing reaches past Switchboard's own policy.
FORBIDDEN = {
    "approvals_confirm",
    "routines_approve",
    "routines_propose",
    "routines_pause",
    "routines_run",
    "routines_disable",
}

# A State the owner flips in the Home app to pause every routine.
PAUSE_STATE = "routines-paused"

STEP_SCOPES = {
    "home.set": "home:control",
    "home.scene": "home:control",
    "state.set": "home:control",
    "home.set_secure": "home:secure",
}


def selectors(definition):
    """Every device selector, with a path for messages."""
    for i, t in enumerate(definition["triggers"]):
        if "device" in t:
            yield f"triggers[{i}]", t["device"]
    for name in ("conditions", "until"):
        for cond in walk_conditions(definition.get(name)):
            if "device" in cond:
                yield name, cond["device"]
    for path, step in walk_steps(definition["steps"]):
        for cond in walk_conditions(step.get("condition")):
            if "device" in cond:
                yield f"steps.{path}", cond["device"]
        if "device" in step:
            yield f"steps.{path}", step["device"]


def all_conditions(definition):
    for name in ("conditions", "until"):
        yield from walk_conditions(definition.get(name))
    for _, step in walk_steps(definition["steps"]):
        yield from walk_conditions(step.get("condition"))


def check(core, definition, index, scenes, author):
    """Returns (scopes, problems, warnings)."""
    problems, warnings = [], []
    scopes = set()
    uses_home = False
    for where, sel in selectors(definition):
        uses_home = True
        try:
            index.resolve(sel) if not where.startswith(
                "steps"
            ) or "device" not in sel else index.accessories_for(sel)
        except Unresolved as exc:
            problems.append(f"{where}: {exc}")
    if uses_home:
        scopes.add("home:read")
    for cond in all_conditions(definition):
        if cond["kind"] == "weather":
            scopes.add("weather:read")
        elif cond["kind"] == "calendar":
            scopes.add("calendar:read")
    for path, step in walk_steps(definition["steps"]):
        kind = step["kind"]
        where = f"steps.{path}"
        if kind in STEP_SCOPES:
            scopes.add(STEP_SCOPES[kind])
        if kind == "home.set":
            check_home_set(index, step, where, problems)
        elif kind == "home.set_secure":
            check_secure(index, step, where, problems)
        elif kind == "state.set":
            try:
                if state_id(index, step["state"]) == PAUSE_STATE:
                    problems.append(f"{where}: routines can't change the Routines paused State")
            except Unresolved as exc:
                problems.append(f"{where}: {exc}")
        elif kind == "home.scene":
            if not any(
                step["scene"].casefold()
                in (s.get("id", "").casefold(), s.get("name", "").casefold())
                for s in scenes
            ):
                problems.append(f"{where}: no scene '{step['scene']}'")
        elif kind == "call":
            cap = core.registry.capabilities.get(step["tool"])
            if cap is None:
                problems.append(f"{where}: no tool '{step['tool']}'")
                continue
            if cap.name in FORBIDDEN or cap.name.startswith("routines_"):
                problems.append(f"{where}: routines can't call {cap.name}")
                continue
            scopes.add(cap.scope)
            if has_template(step["args"]):
                warnings.append(
                    f"{where}: arguments use values from the run; checked again when it runs"
                )
            else:
                try:
                    validate(step["args"], cap.input_schema)
                except CapabilityError:
                    problems.append(f"{where}: arguments don't match {cap.name}'s schema")
            if cap.tier == "approval":
                warnings.append(f"{where}: {cap.name} asks you to approve each time it runs")
    for scope in sorted(scopes) if author else ():
        try:
            core.policy.require_scope(author, scope)
        except CapabilityError:
            problems.append(f"needs scope {scope}, which {author} doesn't have")
    return scopes, problems, warnings


def state_id(index, ref):
    """A State's id from its name or id (States are accessories in room States)."""
    key = ref.removeprefix("state:").casefold()
    for a in index.accessories.values():
        if a["room"] == "States" and key in (
            a["id"].removeprefix("state:").casefold(),
            a["name"].casefold(),
        ):
            return a["id"].removeprefix("state:")
    raise Unresolved(f"No State '{ref}'")


def check_home_set(index, step, where, problems):
    try:
        aids = index.accessories_for(step["device"])
    except Unresolved as exc:
        problems.append(f"{where}: {exc}")
        return
    from ..domains.home import SECURE_CHARACTERISTICS  # here: avoids an import cycle

    for aid in aids:
        writable = {
            c["type"] for c in index.chars.values() if c["accessory_id"] == aid and c["writable"]
        }
        for key in step["values"]:
            if key in SECURE_CHARACTERISTICS:
                problems.append(f"{where}: '{key}' needs home.set_secure")
            elif key == "on" and writable & {"on", "active"}:
                continue
            elif key not in writable:
                problems.append(f"{where}: {index.label(aid)} has no writable '{key}'")


def check_secure(index, step, where, problems):
    try:
        aids = index.accessories_for(step["device"])
    except Unresolved as exc:
        problems.append(f"{where}: {exc}")
        return
    want, _ = SECURE_TARGETS[step["values"]["target"]]
    if len(aids) != 1:
        problems.append(f"{where}: home.set_secure acts on one device; {len(aids)} match")
    for aid in aids:
        types = {c["type"] for c in index.chars.values() if c["accessory_id"] == aid}
        if want not in types:
            problems.append(
                f"{where}: {index.label(aid)} can't be set to {step['values']['target']}"
            )
