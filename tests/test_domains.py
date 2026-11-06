import json

import pytest
from aiohttp import web

from switchboard.backends import Backends, OrbApprovalNotifier
from switchboard.protocol import MODERN_VERSION, VERSION_KEY

from .conftest import running_app


async def test_health_without_probes_does_not_read_aggregator(core, fake_backends):
    result = await core.call("dot", "ops_health", {})
    assert result["status"] == "ok"
    assert result["services"] == []
    assert result["summary"] == {"up": 0, "degraded": 0, "down": 0}
    assert fake_backends[1] == []
    assert "PRIVATE" not in json.dumps(result)


async def test_library_summary(core):
    result = await core.call("dot", "media_library_status", {})
    assert result["status"] == "ok"
    assert result["movies"]["total"] == 12
    assert result["degraded_services"] == ["readarr"]
    assert result["downloads"] == {"active": 1}
    assert "PRIVATE" not in json.dumps(result)


async def test_downloads(core, fake_backends):
    result = await core.call("dot", "media_downloads", {})
    assert result["active"][0]["id"] == "dl1"
    assert result["history"][0]["id"] == "dl2"
    assert "RAW" not in json.dumps(result)
    assert any(call["query"].get("limit") == "10" for call in fake_backends[1])


async def test_search_filter_limit_tmdb_and_availability(core, fake_backends):
    result = await core.call("dot", "media_search", {"query": "DUNE"})
    assert len(result["library"]) == 10
    assert all("dune" in row["title"].lower() for row in result["library"])
    assert result["overseerr"][0]["tmdb_id"] == 900
    assert result["overseerr"][0]["availability"] == "available"
    assert "PRIVATE" not in json.dumps(result)
    result = await core.call("dot", "media_search", {"query": "Dune", "kind": "show"})
    assert len(result["library"]) == 1
    assert result["library"][0]["tmdb_id"] == 901
    assert result["overseerr"][0]["availability"] == "partially_available"
    assert all(row["kind"] == "show" for row in result["overseerr"])
    core.config.backends["overseerr"]["enabled"] = False
    result = await core.call("dot", "media_search", {"query": "Arrival", "kind": "movie"})
    assert result["status"] == "ok"
    assert result["warnings"]
    assert len(result["library"]) == 1


async def test_home_summary_and_state(core):
    result = await core.call("dot", "home_list", {})
    assert result["homes"][0]["id"] == "h1"
    assert result["homes"][0]["accessories"][0]["id"] == "a1"
    assert result["homes"][0]["scenes"][0]["id"] == "s1"
    result = await core.call("dot", "home_get", {"accessory_id": "a1"})
    assert result["accessory"]["state"] == {"on": True, "brightness": 50}
    assert "RAW" not in json.dumps(result)
    result = await core.call("dot", "home_get", {"name": "Office/Lamp"})
    assert result["accessory"]["id"] == "a1"
    result = await core.call("dot", "home_get", {"name": "ambiguous"})
    assert result["error"]["code"] == "backend_ambiguous"


async def test_calendar_reminders_truthful_windows(core):
    result = await core.call("dot", "calendar_upcoming", {"days": 1})
    assert len(result["events"]) == 1
    assert result["events"][0]["title"] == "Meeting"
    assert result["event_ids_available"] is False
    result = await core.call("dot", "calendar_upcoming", {"days": 14})
    assert result["requested_days"] == 14
    assert "tomorrow" in result["coverage"]
    result = await core.call("dot", "reminders_due", {"days": 7})
    assert [row["id"] for row in result["reminders"]] == ["rem1"]
    assert result["due_dates_available"] is False
    assert "NEVER" not in json.dumps(result)
    assert "Private Things" not in json.dumps(result)


async def test_doctor_no_secrets_and_wrong_modes(core, config):
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["backends"]["home"]["status"] == "ok"
    assert result["tokens"]["dot"]["status"] == "ok"
    assert result["backends"]["orb"]["status"] == "disabled"
    assert result["checks"]["policy"]["status"] == "ok"
    assert "fake-home-read" not in json.dumps(result)
    (config.token_dir / "dot.token").chmod(0o644)
    result = await core.call("owner", "ops_switchboard_status", {})
    assert result["tokens"]["dot"]["status"] == "degraded"
    (config.config_dir / "policy.yaml").write_text("broken: yes")
    from switchboard.doctor import diagnose

    result = await diagnose(config, core.backends, core.policy)
    assert result["checks"]["policy"]["status"] == "down"


@pytest.mark.parametrize(
    "name,args",
    [
        ("home_get", {}),
        ("home_get", {"name": "Lamp", "accessory_id": "a1"}),
        ("calendar_upcoming", {"days": 32}),
        ("calendar_upcoming", {"days": True}),
        ("calendar_upcoming", {"days": 0}),
        ("reminders_due", {"days": 32}),
        ("media_search", {"query": ""}),
        ("media_search", {"query": " "}),
        ("media_search", {"query": "a", "kind": "book"}),
        ("home_list", {"extra": "ignored?"}),
    ],
)
async def test_schema_validation(core, name, args):
    assert (await core.call("dot", name, args))["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    "name,args,backend",
    [
        ("media_library_status", {}, "media"),
        ("media_downloads", {}, "media"),
        ("media_search", {"query": "Dune"}, "media"),
        ("home_list", {}, "home"),
        ("home_get", {"accessory_id": "a1"}, "home"),
        ("calendar_upcoming", {}, "frame"),
        ("reminders_due", {}, "frame"),
    ],
)
async def test_backend_down_structured(core, name, args, backend):
    # Port reserved then released on an ephemeral test server, never a live service.
    app = web.Application()
    async with running_app(app) as dead_url:
        pass
    core.config.backends[backend]["url"] = dead_url
    core.config.backends["overseerr"]["enabled"] = False
    result = await core.call("dot", name, args)
    assert result["status"] == "error"
    assert result["error"]["code"] == "backend_down"


async def test_doctor_backend_down(core):
    app = web.Application()
    async with running_app(app) as dead_url:
        pass
    core.config.backends["media"]["url"] = dead_url
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["status"] == "degraded"
    assert result["backends"]["media"]["status"] == "down"


async def test_backend_http_error_redacts(core, fake_backends):
    fake_backends[0]["/library/summary"] = 502
    result = await core.call("dot", "media_library_status", {})
    assert result["error"]["code"] == "backend_error"
    assert "PRIVATE" not in json.dumps(result)


async def test_orb_notifier_posts_typed_card_only(config):
    received = []
    token = config.config_dir / "orb.secret"
    token.write_text("fake-orb-token")
    token.chmod(0o600)

    async def mcp(request):
        assert request.headers["Authorization"] == "Bearer fake-orb-token"
        assert request.headers["Mcp-Name"] == "orb_ask"
        value = await request.json()
        received.append(value)
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": value["id"],
                "result": {"resultType": "complete", "structuredContent": {"status": "queued"}},
            }
        )

    app = web.Application()
    app.router.add_post("/mcp", mcp)
    async with running_app(app) as url:
        config.backends["orb"].update(url=url + "/mcp", enabled=True, token_file=str(token))
        backend = Backends(config)
        await backend.start()
        try:
            await OrbApprovalNotifier(backend).notify(
                {"approval_id": "apr_test", "summary": "Approve test"}
            )
        finally:
            await backend.close()
    assert received[0]["params"]["name"] == "orb_ask"
    assert received[0]["params"]["_meta"][VERSION_KEY] == MODERN_VERSION
    assert received[0]["params"]["arguments"]["options"][0]["id"] == "approve"


async def test_search_multi_word_query_is_percent_encoded(core):
    result = await core.call("owner", "media_search", {"query": "Masters of the Air: Part 2"})
    assert result["status"] == "ok"
    assert not any("overseerr" in w for w in result.get("warnings", []))
