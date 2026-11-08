"""Routines: agents propose, the owner approves, Switchboard runs them.

Proposing never activates anything. Approval binds the exact definition and
the scopes it may use (a hash), and only the owner can give it.
"""

from datetime import UTC, datetime

from ..errors import CapabilityError
from ..memory_subjects import MemorySubject
from ..routines.language import RoutineError, definition_hash, parse
from ..routines.validator import check
from ..schema import obj, string
from .common import array, output

NAME = {"type": "string", "pattern": r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", "maxLength": 64}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
ANY = {"type": "object"}


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def version_row(v, *, full=False):
    row = {
        "routine": v["routine"],
        "version": v["version"],
        "hash": v["hash"],
        "status": v["status"],
        "author": v["author"],
        "scopes": v["scopes"],
        "created_at": iso(v["created_at"]),
        "approved_at": iso(v["approved_at"]),
    }
    if desc := v["definition"].get("description"):
        row["description"] = desc
    if v.get("note"):
        row["note"] = v["note"]
    if full:
        row["yaml"] = v["yaml"]
        row["triggers"] = [t["kind"] for t in v["definition"]["triggers"]]
    return row


def run_row(r):
    return {
        "run_id": r["run_id"],
        "routine": r["routine"],
        "hash": r["hash"],
        "status": r["status"],
        "trigger": r["trigger"],
        "started_at": iso(r["started_at"]),
        "finished_at": iso(r["finished_at"]),
        **({"error": r["error"]} if r["error"] else {}),
        **({"cause": r["cause"]} if r["cause"] else {}),
    }


def engine(core):
    if getattr(core, "routines", None) is None:
        raise CapabilityError("routines_unavailable", "The routine engine isn't running")
    return core.routines


async def prepare(core, client, source, *, check_scopes=True):
    """Parse and check a definition. Returns (definition, scopes, hash, warnings).
    A dry run (`check_scopes=False`) skips whether the client could author it."""
    try:
        definition = parse(source)
    except RoutineError as exc:
        raise CapabilityError("invalid_routine", "; ".join(exc.problems[:8])) from None
    eng = engine(core)
    if not eng.index_ready:
        await eng.refresh_index()
    if not eng.index_ready:
        raise CapabilityError("home_unavailable", "HomeRelay is unreachable; can't check devices")
    scopes, problems, warnings = check(
        core, definition, eng.index, eng.scenes, client if check_scopes else None
    )
    if problems:
        raise CapabilityError("invalid_routine", "; ".join(problems[:8]))
    return definition, scopes, definition_hash(definition, scopes), warnings


def register(registry):
    @registry.declare(
        "routines_list",
        "List routines",
        "Home routines (automations): which exist, which are active, last runs. "
        "Use when checking which home routines exist, which are active and how they last ran. "
        "Requires routine read access. Returns each routine's newest version, status, last run "
        "and the engine's health.",
        obj(),
        scope="routines:read",
        output_schema=output(routines=array(), engine=ANY),
        memory=MemorySubject("routines", result_paths=("routines[].routine",)),
    )
    async def list_routines(core, client, args):
        eng = engine(core)
        latest = {}
        for v in eng.store.versions():
            latest.setdefault(v["routine"], v)
            if v["status"] == "active":
                latest[v["routine"]] = v
        rows = []
        for name, v in sorted(latest.items()):
            row = version_row(v)
            if runs := eng.store.runs(name, 1):
                row["last_run"] = {"status": runs[0]["status"], "at": iso(runs[0]["started_at"])}
            if skip := eng.skips.get(name):
                row["last_skip"] = {"reason": skip["reason"], "at": iso(skip["at"])}
            rows.append(row)
        return {"status": "ok", "routines": rows, "engine": eng.status()}

    @registry.declare(
        "routines_get",
        "Show a routine",
        "Home routine detail: YAML, versions, approved scopes and recent runs. "
        "Use when reading a routine's YAML, versions, approved scopes and recent runs. "
        "Requires the routine name. Returns every version, newest first.",
        obj({"routine": NAME}, ("routine",)),
        scope="routines:read",
        output_schema=output(routine=string(64), versions=array(), runs=array()),
        memory=MemorySubject(
            "routines", arg_keys=("routine",), result_paths=("routine", "runs[].routine")
        ),
    )
    async def get_routine(core, client, args):
        eng = engine(core)
        versions = eng.store.versions(args["routine"])
        if not versions:
            raise CapabilityError("not_found", "No routine by that name")
        return {
            "status": "ok",
            "routine": args["routine"],
            "versions": [version_row(v, full=True) for v in versions[:10]],
            "runs": [run_row(r) for r in eng.store.runs(args["routine"], 10)],
        }

    @registry.declare(
        "routines_runs",
        "Routine runs",
        "Home routine run history: what a routine did and why. "
        "Use when checking what a routine did and why. Requires a routine name, a run ID, or "
        "neither for all routines. Returns recent runs, or one run's journaled steps.",
        obj(
            {
                "routine": NAME,
                "run_id": string(40),
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        scope="routines:read",
        output_schema=output(runs=array(), steps=array()),
        memory=MemorySubject(
            "routines", arg_keys=("routine",), result_paths=("routine", "runs[].routine")
        ),
    )
    async def runs(core, client, args):
        eng = engine(core)
        if run_id := args.get("run_id"):
            run = eng.store.run(run_id)
            if run is None:
                raise CapabilityError("not_found", "No run with that ID")
            steps = [
                {
                    k: s[k]
                    for k in ("path", "kind", "status", "output", "error")
                    if s.get(k) is not None
                }
                | {"at": iso(s["started_at"])}
                for s in eng.store.steps(run_id)
            ]
            return {
                "status": "ok",
                "runs": [run_row(run)],
                "steps": steps,
                "limit": args.get("limit", 20),
            }
        limit = args.get("limit", 20)
        rows = eng.store.runs(args.get("routine"), limit)
        return {"status": "ok", "runs": [run_row(r) for r in rows], "steps": [], "limit": limit}

    @registry.declare(
        "routines_propose",
        "Propose a routine",
        "Propose a home routine (automation) for the owner to approve. "
        "Use when drafting a home automation for the owner to approve. Requires routine YAML "
        "(see the switchboard-routines skill). Returns the checked scopes, the hash the owner "
        "approves and a dry run. Never activates anything.",
        obj({"yaml": string(20_000)}, ("yaml",)),
        scope="routines:propose",
        tier="write",
        idempotent=True,
        timeout_s=30,
        audit_hash_fields=frozenset({"yaml"}),
        output_schema=output(
            routine=string(64),
            version={"type": "integer"},
            hash=HASH,
            scopes=array({"type": "string"}),
            warnings=array({"type": "string"}),
            plan=ANY,
            approve=string(200),
            existing={"type": "boolean"},
        ),
        memory=MemorySubject(
            "routines", arg_keys=("routine",), result_paths=("routine", "runs[].routine")
        ),
    )
    async def propose(core, client, args):
        definition, scopes, hashed, warnings = await prepare(core, client, args["yaml"])
        eng = engine(core)
        stored = eng.store.add_version(
            definition["routine"], args["yaml"], definition, scopes, client, hashed
        )
        plan = await eng.plan(definition)
        command = f"sb routines approve {definition['routine']} {hashed}"
        if not stored["existing"]:
            eng.notify(
                f"routine-proposed:{hashed}",
                f"Routine proposed: {definition['routine']}",
                (
                    f"{client} proposed {definition['routine']} v{stored['version']} "
                    f"(scopes: {', '.join(sorted(scopes)) or 'none'}). "
                    f"Review with routines_get, approve with: {command}"
                ),
            )
        return {
            "status": "proposed" if not stored["existing"] else stored["status"],
            "routine": definition["routine"],
            "version": stored["version"],
            "hash": hashed,
            "scopes": sorted(scopes),
            "warnings": warnings,
            "plan": plan,
            "approve": command,
            "existing": stored["existing"],
        }

    @registry.declare(
        "routines_plan",
        "Dry-run a routine",
        "Dry-run a home routine: what it would do right now. "
        "Use when checking what a routine would do right now without doing it. Requires YAML, "
        "or a routine name for its active version. Returns resolved devices, next scheduled "
        "times, whether conditions pass now, and each step's calls.",
        obj({"routine": NAME, "yaml": string(20_000)}),
        scope="routines:read",
        timeout_s=30,
        output_schema=output(plan=ANY, routine=string(64)),
        memory=MemorySubject(
            "routines", arg_keys=("routine",), result_paths=("routine", "runs[].routine")
        ),
    )
    async def plan(core, client, args):
        eng = engine(core)
        if "yaml" in args:
            definition, _, _, _ = await prepare(core, client, args["yaml"], check_scopes=False)
        elif "routine" in args:
            version = eng.store.version(
                routine=args["routine"], status="active"
            ) or eng.store.version(routine=args["routine"])
            if version is None:
                raise CapabilityError("not_found", "No routine by that name")
            definition = version["definition"]
        else:
            raise CapabilityError("invalid_arguments", "Give yaml or routine")
        return {
            "status": "ok",
            "routine": definition["routine"],
            "plan": await eng.plan(definition),
        }

    @registry.declare(
        "routines_run",
        "Run a routine now",
        "Run an approved home routine now. "
        "Use when the owner asks to run an approved routine that has a manual trigger. "
        "Requires the routine name. Conditions are skipped for manual runs. Returns the run ID.",
        obj({"routine": NAME}, ("routine",)),
        scope="routines:run",
        tier="write",
        idempotent=False,
        output_schema=output(run_id={"type": ["string", "null"]}),
        memory=MemorySubject(
            "routines", arg_keys=("routine",), result_paths=("routine", "runs[].routine")
        ),
    )
    async def run_now(core, client, args):
        run_id = await engine(core).run_manual(args["routine"], client)
        if run_id is None:
            raise CapabilityError("blocked", "The run didn't start (see routines_list last_skip)")
        return {
            "status": "running" if run_id != "queued" else "queued",
            "run_id": None if run_id == "queued" else run_id,
        }

    @registry.declare(
        "routines_disable",
        "Turn a routine off",
        "Turn a home routine off. "
        "Use when a routine misbehaves or the owner asks to stop it. Requires the routine and a "
        "short reason. Returns the disabled routine; its runs stop and it needs a new approval.",
        obj({"routine": NAME, "reason": string(200)}, ("routine", "reason")),
        scope="routines:disable",
        tier="write",
        idempotent=True,
        output_schema=output(routine=string(64)),
        memory=MemorySubject(
            "routines", arg_keys=("routine",), result_paths=("routine", "runs[].routine")
        ),
    )
    async def disable(core, client, args):
        eng = engine(core)
        changed = eng.store.set_status(args["routine"], "disabled", f"{client}: {args['reason']}")
        if not changed:
            raise CapabilityError("not_found", "No active routine by that name")
        await eng.reload()
        return {"status": "disabled", "routine": args["routine"]}

    @registry.declare(
        "routines_approve",
        "Approve a routine version",
        "Approve a home routine version (owner only). "
        "Use when the owner has reviewed a proposed routine and wants it live. Requires the owner "
        "and the exact version hash. Returns the activated version, which replaces the old one.",
        obj({"routine": NAME, "hash": HASH}, ("routine", "hash")),
        scope="routines:approve",
        tier="write",
        idempotent=True,
        timeout_s=30,
        output_schema=output(routine=string(64), version={"type": "integer"}, hash=HASH),
        memory=MemorySubject(
            "routines", arg_keys=("routine",), result_paths=("routine", "runs[].routine")
        ),
    )
    async def approve(core, client, args):
        if client != "owner":
            raise CapabilityError("owner_required", "Only the owner approves routines")
        eng = engine(core)
        version = eng.store.version(hashed=args["hash"])
        if version is None or version["routine"] != args["routine"]:
            raise CapabilityError("not_found", "No version of that routine has this hash")
        if version["status"] == "active":
            return {
                "status": "already_done",
                "routine": version["routine"],
                "version": version["version"],
                "hash": version["hash"],
            }
        # Check again: devices, tools and the author's scopes may have changed since it
        # was proposed.
        _, scopes, hashed, _ = await prepare(core, version["author"], version["yaml"])
        if hashed != version["hash"]:
            raise CapabilityError(
                "changed", "The routine now needs different scopes; propose it again"
            )
        eng.store.approve(hashed, client)
        eng.store.clear_triggers(version["routine"])
        await eng.reload()
        return {
            "status": "approved",
            "routine": version["routine"],
            "version": version["version"],
            "hash": hashed,
        }

    @registry.declare(
        "routines_pause",
        "Pause or resume all routines",
        "Pause or resume all home routines (owner only). "
        "Use when the owner wants every routine stopped at once, or resumed. Requires the owner. "
        "Returns the new state; running routines stop at their next step.",
        obj({"paused": {"type": "boolean"}}, ("paused",)),
        scope="routines:approve",
        tier="write",
        idempotent=True,
        output_schema=output(paused={"type": "boolean"}),
        memory=MemorySubject("routines", subjects=("routines",)),
    )
    async def pause(core, client, args):
        if client != "owner":
            raise CapabilityError("owner_required", "Only the owner pauses routines")
        eng = engine(core)
        eng.store.set_meta("paused", "1" if args["paused"] else "0")
        if args["paused"]:
            for _routine, run_ids in list(eng.running.items()):
                for run_id in list(run_ids):
                    eng.cancel(run_id, "paused")
        return {"status": "paused" if args["paused"] else "resumed", "paused": args["paused"]}
