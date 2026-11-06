"""Things contracts use only the fake private socket; no installed app is contacted."""

import json

import pytest

from switchboard import cli
from switchboard.config import Config
from switchboard.errors import CapabilityError
from switchboard.policy import Policy

from .conftest import change_policy
from .test_eventkit import META, socket_helper  # noqa: F401

TODO = {
    "id": "t1",
    "name": "Milk",
    "notes": "Task notes",
    "status": "open",
    "due": "2026-11-01",
    "deadline": "2026-11-01",
    "activation_date": None,
    "project_id": "p1",
    "project_name": "Shopping",
    "area_id": "a1",
    "area_name": "Home",
    "tags": ["Errand"],
    "private": "NEVER_RETURN",
}
RULES = {"allow_all": True, "projects": [], "areas": [], "deny_projects": [], "deny_areas": []}


def allow(config, **rules):
    change_policy(config, lambda p: p["allowlists"].update(tasks={**RULES, **rules}))


@pytest.fixture
async def things_helper(config, socket_helper):  # noqa: F811
    state, calls, path = socket_helper
    config.backends["things"]["enabled"] = True
    # Enabling Things is independent of Calendar/Reminders, but shares its socket.
    config.backends["eventkit"]["enabled"] = False
    state.update(
        {
            "things.status": {"running": True, "automation": "authorized"},
            "things.request_access": {"running": True, "automation": "authorized"},
            "things.lists": {"todos": [TODO], "truncated": True, **META},
            "things.search": {"todos": [TODO], **META},
            "things.projects": {"projects": [{**TODO, "id": "p1"}], **META},
            "things.areas": {
                "areas": [{"id": "a1", "name": "Home", "private": "NEVER_RETURN"}],
                **META,
            },
        }
    )
    writes = []
    current = dict(TODO)

    def mutate(args):
        try:
            # Emulate the native helper's destination resolution, including a task
            # moved since discovery. No capped-list preflight may authorize writes.
            Policy(config.config_dir / "policy.yaml").require_task(
                args, current["project_id"], current["area_id"]
            )
        except CapabilityError:
            return {"ok": False, "error": {"code": "resource_denied", "message": "PRIVATE"}}
        if args.get("checklist") or args.get("when") in {"evening", "tonight"}:
            return {"ok": False, "error": {"code": "unsupported", "message": "PRIVATE"}}
        writes.append(args)
        return {"todo": {**current, "status": "completed" if "todo_id" in args else "open"}, **META}

    state["things.add"] = state["things.complete"] = mutate

    def update(args):
        try:
            policy = Policy(config.config_dir / "policy.yaml")
            policy.require_task(args, current["project_id"], current["area_id"])
            if (
                current.get("class", "selected to do") not in {"to do", "selected to do"}
                or current["status"] != "open"
            ):
                raise CapabilityError("resource_denied", "Closed or project")
            project, area = current["project_id"], current["area_id"]
            if "project_id" in args:
                project = args["project_id"]
                # Fake Things project metadata; helpers resolve the parent themselves.
                parents = {"p1": "a1", "p2": "a2", "p0": None}
                if project not in parents:
                    return {"ok": False, "error": {"code": "not_found"}}
                area = parents[project]
            elif "area_id" in args:
                project, area = None, args["area_id"]
            policy.require_task(args, project, area)
        except CapabilityError:
            return {"ok": False, "error": {"code": "resource_denied", "message": "PRIVATE"}}
        if set(args.get("tags", [])) - {"Errand", "Work"}:
            return {"ok": False, "error": {"code": "invalid_arguments"}}
        writes.append(args)
        current.update(project_id=project, area_id=area)
        for key, value in args.items():
            if key in {"title", "notes", "deadline", "tags", "when"}:
                current[{"title": "name", "when": "activation_date"}.get(key, key)] = value
        return {"todo": current, **META}

    state["things.update"] = update
    yield state, calls, path, writes, current


@pytest.mark.parametrize(
    "name,list_name",
    [("today", "Today"), ("inbox", "Inbox"), ("upcoming", "Upcoming"), ("anytime", "Anytime")],
)
async def test_task_lists(core, things_helper, name, list_name):
    _, calls, _, _, _ = things_helper
    result = await core.call("dot", "tasks_" + name, {"limit": 1})
    assert result["todos"][0]["id"] == "t1"
    assert result["todos"][0]["tags"] == ["Errand"]
    assert result["timezone"] == META["timezone"]
    assert result["truncated"] is True
    assert calls[-1] == {"method": "things.lists", "args": {"list": list_name, "limit": 1}}
    assert "NEVER_RETURN" not in json.dumps(result)


async def test_discovery_search_and_status_do_not_prompt(core, things_helper):
    _, calls, _, _, _ = things_helper
    query = '" & do shell script "never" -- 雪'
    assert (await core.call("dot", "tasks_search", {"query": query}))["todos"]
    assert calls[-1]["args"]["query"] == query
    assert (await core.call("dot", "tasks_list_projects", {}))["projects"][0]["id"] == "p1"
    assert (await core.call("dot", "tasks_list_areas", {}))["areas"] == [
        {"id": "a1", "name": "Home"}
    ]
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["backends"]["things"] == {
        "status": "ok",
        "reason": "reachable; Automation status reported without prompting",
        "running": True,
        "automation": "authorized",
    }
    assert not any("request_access" in call["method"] for call in calls)
    assert "things.request_access" not in core.registry.capabilities
    assert "things.status" not in core.registry.capabilities


async def test_scope_and_missing_policy_fail_closed(core, config, things_helper):
    _, calls, _, writes, _ = things_helper
    for name, args in [("tasks_add", {"title": "Milk"}), ("tasks_complete", {"todo_id": "t1"})]:
        assert (await core.call("dot", name, args))["error"]["code"] == "resource_denied"
        assert (await core.call("agy", name, args))["error"]["code"] == "scope_denied"
    assert calls == writes == []
    for name in ["tasks_today", "tasks_list_projects", "tasks_list_areas", "tasks_search"]:
        result = await core.call("agy", name, {"query": "Milk"} if name.endswith("search") else {})
        assert result["error"]["code"] == "scope_denied"
        assert name not in {tool["name"] for tool in core.tools("agy")}
    assert calls == []


@pytest.mark.parametrize(
    "rules,project,area,allowed",
    [
        ({}, "p1", "a1", True),
        ({"deny_projects": ["p1"]}, "p1", "a1", False),
        ({"deny_areas": ["a1"]}, "p1", "a1", False),
        ({"allow_all": False}, "p1", "a1", False),
        ({"allow_all": False, "projects": ["p1"]}, "p1", "a1", True),
        ({"allow_all": False, "areas": ["a1"]}, "p1", "a1", True),
        ({"allow_all": False, "projects": ["p1"], "deny_areas": ["a1"]}, "p1", "a1", False),
        ({"allow_all": False}, None, None, True),
        ({"allow_all": False, "areas": ["a1"]}, None, "a1", True),
        ({"allow_all": False}, None, "a1", False),
    ],
)
async def test_current_destination_controls_every_write(
    core, config, things_helper, rules, project, area, allowed
):
    _, calls, _, writes, current = things_helper
    allow(config, **rules)
    current.update(project_id=project, area_id=area)
    for name, args in [
        ("tasks_complete", {"todo_id": "t1"}),
        (
            "tasks_add",
            {
                "title": "Milk",
                **({"project_id": project} if project else {"area_id": area} if area else {}),
            },
        ),
    ]:
        result = await core.call("dot", name, args)
        assert (result["status"] == "ok") is allowed
        if allowed:
            assert calls[-1]["args"]["allow_all"] == rules.get("allow_all", True)
            assert result["todo"]["id"] == "t1"
        else:
            assert result["error"]["code"] in {"resource_denied", "things_resource_denied"}
    assert len(writes) == (2 if allowed else 0)


async def test_policy_reload_and_current_area_check(core, config, things_helper):
    _, _, _, writes, current = things_helper
    allow(config, projects=["p1"], allow_all=False)
    assert (await core.call("dot", "tasks_complete", {"todo_id": "t1"}))["status"] == "ok"
    allow(config, projects=["p1"], allow_all=False, deny_areas=["moved-area"])
    current["area_id"] = "moved-area"
    assert (await core.call("dot", "tasks_complete", {"todo_id": "t1"}))["error"][
        "code"
    ] == "things_resource_denied"
    assert len(writes) == 1
    assert any(
        row["capability"] == "tasks_complete" and row["outcome"] == "error"
        for row in core.audit.tail()
    )


@pytest.mark.parametrize(
    "name,args",
    [
        ("tasks_today", {"limit": 201}),
        ("tasks_inbox", {"limit": True}),
        ("tasks_search", {"query": "x" * 201}),
        ("tasks_search", {"query": "x", "limit": 51}),
        ("tasks_search", {"query": " "}),
        ("tasks_add", {"title": " "}),
        ("tasks_add", {"title": "Milk", "when": "next week"}),
        ("tasks_add", {"title": "Milk", "when": "2026-02-30"}),
        ("tasks_add", {"title": "Milk", "deadline": "2026-1-01"}),
        ("tasks_add", {"title": "Milk", "project_id": "p1", "area_id": "a1"}),
        ("tasks_add", {"title": "Milk", "tags": ["x"] * 11}),
        ("tasks_add", {"title": "Milk", "tags": ["a,b"]}),
        ("tasks_add", {"title": "Milk", "checklist": ["x"] * 21}),
        ("tasks_add", {"title": "Milk", "checklist": ["a\nb"]}),
        ("tasks_complete", {"todo_id": "t1", "allow_all": True}),
        ("tasks_complete", {"todo_id": ""}),
    ],
)
async def test_invalid_task_inputs_never_reach_helper(core, things_helper, name, args):
    assert (await core.call("dot", name, args))["error"]["code"] == "invalid_arguments"
    assert things_helper[1] == things_helper[3] == []


async def test_add_preserves_data_and_no_automatic_retry(core, config, things_helper):
    state, calls, _, writes, _ = things_helper
    allow(config)
    args = {
        "title": '雪 "quote" \\ newline\n',
        "notes": "a\nb",
        "when": "tomorrow",
        "deadline": "2026-11-01",
        "tags": ["Errand"],
        "project_id": "p1",
    }
    assert (await core.call("dot", "tasks_add", args))["status"] == "ok"
    assert all(writes[-1][key] == value for key, value in args.items())
    state["things.add"] = {"ok": False, "error": {"code": "backend_error", "message": "PRIVATE"}}
    before = len(calls)
    result = await core.call("dot", "tasks_add", args)
    assert result["error"]["code"] == "things_backend_error"
    assert len(calls) == before + 1
    assert "PRIVATE" not in json.dumps(result)


async def test_unsupported_features_do_not_mutate(core, config, things_helper):
    allow(config)
    for extra in [{"when": "tonight"}, {"when": "evening"}, {"checklist": ["Milk"]}]:
        result = await core.call("dot", "tasks_add", {"title": "Milk", **extra})
        assert result["error"]["code"] == "things_unsupported"
    assert things_helper[3] == []


@pytest.mark.parametrize(
    "value",
    [
        {"todos": [TODO, TODO]},
        {"todos": [{**TODO, "id": ""}]},
        {"todos": [{**TODO, "tags": [True]}]},
        {"todos": [{"id": "t1", "name": "Milk"}]},
        {"todos": "PRIVATE"},
    ],
)
async def test_malformed_helper_results_fail_safely(core, things_helper, value):
    things_helper[0]["things.lists"] = value
    result = await core.call("dot", "tasks_today", {"limit": 1})
    assert result["error"]["code"] == "backend_invalid"
    assert "PRIVATE" not in json.dumps(result)


async def test_disabled_permission_and_socket_failures(core, config, things_helper):
    state, calls, path, _, _ = things_helper
    config.backends["things"]["enabled"] = False
    assert (await core.call("dot", "tasks_today", {}))["error"]["code"] == "backend_disabled"
    assert calls == []
    config.backends["things"]["enabled"] = True
    for permission in ["denied", "not_determined", "unavailable"]:
        state["things.status"]["automation"] = permission
        assert (await core.backends.things.probe())["status"] == "degraded"
    state["things.status"] = {"running": False, "automation": "unavailable"}
    closed = await core.backends.things.probe()  # A closed app opens on the next read.
    assert closed["status"] == "ok" and "opens on demand" in closed["reason"]
    state["things.status"] = {"running": False, "automation": "denied"}
    assert (await core.backends.things.probe())["status"] == "degraded"
    state["things.status"] = {"running": True, "automation": "authorized"}
    state["things.lists"] = {"ok": False, "error": {"code": "unauthorized", "message": "PRIVATE"}}
    assert (await core.call("dot", "tasks_today", {}))["error"]["code"] == "things_unauthorized"
    path.chmod(0o666)
    assert (await core.call("dot", "tasks_today", {}))["error"]["code"] == "backend_invalid"
    path.unlink()
    assert (await core.call("dot", "tasks_today", {}))["error"]["code"] == "backend_down"
    assert not any("request_access" in call["method"] for call in calls)


async def test_local_owner_cli_access_only(config, things_helper):
    _, calls, _, _, _ = things_helper
    args = cli.parser().parse_args(["--client", "dot", "things", "request-access"])
    with pytest.raises(CapabilityError, match="owner"):
        await cli.execute(args, config)
    assert calls == []
    args = cli.parser().parse_args(["things", "status"])
    assert (await cli.execute(args, config))["automation"] == "authorized"
    assert calls[-1]["method"] == "things.status"
    args = cli.parser().parse_args(["things", "request-access"])
    assert (await cli.execute(args, config))["status"] == "ok"
    assert calls[-1]["method"] == "things.request_access"
    (config.token_dir / "owner.token").unlink()
    with pytest.raises((OSError, CapabilityError)):
        await cli.execute(args, config)


@pytest.mark.parametrize(
    "rules",
    [
        {"allow_all": "yes"},
        {"allow_all": True, "unknown": []},
        {"allow_all": True, "projects": [True]},
        {"allow_all": True, "deny_areas": "a1"},
    ],
)
def test_task_policy_validation(config, rules):
    change_policy(config, lambda p: p["allowlists"].update(tasks=rules))
    with pytest.raises(CapabilityError, match="Policy"):
        Policy(config.config_dir / "policy.yaml").load()


def test_things_config_shares_socket(config):
    (config.config_dir / "config.yaml").write_text("backends: {things: {enabled: true}}")
    loaded = Config.load()
    assert loaded.backends["things"] == {"enabled": True}
    assert loaded.backends["eventkit"]["enabled"] is False
    (config.config_dir / "config.yaml").write_text("backends: {things: {socket: /tmp/other}}")
    with pytest.raises(ValueError):
        Config.load()


@pytest.mark.parametrize(
    "value",
    [
        {"running": True, "automation": []},
        {"running": True, "automation": {}},
        {"running": True, "automation": "unknown"},
        {"running": "yes", "automation": "authorized"},
    ],
)
async def test_malformed_things_status_does_not_break_doctor(core, things_helper, value):
    things_helper[0]["things.status"] = value
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["status"] == "degraded"
    assert result["backends"]["things"]["status"] == "down"


def test_example_grants_task_scopes_to_agents():
    from pathlib import Path

    from switchboard.auth import CLIENTS

    policy = Policy(Path(__file__).resolve().parents[1] / "config/policy.example.yaml")
    for client in CLIENTS:
        if client in ("cortex", "deepseek"):
            assert not {"tasks:read", "tasks:write"} & set(policy.client(client)["scopes"])
            continue
        assert {"tasks:read", "tasks:write"} <= set(policy.client(client)["scopes"])
    assert "approvals:confirm" not in policy.client("owner")["scopes"]


async def test_untitled_project_does_not_break_listing(core, things_helper):
    state = things_helper[0]
    state["things.projects"] = {
        "projects": [{**TODO, "id": "p1"}, {**TODO, "id": "p2", "name": ""}],
        **META,
    }
    projects = (await core.call("dot", "tasks_list_projects", {}))["projects"]
    assert [p["id"] for p in projects] == ["p1", "p2"]


def test_applescript_accepts_selected_todos():
    """Things reports ordinary to-dos as "selected to do"; a bare class check drops them all."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "native/SwitchboardEventKit/Sources/EventKitBridge/Resources/Things.applescript"
    ).read_text()
    assert "class of t is to do" not in source
    assert "class of t is not to do" not in source
    assert source.count("{to do, selected to do}") == 3


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"title": " "},
        {"title": "x" * 201},
        {"notes": None},
        {"notes": "x" * 2001},
        {"deadline": ""},
        {"deadline": "2026-02-30"},
        {"deadline": True},
        {"when": "evening"},
        {"when": "tonight"},
        {"when": "2026-02-30"},
        {"when": None},
        {"when": "2026-1-01"},
        {"project_id": "p2", "area_id": "a2"},
        {"project_id": " "},
        {"area_id": None},
        {"tags": ["a,b"]},
        {"tags": ["a\nb"]},
        {"tags": ["a\rb"]},
        {"tags": [" "]},
        {"tags": [True]},
        {"tags": ["x"] * 11},
        {"tags": ["x" * 201]},
        {"checklist": []},
        {"allow_all": True},
        {"todo_id": "", "title": "Changed"},
    ],
)
async def test_update_schema_rejects_invalid_changes(core, things_helper, extra):
    result = await core.call("dot", "tasks_update", {"todo_id": "t1", **extra})
    assert result["error"]["code"] == "invalid_arguments"
    assert things_helper[1] == things_helper[3] == []


@pytest.mark.parametrize(
    "extra,field,value",
    [
        (
            {"title": '雪 " & do shell script "never" --'},
            "name",
            '雪 " & do shell script "never" --',
        ),
        ({"notes": ""}, "notes", ""),
        ({"deadline": None}, "deadline", None),
        ({"deadline": "2026-12-01"}, "deadline", "2026-12-01"),
        ({"tags": []}, "tags", []),
        ({"tags": ["Work"]}, "tags", ["Work"]),
        ({"project_id": "p2"}, "project_id", "p2"),
        ({"area_id": "a2"}, "project_id", None),
        *[
            ({"when": value}, "activation_date", value)
            for value in ["today", "tomorrow", "anytime", "someday", "2026-12-01"]
        ],
    ],
)
async def test_update_preserves_omissions_and_explicit_clears(
    core, config, things_helper, extra, field, value
):
    allow(config)
    _, calls, _, writes, current = things_helper
    before = dict(current)
    result = await core.call("dot", "tasks_update", {"todo_id": "t1", **extra})
    assert result["status"] == "ok"
    assert result["todo"][field] == value
    assert result["todo"]["status"] == "open"
    for key in {"name", "notes", "deadline", "tags", "activation_date"} - {field}:
        assert result["todo"][key] == before[key]
    assert calls[-1]["method"] == "things.update"
    assert writes[-1] == {"todo_id": "t1", **extra, **RULES}
    assert "NEVER_RETURN" not in json.dumps(result)


@pytest.mark.parametrize(
    "rules,extra,allowed",
    [
        ({"deny_projects": ["p1"]}, {"project_id": "p2"}, False),
        ({"deny_areas": ["a1"]}, {"area_id": "a2"}, False),
        ({"deny_projects": ["p2"]}, {"project_id": "p2"}, False),
        ({"deny_areas": ["a2"]}, {"project_id": "p2"}, False),
        ({"deny_areas": ["a2"]}, {"area_id": "a2"}, False),
        ({"allow_all": False, "projects": ["p1"]}, {"project_id": "p2"}, False),
        ({"allow_all": False, "projects": ["p2"]}, {"project_id": "p2"}, False),
        ({"allow_all": False, "projects": ["p1", "p2"]}, {"project_id": "p2"}, True),
        ({"allow_all": False, "areas": ["a1", "a2"]}, {"project_id": "p2"}, True),
        ({"allow_all": False, "projects": ["p1"], "areas": ["a2"]}, {"area_id": "a2"}, True),
    ],
)
async def test_update_checks_current_and_new_destinations(
    core, config, things_helper, rules, extra, allowed
):
    allow(config, **rules)
    _, _, _, writes, current = things_helper
    before = dict(current)
    result = await core.call("dot", "tasks_update", {"todo_id": "t1", "title": "Changed", **extra})
    assert (result["status"] == "ok") is allowed
    assert len(writes) == int(allowed)
    if not allowed:
        assert result["error"]["code"] in {"resource_denied", "things_resource_denied"}
        assert current == before


@pytest.mark.parametrize(
    "state", [{"status": "completed"}, {"status": "canceled"}, {"class": "project"}]
)
async def test_update_refuses_closed_tasks_and_projects(core, config, things_helper, state):
    allow(config)
    things_helper[4].update(state)
    result = await core.call("dot", "tasks_update", {"todo_id": "t1", "notes": "Changed"})
    assert result["error"]["code"] == "things_resource_denied"
    assert not things_helper[3]


async def test_update_scope_policy_reload_unknown_tags_and_no_retry(core, config, things_helper):
    state, calls, _, writes, _ = things_helper
    args = {"todo_id": "t1", "title": "Changed"}
    assert (await core.call("dot", "tasks_update", args))["error"]["code"] == "resource_denied"
    allow(config)
    assert (await core.call("agy", "tasks_update", args))["error"]["code"] == "scope_denied"
    assert calls == []
    assert (await core.call("dot", "tasks_update", {**args, "tags": ["New"]}))["error"][
        "code"
    ] == "things_invalid_arguments"
    assert not writes
    assert (await core.call("dot", "tasks_update", args))["status"] == "ok"
    allow(config, deny_areas=["a1"])
    assert (await core.call("dot", "tasks_update", args))["error"][
        "code"
    ] == "things_resource_denied"
    allow(config)
    state["things.update"] = {"ok": False, "error": {"code": "backend_error", "message": "PRIVATE"}}
    before = len(calls)
    result = await core.call("dot", "tasks_update", args)
    assert result["error"]["code"] == "things_backend_error"
    assert len(calls) == before + 1
    assert "PRIVATE" not in json.dumps(result)


def test_fixed_update_script_checks_before_mutation_and_shares_dates():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "native/SwitchboardEventKit/Sources/EventKitBridge/Resources/Things.applescript"
    ).read_text()
    update = source.split("on updateTodo(", 1)[1].split("end updateTodo", 1)[0]
    first_mutation = update.index("set project of t")
    assert update.index("{to do, selected to do}") < first_mutation
    assert update.index("if status of t is not open") < first_mutation
    assert update.count("my checkDestination(") == 3
    assert update.rindex("my checkDestination(") < first_mutation
    assert update.index("exists tag named") < first_mutation
    assert "my applyDates(t," in update
    assert source.count("schedule t for scheduledDay") == 1
    assert source.count("my applyDates(t,") == 2


def test_fixed_applescript_compiles_only(tmp_path):
    import subprocess
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "native/SwitchboardEventKit/Sources/EventKitBridge/Resources/Things.applescript"
    )
    subprocess.run(
        ["/usr/bin/osacompile", "-o", str(tmp_path / "things.scpt"), str(source)],
        check=True,
        capture_output=True,
    )
