"""Everyday writes use isolated fake services and enter through Core."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from aiohttp import web

from switchboard.config import Config
from switchboard.core import Core

from .conftest import change_policy, domain_payload, running_app


@pytest.fixture
async def writes(config):
    for name, value in (("read", "fake-read"), ("control", "fake-control"), ("key", "fake-key")):
        path = config.config_dir / name
        path.write_text(value)
        path.chmod(0o600)
    change_policy(
        config,
        lambda p: p["allowlists"].update(
            home={"allow_all_safe": True, "secure_accessories": [], "secure_scenes": []},
            media={"movies": ["42"], "shows": ["42"]},
        ),
    )
    change_policy(
        config,
        lambda p: [
            p["clients"][name]["scopes"].extend(["media:request", "home:control", "home:secure"])
            for name in ("dot", "owner", "codex")
        ],
    )
    lamp = {
        "id": "lamp-id",
        "name": "Lamp",
        "category": "lightbulb",
        "state": {"on": True},
        "services": [
            {
                "type": "lightbulb",
                "primary": True,
                "characteristics": [
                    {"id": "power-id", "type": "on", "writable": True, "value": True},
                    {"id": "bright-id", "type": "brightness", "writable": True, "value": 30},
                    {"id": "read-id", "type": "currentTemperature", "writable": False},
                    {"id": "custom-id", "type": "custom.secret", "writable": True},
                ],
            }
        ],
    }
    lock = {
        "id": "lock-id",
        "category": "doorLock",
        "state": {"lockTarget": 1},
        "services": [
            {
                "type": "lock",
                "characteristics": [
                    {"id": "lock-char", "type": "lockTarget", "writable": True, "value": 1},
                ],
            }
        ],
    }
    state = {
        "movie": {"id": 42, "title": "Movie", "releaseDate": "2024-01-01", "mediaInfo": None},
        "tv": {"id": 42, "name": "Show", "firstAirDate": "2020-01-01", "mediaInfo": None},
        "accessories": {"lamp-id": lamp, "lock-id": lock},
        "scenes": [{"id": "scene-id", "name": "Evening", "kind": "user", "home": "Home"}],
        "post_status": 201,
        "home_status": 200,
    }
    calls = []

    async def handle(request):
        path = request.path
        payload = await request.json() if request.can_read_body else None
        calls.append((request.method, path, payload))
        if path in ("/api/v1/movie/42", "/api/v1/tv/42", "/api/v1/request"):
            assert request.headers["X-Api-Key"] == "fake-key"
            if request.method == "GET":
                return web.json_response(state["movie" if "/movie/" in path else "tv"])
            return web.json_response(
                {"id": 71, "status": 1, "private": "PRIVATE"}, status=state["post_status"]
            )
        assert request.headers["Authorization"] == (
            "Bearer fake-read" if request.method == "GET" else "Bearer fake-control"
        )
        if request.method != "GET" and state["home_status"] != 200:
            return web.json_response({"secret": "PRIVATE"}, status=state["home_status"])
        if path == "/api/v1/scenes":
            return web.json_response(state["scenes"])
        if path.endswith("/run"):
            return web.json_response(state["scenes"][0])
        if "/characteristics/" in path:
            assert request.method == "PUT"
            return web.json_response({"id": path.rsplit("/", 1)[-1], "private": "PRIVATE"})
        identifier = path.split("/accessories/")[-1].removesuffix("/toggle")
        # HomeRelay permits names; Switchboard must refuse their resolved ID for writes.
        identifier = "lamp-id" if identifier == "Lamp" else identifier
        return web.json_response(state["accessories"][identifier])

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with running_app(app) as url:
        config.backends["home"].update(
            url=url + "/api/v1",
            token_file=str(config.config_dir / "read"),
            control_token_file=str(config.config_dir / "control"),
        )
        config.backends["overseerr"].update(
            url=url, enabled=True, api_key_file=str(config.config_dir / "key")
        )
        core = Core(config)
        await core.start()
        try:
            yield core, state, calls
        finally:
            await core.close()


def mutations(calls):
    return [call for call in calls if call[0] != "GET"]


@pytest.mark.parametrize(
    "cap,kind,seasons",
    [
        ("media_request_movie", "movie", None),
        ("media_request_show", "tv", None),
        ("media_request_show", "tv", "all"),
        ("media_request_show", "tv", [0, 1, 3]),
    ],
)
async def test_media_request_payload_and_dedup(writes, cap, kind, seasons):
    core, state, calls = writes
    args = {"tmdb_id": 42}
    if seasons is not None:
        args["seasons"] = seasons
    first = await core.call("dot", cap, args)
    assert domain_payload(first) == {
        "status": "requested",
        "request_id": 71,
        "title": "Movie" if kind == "movie" else "Show",
        "year": "2024" if kind == "movie" else "2020",
    }
    again = await core.call("codex", cap, args)
    assert again["status"] == "already_requested"
    expected = {"mediaType": kind, "mediaId": 42}
    if kind == "tv":
        expected["seasons"] = seasons if seasons is not None else "all"
    assert mutations(calls) == [("POST", "/api/v1/request", expected)]
    assert "PRIVATE" not in json.dumps(first)


@pytest.mark.parametrize(
    "status,requests,expected",
    [
        (5, [], "already_available"),
        (2, [], "already_requested"),
        (3, [], "already_requested"),
        (4, [], "already_requested"),
        (1, [{"id": 9, "status": 1}], "already_requested"),
        (1, [{"id": 9, "status": 2}], "already_requested"),
        (1, [{"id": 9, "status": 3}], "requested"),
        # Seerr 3.x statuses: completed/failed requests, blocklisted and deleted media.
        (5, [{"id": 9, "status": 5}], "already_available"),
        (1, [{"id": 9, "status": 4}], "requested"),
        (6, [], "blocklisted"),
        (7, [], "requested"),
    ],
)
@pytest.mark.parametrize(
    "cap,kind", [("media_request_movie", "movie"), ("media_request_show", "tv")]
)
async def test_media_preflight(writes, status, requests, expected, cap, kind):
    core, state, calls = writes
    state[kind]["mediaInfo"] = {"status": status, "requests": requests}
    result = await core.call("dot", cap, {"tmdb_id": 42})
    assert result["status"] == expected
    assert (
        result["outcome"]
        == {
            "requested": "accepted",
            "already_available": "already_done",
            "already_requested": "already_done",
            "blocklisted": "blocked",
        }[expected]
    )
    assert len(mutations(calls)) == (expected == "requested")


async def test_media_concurrent_and_restart(writes):
    core, state, calls = writes
    result = await asyncio.gather(
        *(core.call("dot", "media_request_movie", {"tmdb_id": 42}) for _ in range(8))
    )
    assert sum(row["status"] == "requested" for row in result) == 1
    restarted = Core(core.config)
    await restarted.start()
    try:
        assert (await restarted.call("dot", "media_request_movie", {"tmdb_id": 42}))[
            "status"
        ] == "already_requested"
    finally:
        await restarted.close()
    assert len(mutations(calls)) == 1


async def test_media_uncertain_submission_never_replayed(writes):
    core, state, calls = writes
    state["post_status"] = 502
    assert (await core.call("dot", "media_request_show", {"tmdb_id": 42}))["error"][
        "code"
    ] == "backend_error"
    state["post_status"] = 201
    result = await core.call("dot", "media_request_show", {"tmdb_id": 42, "seasons": [2]})
    assert result["error"]["code"] == "request_uncertain"
    assert len(mutations(calls)) == 1
    state["tv"]["mediaInfo"] = {"status": 2, "requests": [{"id": 71, "status": 1}]}
    assert (await core.call("dot", "media_request_show", {"tmdb_id": 42}))["request_id"] == 71
    assert "PRIVATE" not in json.dumps(core.audit.tail())


@pytest.mark.parametrize(
    "cap,args",
    [
        ("media_request_movie", {"tmdb_id": True}),
        ("media_request_show", {"tmdb_id": 0}),
        ("media_request_show", {"tmdb_id": 42, "seasons": []}),
        ("media_request_show", {"tmdb_id": 42, "seasons": [-1]}),
        ("media_request_show", {"tmdb_id": 42, "seasons": [True]}),
        ("media_request_show", {"tmdb_id": 42, "seasons": "latest"}),
        ("media_request_movie", {"tmdb_id": 42, "profileId": 1}),
        ("home_set", {"accessory_id": "lamp-id", "action": "set"}),
        ("home_set", {"accessory_id": "lamp-id", "action": "on", "value": 1}),
        ("home_set", {"accessory_id": "lamp-id", "action": "on", "characteristic": "on"}),
        ("home_set", {"name": "Lamp", "action": "on"}),
        ("home_run_scene", {"name": "Evening"}),
    ],
)
async def test_invalid_write_args(writes, cap, args):
    core, state, calls = writes
    assert (await core.call("dot", cap, args))["error"]["code"] == "invalid_arguments"
    assert not mutations(calls)


@pytest.mark.parametrize(
    "action,payload,suffix",
    [("on", {"on": True}, ""), ("off", {"on": False}, ""), ("toggle", {}, "/toggle")],
)
async def test_home_power_routes(writes, action, payload, suffix):
    core, state, calls = writes
    result = await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": action})
    assert result["status"] == "ok"
    assert mutations(calls) == [("POST", "/api/v1/accessories/lamp-id" + suffix, payload)]


@pytest.mark.parametrize("characteristic", ["brightness", "bright-id"])
async def test_home_characteristic_is_bound_to_accessory(writes, characteristic):
    core, state, calls = writes
    result = await core.call(
        "dot",
        "home_set",
        {"accessory_id": "lamp-id", "action": "set", "characteristic": characteristic, "value": 50},
    )
    assert result["status"] == "ok"
    assert mutations(calls) == [("PUT", "/api/v1/characteristics/bright-id", {"value": 50})]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("characteristic", ["lock-char", "read-id", "custom-id", "identify"])
async def test_home_rejects_foreign_readonly_and_custom_characteristics(writes, characteristic):
    core, state, calls = writes
    result = await core.call(
        "dot",
        "home_set",
        {"accessory_id": "lamp-id", "action": "set", "characteristic": characteristic, "value": 1},
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert not mutations(calls)


@pytest.mark.parametrize(
    "category",
    [
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
    ],
)
async def test_home_safe_types(writes, category):
    core, state, calls = writes
    row = state["accessories"]["lamp-id"]
    row["category"] = category
    row["services"][0]["type"] = category
    assert (await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"}))[
        "status"
    ] == "ok"


@pytest.mark.parametrize(
    "category", ["doorLock", "garageDoor", "securitySystem", "door", "window", "alarm"]
)
async def test_home_sensitive_categories(writes, category):
    core, state, calls = writes
    state["accessories"]["lamp-id"]["category"] = category
    result = await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"})
    assert result["error"]["code"] == "secure_required"
    assert "home_set_secure" in result["error"]["message"]
    assert not mutations(calls)


@pytest.mark.parametrize(
    "service", ["lock", "lockManagement", "garageDoor", "securitySystem", "alarm"]
)
async def test_safe_category_cannot_hide_sensitive_service(writes, service):
    core, state, calls = writes
    state["accessories"]["lamp-id"]["services"].append({"type": service, "characteristics": []})
    assert (await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"}))[
        "error"
    ]["code"] == "secure_required"
    assert not mutations(calls)


async def test_home_refuses_names_unknown_categories_and_missing_services(writes):
    core, state, calls = writes
    args = {"accessory_id": "Lamp", "action": "on"}
    assert (await core.call("dot", "home_set", args))["error"]["code"] == "invalid_arguments"
    args["accessory_id"] = "lamp-id"
    row = state["accessories"]["lamp-id"]
    row["category"] = "camera"
    row["services"][0]["type"] = "camera"
    assert (await core.call("dot", "home_set", args))["error"]["code"] == "unsupported_accessory"
    row["category"] = "lightbulb"
    row["services"] = []
    assert (await core.call("dot", "home_set", args))["error"]["code"] == "backend_invalid"
    assert not mutations(calls)


@pytest.mark.parametrize(
    "cap,args",
    [
        ("media_request_movie", {"tmdb_id": 42}),
        ("media_request_show", {"tmdb_id": 42}),
        ("home_set", {"accessory_id": "lamp-id", "action": "on"}),
        ("home_set_secure", {"accessory_id": "lock-id", "action": "toggle"}),
        ("home_run_scene", {"scene_id": "scene-id"}),
        ("home_run_scene_secure", {"scene_id": "scene-id"}),
    ],
)
async def test_write_scope_denial_hidden_and_direct(writes, cap, args):
    core, state, calls = writes
    assert (await core.call("agy", cap, args))["error"]["code"] == "scope_denied"
    assert cap not in [tool["name"] for tool in core.tools("agy")]
    assert not calls


@pytest.mark.parametrize(
    "policy",
    [{}, {"allow_all_safe": False}, {"allow_all_safe": True, "deny_accessories": ["lamp-id"]}],
)
async def test_home_policy_defaults_and_deny(writes, policy):
    core, state, calls = writes
    change_policy(core.config, lambda p: p["allowlists"].update(home=policy))
    assert (await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"}))[
        "error"
    ]["code"] == "resource_denied"
    assert not mutations(calls)


async def test_explicit_safe_allowlist_and_media_revoke(writes):
    core, state, calls = writes
    change_policy(
        core.config, lambda p: p["allowlists"].update(home={"accessories": ["lamp-id"]}, media={})
    )
    assert (await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"}))[
        "status"
    ] == "ok"
    calls.clear()
    assert (await core.call("dot", "media_request_movie", {"tmdb_id": 42}))["error"][
        "code"
    ] == "resource_denied"
    assert not calls


async def approved(core, cap, args):
    result = await core.call("dot", cap, args)
    assert result["status"] == "approval_required"
    assert core.resolve("owner", result["approval_id"], True)["status"] == "approved"
    return {**args, "approval_id": result["approval_id"]}


async def test_secure_requires_allowlist_and_consumes_grant_even_on_denial(writes):
    core, state, calls = writes
    args = await approved(core, "home_set_secure", {"accessory_id": "lock-id", "action": "toggle"})
    assert (await core.call("dot", "home_set_secure", args))["error"]["code"] == "resource_denied"
    change_policy(
        core.config, lambda p: p["allowlists"]["home"].update(secure_accessories=["lock-id"])
    )
    assert (await core.call("dot", "home_set_secure", args))["error"]["code"] == "approval_invalid"
    assert not mutations(calls)


async def test_secure_exact_approval_and_policy_recheck(writes):
    core, state, calls = writes
    change_policy(
        core.config, lambda p: p["allowlists"]["home"].update(secure_accessories=["lock-id"])
    )
    args = await approved(
        core,
        "home_set_secure",
        {"accessory_id": "lock-id", "action": "set", "characteristic": "lockTarget", "value": 0},
    )
    assert (await core.call("dot", "home_set_secure", {**args, "value": 1}))["error"][
        "code"
    ] == "approval_mismatch"
    assert (await core.call("codex", "home_set_secure", args))["error"][
        "code"
    ] == "approval_mismatch"
    assert (await core.call("dot", "home_set_secure", args))["status"] == "ok"
    assert (await core.call("dot", "home_set_secure", args))["error"]["code"] == "approval_invalid"
    assert mutations(calls) == [("PUT", "/api/v1/characteristics/lock-char", {"value": 0})]
    args = await approved(core, "home_set_secure", {"accessory_id": "lock-id", "action": "toggle"})
    change_policy(
        core.config, lambda p: p["allowlists"]["home"].update(deny_accessories=["lock-id"])
    )
    assert (await core.call("dot", "home_set_secure", args))["error"]["code"] == "resource_denied"
    assert len(mutations(calls)) == 1


async def test_home_scene_opaque_and_secure_override(writes):
    core, state, calls = writes
    # Opaque scenes could unlock a door, so allow_all_safe does not cover them.
    denied = await core.call("dot", "home_run_scene", {"scene_id": "scene-id"})
    assert denied["error"]["code"] == "resource_denied"
    assert not mutations(calls)
    change_policy(core.config, lambda p: p["allowlists"]["home"].update(scenes=["scene-id"]))
    result = await core.call("dot", "home_run_scene", {"scene_id": "scene-id"})
    assert result["status"] == "ok" and result["actions_exposed"] is False
    assert "cannot be detected" in result["limitations"][0]
    assert mutations(calls) == [("POST", "/api/v1/scenes/scene-id/run", {})]
    calls.clear()
    change_policy(core.config, lambda p: p["allowlists"]["home"].update(secure_scenes=["scene-id"]))
    assert (await core.call("dot", "home_run_scene", {"scene_id": "scene-id"}))["error"][
        "code"
    ] == "secure_required"
    args = await approved(core, "home_run_scene_secure", {"scene_id": "scene-id"})
    assert (await core.call("dot", "home_run_scene_secure", args))["status"] == "ok"
    assert len(mutations(calls)) == 1


async def test_scene_exposed_sensitive_actions(writes):
    core, state, calls = writes
    state["scenes"][0]["actions"] = [{"accessoryId": "lock-id"}]
    result = await core.call("dot", "home_run_scene", {"scene_id": "scene-id"})
    assert result["error"]["code"] == "secure_required"
    assert "home_run_scene_secure" in result["error"]["message"]
    args = await approved(core, "home_run_scene_secure", {"scene_id": "scene-id"})
    change_policy(
        core.config,
        lambda p: p["allowlists"]["home"].update(
            secure_scenes=["scene-id"], secure_accessories=["lock-id"]
        ),
    )
    assert (await core.call("dot", "home_run_scene_secure", args))["status"] == "ok"
    assert len(mutations(calls)) == 1


async def test_scene_deny_targets_and_unknown_contents(writes):
    core, state, calls = writes
    state["scenes"][0]["actions"] = [{"accessory_id": "lamp-id"}]
    change_policy(
        core.config, lambda p: p["allowlists"]["home"].update(deny_accessories=["lamp-id"])
    )
    assert (await core.call("dot", "home_run_scene", {"scene_id": "scene-id"}))["error"][
        "code"
    ] == "resource_denied"
    state["scenes"][0]["actions"] = [{"unknown": "lamp-id"}]
    assert (await core.call("dot", "home_run_scene", {"scene_id": "scene-id"}))["error"][
        "code"
    ] == "backend_invalid"
    assert not mutations(calls)


@pytest.mark.parametrize("mode", ["missing", "symlink", "public"])
async def test_home_never_falls_back_to_read_token(writes, mode):
    core, state, calls = writes
    path = core.config.config_dir / "control"
    if mode == "public":
        path.chmod(0o644)
    else:
        path.unlink()
        if mode == "symlink":
            path.symlink_to(core.config.config_dir / "read")
    result = await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"})
    assert result["error"]["code"] == "backend_credentials"
    assert not mutations(calls)
    assert (await core.call("dot", "home_get", {"accessory_id": "lamp-id"}))["status"] == "ok"
    assert "fake-control" not in json.dumps(core.audit.tail())


@pytest.mark.parametrize("value", ["true", 1, [], None])
def test_home_policy_flag_strict(config, value):
    from switchboard.errors import CapabilityError
    from switchboard.policy import Policy

    change_policy(config, lambda p: p["allowlists"].update(home={"allow_all_safe": value}))
    with pytest.raises(CapabilityError, match="Policy missing or invalid"):
        Policy(config.config_dir / "policy.yaml").load()


def test_home_control_config(config):
    path = config.config_dir / "config.yaml"
    path.write_text(yaml.safe_dump({"backends": {"home": {"control_token_file": "~/control"}}}))
    assert Config.load().backends["home"]["control_token_file"] == str(Path.home() / "control")
    path.write_text(yaml.safe_dump({"backends": {"media": {"control_token_file": "~/control"}}}))
    with pytest.raises(ValueError):
        Config.load()


@pytest.mark.parametrize(
    "info",
    [
        False,
        [],
        {"status": True},
        {"status": 99},
        {"requests": [{"status": "pending"}]},
        {"requests": "PRIVATE"},
    ],
)
async def test_media_malformed_preflight_fails_closed(writes, info):
    core, state, calls = writes
    state["movie"]["mediaInfo"] = info
    result = await core.call("dot", "media_request_movie", {"tmdb_id": 42})
    assert result["error"]["code"] == "backend_invalid"
    assert not mutations(calls)
    assert "PRIVATE" not in json.dumps(result)


async def test_home_secure_failure_consumes_approval(writes):
    core, state, calls = writes
    change_policy(
        core.config, lambda p: p["allowlists"]["home"].update(secure_accessories=["lock-id"])
    )
    args = await approved(core, "home_set_secure", {"accessory_id": "lock-id", "action": "toggle"})
    state["home_status"] = 503
    result = await core.call("dot", "home_set_secure", args)
    assert result["error"]["code"] == "backend_error"
    assert "PRIVATE" not in json.dumps(result)
    assert (await core.call("dot", "home_set_secure", args))["error"]["code"] == "approval_invalid"
    assert len(mutations(calls)) == 1


async def test_scene_secure_default_and_deny(writes):
    core, state, calls = writes
    args = await approved(core, "home_run_scene_secure", {"scene_id": "scene-id"})
    assert (await core.call("dot", "home_run_scene_secure", args))["error"][
        "code"
    ] == "resource_denied"
    change_policy(
        core.config,
        lambda p: p["allowlists"]["home"].update(
            secure_scenes=["scene-id"], deny_scenes=["scene-id"]
        ),
    )
    args = await approved(core, "home_run_scene_secure", {"scene_id": "scene-id"})
    assert (await core.call("dot", "home_run_scene_secure", args))["error"][
        "code"
    ] == "resource_denied"
    assert (await core.call("dot", "home_run_scene", {"scene_id": "scene-id"}))["error"][
        "code"
    ] == "resource_denied"
    assert not mutations(calls)


async def test_home_camera_cannot_hide_behind_light_service(writes):
    core, state, calls = writes
    state["accessories"]["lamp-id"]["category"] = "camera"
    result = await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"})
    assert result["error"]["code"] == "unsupported_accessory"
    assert not mutations(calls)


async def test_home_fan_active_shortcut(writes):
    core, state, calls = writes
    row = state["accessories"]["lamp-id"]
    row["category"] = row["services"][0]["type"] = "fan"
    row["services"][0]["characteristics"][0]["type"] = "active"
    result = await core.call("dot", "home_set", {"accessory_id": "lamp-id", "action": "on"})
    assert result["status"] == "ok"
    assert mutations(calls) == [("POST", "/api/v1/accessories/lamp-id", {"on": True})]


def test_example_write_scopes_and_secure_defaults():
    policy = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config/policy.example.yaml").read_text()
    )
    for client in set(policy["clients"]) - {"cortex", "deepseek", "dream"}:
        assert {"media:request", "home:control", "home:secure"} <= set(
            policy["clients"][client]["scopes"]
        )
    assert policy["allowlists"]["home"]["allow_all_safe"] is True
    assert policy["allowlists"]["home"]["secure_accessories"] == []
    assert policy["allowlists"]["home"]["secure_scenes"] == []


async def test_media_allow_all_and_deny(writes):
    core, state, calls = writes
    change_policy(
        core.config,
        lambda p: (
            p["allowlists"]
            .setdefault("media", {})
            .update(allow_all=True, movies=[], deny_movies=["13"])
        ),
    )
    assert core.policy.require_allowed("media", "movies", "999") is None
    import pytest

    from switchboard.errors import CapabilityError

    with pytest.raises(CapabilityError):
        core.policy.require_allowed("media", "movies", "13")
    change_policy(core.config, lambda p: p["allowlists"]["media"].update(allow_all=False))
    with pytest.raises(CapabilityError):
        core.policy.require_allowed("media", "movies", "999")
