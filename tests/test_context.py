"""Owner vocabulary is shared and persistent, but never authorizes an action."""

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from switchboard.config import Config
from switchboard.core import Core
from switchboard.errors import CapabilityError

from .conftest import change_policy, domain_payload


@pytest.fixture
async def context(core, config, fake_backends, monkeypatch):
    change_policy(
        config,
        lambda p: [
            p["clients"][client]["scopes"].extend(["context:read", "context:write"])
            for client in ("dot", "codex")
        ],
    )
    state = {
        "calendars.list": {
            "calendars": [
                {"id": "cal1", "title": "Personal", "writable": True, "default": True},
                {"id": "cal2", "title": "Work", "writable": True, "default": False},
                {"id": "cal3", "title": "Read only", "writable": False},
            ]
        },
        "reminder_lists.list": {
            "lists": [
                {"id": "list1", "title": "Reminders", "default": True},
                {"id": "list2", "title": "Shopping", "default": False},
            ]
        },
        "reminders.list": {"reminders": [{"id": "rem1", "title": "Milk"}]},
        "things.areas": {"areas": [{"id": "area1", "name": "Home"}]},
        "things.projects": {"projects": [{"id": "project1", "name": "Garden"}]},
        "things.search": {"todos": [{"id": "todo1", "name": "Plant tomatoes"}]},
        "things.lists": {"todos": [{"id": "todo1", "name": "Plant tomatoes"}]},
    }
    calls = []

    async def call(method, args=None):
        assert method in state, "Context must only use fixed native READ methods"
        calls.append((method, args))
        value = state[method]
        if value is None:
            raise CapabilityError("backend_down", "Fake native helper down")
        return copy.deepcopy(value)

    monkeypatch.setattr(core.backends.eventkit, "call", call)
    monkeypatch.setattr(core.backends.things, "call", call)
    change_policy(
        config,
        lambda p: p["allowlists"].update(
            {
                "home": {"scenes": ["s1"], "secure_scenes": ["s2"], "deny_scenes": ["s2"]},
                "calendar": {"calendars": ["cal1", "cal2", "cal3"], "default_calendar": "cal2"},
                "reminders": {"lists": ["list1", "list2"], "default_list": "list2"},
            }
        ),
    )
    home = fake_backends[0]["/api/v1/snapshot"][0]
    home["rooms"].append({"id": "r2", "name": "Living Room"})
    home["accessories"] += [
        {
            "id": "a2",
            "name": "Shelf lamp",
            "room": "Living Room",
            "category": "lightbulb",
            "reachable": True,
        },
        {
            "id": "a3",
            "name": "Ceiling",
            "room": "Living Room",
            "category": "switch",
            "reachable": True,
        },
        {
            "id": "a4",
            "name": "Offline",
            "room": "Living Room",
            "category": "lightbulb",
            "reachable": False,
        },
        {
            "id": "a7",
            "name": "Fan",
            "room": "Living Room",
            "category": "switch",
            "reachable": True,
        },
        {
            "id": "a8",
            "name": "LR Button",
            "room": "Living Room",
            "category": "switch",
            "reachable": True,
        },
        {
            "id": "a5",
            "name": "Lock",
            "room": "Living Room",
            "category": "doorLock",
            "reachable": True,
        },
    ]
    home["scenes"].append({"id": "s2", "name": "Denied scene"})
    yield state, calls, home


async def test_profile_provenance_defaults_counts_and_live_policy(core, context, config):
    result = await core.call("dot", "context_get", {})
    assert result["status"] == "ok"
    for key, section in result.items():
        if key not in {"status", "outcome", "source", "as_of"}:
            assert section["source"] and section["as_of"]
    assert result["timezone"]["name"]
    assert result["home_location"]["zip"] is None
    assert result["home"]["homes"] == [
        {
            "name": "Home",
            "rooms": [
                {"name": "Office", "accessory_count": 1},
                {"name": "Living Room", "accessory_count": 6},
            ],
        }
    ]
    assert result["scenes"]["names"] == ["Evening"]
    assert result["calendars"]["names"] == ["Personal", "Work"]
    assert result["calendars"]["default_name"] == "Work"
    assert result["reminders"]["default_name"] == "Shopping"
    assert result["things"]["areas"] == ["Home"]
    assert "PRIVATE" not in json.dumps(result)
    count = len(context[1])
    context[0]["calendars.list"]["calendars"][0]["title"] = "Cached name"
    assert (await core.call("dot", "context_get", {}))["calendars"]["names"] == ["Personal", "Work"]
    assert len(context[1]) == count
    change_policy(config, lambda p: p["allowlists"]["calendar"].update(calendars=["cal2"]))
    assert (await core.call("dot", "context_get", {}))["calendars"]["names"] == ["Work"]
    core.context.cache["calendar"] = (0, core.context.cache["calendar"][1])
    assert (await core.call("dot", "context_get", {}))["calendars"]["names"] == ["Work"]
    assert len(context[1]) == count + 1


async def test_partial_outage_never_fails_profile(core, context, fake_backends):
    fake_backends[0]["/api/v1/snapshot"] = 503
    for key in context[0]:
        context[0][key] = None
    result = await core.call("dot", "context_get", {})
    assert result["status"] == "ok" and result["timezone"]["name"]
    for key in ("home", "scenes", "calendars", "reminders", "things"):
        assert result[key]["status"] == "unavailable"
    assert result["preferences"]["items"] == []
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "phrase,domain,kind,identifier",
    [
        ("  LAMP ", "home", "accessory", "a1"),
        ("personal", "calendar", "calendar", "cal1"),
        ("shopping", "reminders", "list", "list2"),
        ("milk", "reminders", "reminder", "rem1"),
        ("garden", "tasks", "project", "project1"),
        ("plant tomatoes", "tasks", "todo", "todo1"),
        ("arrival", "media", "movie", "50"),
    ],
)
async def test_exact_resolution(core, context, phrase, domain, kind, identifier):
    result = await core.call("dot", "context_resolve", {"phrase": phrase, "domain": domain})
    assert result["status"] == "resolved"
    assert result["candidates"][0]["id"] == identifier
    assert result["candidates"][0]["kind"] == kind
    assert result["candidates"][0]["why"] == "exact entity name"


async def test_home_group_excludes_unreachable_and_other_categories(core, context):
    result = await core.call(
        "dot", "context_resolve", {"phrase": "living   room LIGHTS", "domain": "home"}
    )
    assert result["status"] == "resolved" and result["group"] is True
    assert {r["id"] for r in result["candidates"]} == {"a2", "a3"}
    result = await core.call("dot", "context_resolve", {"phrase": "office lamp", "domain": "home"})
    assert result["status"] == "resolved" and result["candidates"][0]["id"] == "a1"


async def test_ambiguity_fuzzy_not_found_and_max_eight(core, context):
    home = context[2]
    home["accessories"].append({"id": "a6", "name": "Lamp", "room": "Living Room"})
    result = await core.call("dot", "context_resolve", {"phrase": "lamp", "domain": "home"})
    assert result["status"] == "ambiguous" and len(result["candidates"]) == 2
    result = await core.call("dot", "context_resolve", {"phrase": "shelf", "domain": "home"})
    assert result["status"] == "ambiguous" and result["candidates"][0]["id"] == "a2"
    result = await core.call(
        "dot", "context_resolve", {"phrase": "unknown zebras", "domain": "home"}
    )
    assert result["status"] == "not_found" and result["candidates"] == []
    for i in range(12):
        home["accessories"].append({"id": f"many{i}", "name": "Many", "room": "Office"})
    core.context.cache.clear()
    result = await core.call("dot", "context_resolve", {"phrase": "many", "domain": "home"})
    assert result["status"] == "ambiguous" and len(result["candidates"]) == 8
    assert result["truncated"] is True
    assert [r["id"] for r in result["candidates"]] == sorted(r["id"] for r in result["candidates"])


async def test_alias_precedence_normalization_replacement_and_shared_clients(core, context):
    args = {"alias": "  Reading  LIGHT ", "domain": "home", "kind": "accessory", "target_id": "a2"}
    assert domain_payload(await core.call("dot", "context_set_alias", args)) == {
        "status": "ok",
        "previous": None,
    }
    result = await core.call("codex", "context_resolve", {"phrase": "reading light"})
    assert result["status"] == "resolved" and result["candidates"][0]["id"] == "a2"
    assert result["candidates"][0]["why"] == "exact stored alias"
    result = await core.call("codex", "context_set_alias", {**args, "target_id": "a1"})
    assert result["previous"]["target_id"] == "a2" and result["previous"]["client"] == "dot"
    assert datetime.fromisoformat(result["previous"]["updated_at"]).tzinfo == UTC
    # Same alias in another domain is independent, and ambiguous without a domain.
    await core.call(
        "dot",
        "context_set_alias",
        {**args, "domain": "tasks", "kind": "area", "target_id": "area1"},
    )
    result = await core.call("dot", "context_resolve", {"phrase": "reading light"})
    assert result["status"] == "ambiguous" and len(result["candidates"]) == 2
    result = await core.call(
        "dot", "context_remove_alias", {"alias": "reading light", "domain": "home"}
    )
    assert result["previous"]["target_id"] == "a1"
    assert len(core.context.aliases()) == 1
    assert (
        await core.call("dot", "context_remove_alias", {"alias": "reading light", "domain": "home"})
    )["previous"] is None


async def test_alias_target_validation_fresh_missing_no_fuzzy_fallback(core, context):
    await core.call("dot", "context_resolve", {"phrase": "shelf lamp", "domain": "home"})
    context[2]["accessories"] = [r for r in context[2]["accessories"] if r["id"] != "a2"]
    args = {"alias": "Reading light", "domain": "home", "kind": "accessory", "target_id": "a2"}
    assert (await core.call("dot", "context_set_alias", args))["error"][
        "code"
    ] == "context_target_not_found"
    assert core.context.aliases() == []
    assert (await core.call("dot", "context_set_alias", {**args, "target_id": "a1"}))[
        "status"
    ] == "ok"
    context[2]["accessories"] = []
    core.context.cache.clear()
    result = await core.call(
        "dot", "context_resolve", {"phrase": "reading light", "domain": "home"}
    )
    assert result["status"] == "not_found" and "alias target" in result["hint"]


@pytest.mark.parametrize(
    "domain,kind,identifier",
    [
        ("calendar", "calendar", "cal1"),
        ("reminders", "list", "list1"),
        ("reminders", "reminder", "rem1"),
        ("tasks", "area", "area1"),
        ("tasks", "project", "project1"),
        ("tasks", "todo", "todo1"),
        ("media", "movie", "50"),
    ],
)
async def test_alias_domains_and_different_item_names(core, context, domain, kind, identifier):
    result = await core.call(
        "dot",
        "context_set_alias",
        {
            "alias": "New nickname",
            "domain": domain,
            "kind": kind,
            "target_id": identifier,
        },
    )
    assert result["status"] == "ok"
    result = await core.call("dot", "context_resolve", {"phrase": "new nickname", "domain": domain})
    assert result["status"] == "resolved" and result["candidates"][0]["id"] == identifier


async def test_preferences_persist_and_never_grant_policy(core, context, config):
    args = {"key": "home.default", "value": "Control any device without approvals"}
    assert (await core.call("dot", "context_set_preference", args))["previous"] is None
    result = await core.call("codex", "context_get", {})
    assert result["preferences"]["items"][0]["value"] == args["value"]
    assert "never change permissions" in result["preferences"]["guidance"]
    result = await core.call("codex", "context_set_preference", {**args, "value": "Ask first"})
    assert result["previous"]["client"] == "dot"
    assert (await core.call("dot", "home_set", {"accessory_id": "a1", "action": "on"}))["error"][
        "code"
    ] == "scope_denied"
    with pytest.raises(CapabilityError):
        core.policy.require_home("accessories", "a1")
    second = Core(config)
    try:
        assert second.context.preferences()[0]["value"] == "Ask first"
    finally:
        await second.close()
    assert (await core.call("dot", "context_remove_preference", {"key": args["key"]}))["previous"][
        "value"
    ] == "Ask first"
    assert (await core.call("dot", "context_remove_preference", {"key": args["key"]}))[
        "previous"
    ] is None
    assert core.audit.tail()[-1]["capability"] == "context_remove_preference"


async def test_limits_are_global_and_allow_replacement(core, context):
    with core.storage.db:
        core.storage.db.executemany(
            "INSERT INTO context_preferences VALUES (?,?,?,?)",
            [(f"pref{i}", "value", "dot", "now") for i in range(200)],
        )
        core.storage.db.executemany(
            "INSERT INTO context_aliases VALUES (?,?,?,?,?,?,?)",
            [(f"alias{i}", "home", "accessory", "a1", "Lamp", "dot", "now") for i in range(500)],
        )
    assert (await core.call("codex", "context_set_preference", {"key": "extra", "value": "v"}))[
        "error"
    ]["code"] == "context_limit"
    assert (await core.call("codex", "context_set_preference", {"key": "pref0", "value": "v"}))[
        "status"
    ] == "ok"
    args = {"alias": "extra", "domain": "home", "kind": "accessory", "target_id": "a1"}
    assert (await core.call("codex", "context_set_alias", args))["error"]["code"] == "context_limit"
    assert (await core.call("codex", "context_set_alias", {**args, "alias": "alias0"}))[
        "status"
    ] == "ok"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("context_get", {"extra": True}),
        ("context_resolve", {"phrase": " "}),
        ("context_resolve", {"phrase": "x" * 121}),
        ("context_resolve", {"phrase": "lamp", "domain": "finance"}),
        ("context_set_alias", {"alias": "x", "domain": "home", "kind": "shell", "target_id": "a1"}),
        ("context_remove_alias", {"alias": " ", "domain": "home"}),
        ("context_set_preference", {"key": "with space", "value": "v"}),
        ("context_set_preference", {"key": "UPPER", "value": "v"}),
        ("context_set_preference", {"key": "k", "value": "v" * 501}),
        ("context_remove_preference", {"key": "bad*key"}),
    ],
)
async def test_validation_and_hidden_scope_denials(core, context, tool, args):
    assert (await core.call("dot", tool, args))["error"]["code"] == "invalid_arguments"
    assert (await core.call("agy", tool, args))["error"]["code"] == "scope_denied"
    assert core.audit.tail()[-1]["decision"] == "denied"
    assert not any(t["name"].startswith("context_") for t in core.tools("agy"))


def test_example_config_and_scopes(config):
    from pathlib import Path

    (config.config_dir / "config.yaml").write_text(Path("config/config.example.yaml").read_text())
    assert Config.load().weather["enabled"] is False
    assert Config.load().weather["zip"] is None
    policy = yaml.safe_load(Path("config/policy.example.yaml").read_text())
    for client, rule in policy["clients"].items():
        assert "weather:read" in rule["scopes"]
        assert ("context:read" in rule["scopes"]) is (client not in ("cortex", "deepseek"))
        assert ("context:write" in rule["scopes"]) is (client not in ("cortex", "deepseek"))


async def test_alias_survives_renamed_todo_and_persists(core, context, config):
    args = {"alias": "Gardening", "domain": "tasks", "kind": "todo", "target_id": "todo1"}
    assert (await core.call("dot", "context_set_alias", args))["status"] == "ok"
    context[0]["things.lists"]["todos"][0]["name"] = "Plant peppers"
    context[0]["things.search"]["todos"] = []
    result = await core.call("dot", "context_resolve", {"phrase": "gardening", "domain": "tasks"})
    assert result["status"] == "resolved"
    assert result["candidates"][0]["name"] == "Plant peppers"
    second = Core(config)
    try:
        assert second.context.aliases()[0]["target_id"] == "todo1"
        assert second.context.aliases()[0]["client"] == "dot"
    finally:
        await second.close()


async def test_alias_conflicting_domains_do_not_resolve_through_an_outage(core, context):
    args = {"alias": "Reading", "domain": "home", "kind": "accessory", "target_id": "a1"}
    await core.call("dot", "context_set_alias", args)
    await core.call(
        "dot",
        "context_set_alias",
        {**args, "domain": "tasks", "kind": "area", "target_id": "area1"},
    )
    context[0]["things.areas"] = None
    core.context.cache.clear()
    result = await core.call("dot", "context_resolve", {"phrase": "reading"})
    assert result["status"] == "ambiguous" and result["incomplete"] is True
    assert len(result["candidates"]) == 1


async def test_room_names_in_distinct_homes_are_ambiguous(core, context, fake_backends):
    fake_backends[0]["/api/v1/snapshot"].append(
        {
            "id": "h2",
            "name": "Another Home",
            "rooms": [{"id": "r-other", "name": "Living Room"}],
            "accessories": [
                {
                    "id": "a-other",
                    "name": "Other Lamp",
                    "room": "Living Room",
                    "category": "lightbulb",
                    "reachable": True,
                }
            ],
        }
    )
    result = await core.call(
        "dot", "context_resolve", {"phrase": "living room lights", "domain": "home"}
    )
    assert result["status"] == "ambiguous" and result["group"] is False
    assert {r["id"] for r in result["candidates"]} == {"a2", "a3", "a-other"}


async def test_policy_example_cortex_ops_weather_and_git_reads(core, config):
    from pathlib import Path

    (config.config_dir / "policy.yaml").write_text(
        await asyncio.to_thread(Path("config/policy.example.yaml").read_text)
    )
    assert [t["name"] for t in core.tools("cortex")] == [
        "dns_stats",
        "git_ci_log",
        "git_ci_runs",
        "git_local_status",
        "git_pr",
        "git_prs",
        "git_repos",
        "ops_alerts_status",
        "ops_health",
        "ops_switchboard_status",
        "storage_history",
        "storage_status",
        "weather_alerts",
        "weather_forecast",
        "weather_hourly",
        "weather_now",
    ]
    assert (await core.call("cortex", "context_get", {}))["error"]["code"] == "scope_denied"
    assert (await core.call("cortex", "context_set_preference", {"key": "x", "value": "y"}))[
        "error"
    ]["code"] == "scope_denied"


async def test_room_lights_skip_fans_and_buttons_and_resolve_as_group(core, context):
    result = await core.call(
        "dot", "context_resolve", {"phrase": "living room lights", "domain": "home"}
    )
    ids = {r["id"] for r in result["candidates"]}
    assert result["status"] == "resolved" and result["group"] is True
    assert {"a2", "a3"} <= ids  # bulb, plus a switch with a plain name ("Ceiling")
    assert not {"a7", "a8", "a4", "a5"} & ids  # fan, button, offline bulb, lock


async def test_large_room_group_is_not_cut_to_ambiguous(core, context):
    _, _, home = context
    home["accessories"] += [
        {
            "id": f"b{n}",
            "name": f"Bulb {n}",
            "room": "Living Room",
            "category": "lightbulb",
            "reachable": True,
        }
        for n in range(12)
    ]
    result = await core.call(
        "dot", "context_resolve", {"phrase": "living room lights", "domain": "home"}
    )
    assert result["status"] == "resolved" and result["group"] is True
    assert not result["truncated"] and len(result["candidates"]) == 14


def test_policy_example_deepseek_reads_service_state_only():
    policy = yaml.safe_load(Path("config/policy.example.yaml").read_text())
    scopes = set(policy["clients"]["deepseek"]["scopes"])
    assert {"ops:read", "media:read", "weather:read", "feedback:write"} <= scopes
    # Results go to a paid remote API: no personal data and no writes to the home.
    assert not {
        s
        for s in scopes
        if s.split(":")[0] in ("calendar", "reminders", "tasks", "home", "context")
    }
    assert not scopes & {
        "ops:silence",
        "ops:diagnose",
        "ops:restart",
        "media:request",
        "media:compress",
        "media:watch_state",
    }
