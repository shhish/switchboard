"""EventKit integration uses private fake Unix sockets, never the installed helper."""

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from switchboard import cli
from switchboard.config import Config
from switchboard.errors import CapabilityError
from switchboard.eventkit import MAX_RESPONSE, EventKit
from switchboard.policy import Policy

from .conftest import change_policy

META = {"timezone": "America/New_York", "timezone_note": "Naive inputs use America/New_York"}
EVENT = {
    "id": "e1",
    "calendar_id": "cal1",
    "title": "Dinner",
    "start": "2026-11-01T18:00:00-05:00",
    "end": "2026-11-01T19:00:00-05:00",
    "all_day": False,
    "private": "NEVER_RETURN",
}
REMINDER = {
    "id": "r1",
    "list_id": "list1",
    "title": "Milk",
    "due": "2026-11-01T12:00:00-05:00",
    "completed": False,
}


@pytest.fixture
async def socket_helper(config):
    # Relative paths avoid Darwin's 104-byte sockaddr_un limit in long worktrees.
    directory = Path(".pytest-tmp") / ("ek-" + uuid4().hex[:8])
    directory.mkdir(mode=0o700, parents=True)
    path = directory / "ek.sock"
    state = {
        "status": {"events": "full_access", "reminders": "full_access", **META},
        "request_access": {"events": "full_access", "reminders": "full_access", **META},
        "events.list": {"events": [EVENT], "truncated": True, **META},
        "events.create": {"event": EVENT, **META},
        "calendars.list": {
            "calendars": [{"id": "cal1", "title": "Personal", "writable": True}],
            **META,
        },
        "reminder_lists.list": {
            "lists": [{"id": "list1", "title": "Personal", "writable": True}],
            **META,
        },
        "reminders.list": {"reminders": [REMINDER], **META},
        "reminders.add": {"reminder": REMINDER, **META},
        "reminders.complete": {"reminder": {**REMINDER, "completed": True}, **META},
    }
    calls, tasks = [], set()
    pause = asyncio.Event()

    async def connection(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            message = json.loads(await reader.readline())
            calls.append(message)
            value = state[message["method"]]
            if callable(value):
                value = value(message["args"])
            if value == "wait":
                await pause.wait()
                return
            if isinstance(value, bytes):
                writer.write(value)
            else:
                # Emulate the helper's current-list check before the completion save.
                if (
                    message["method"] == "reminders.complete"
                    and REMINDER["list_id"] not in message["args"]["list_ids"]
                ):
                    value = {
                        "ok": False,
                        "error": {"code": "resource_denied", "message": "PRIVATE"},
                    }
                writer.write(
                    json.dumps(value if "ok" in value else {"ok": True, "result": value}).encode()
                    + b"\n"
                )
            with contextlib.suppress(ConnectionError, TimeoutError):
                async with asyncio.timeout(1):
                    await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, TimeoutError):
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            writer.transport.abort()
            tasks.discard(task)

    server = await asyncio.start_unix_server(connection, path)
    path.chmod(0o600)
    config.backends["eventkit"].update(enabled=True, socket=str(path))
    try:
        yield state, calls, path
    finally:
        server.close()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
        await server.wait_closed()
        shutil.rmtree(directory)


def allow(config, calendars=None, lists=None, **defaults):
    change_policy(
        config,
        lambda p: p["allowlists"].update(
            {
                "calendar": {
                    "calendars": calendars or [],
                    "default_calendar": defaults.get("calendar", ""),
                },
                "reminders": {"lists": lists or [], "default_list": defaults.get("list", "")},
            }
        ),
    )


async def test_native_reads_and_filters(core, socket_helper, fake_backends):
    _, calls, _ = socket_helper
    result = await core.call("dot", "calendar_upcoming", {"days": 31, "calendar_ids": ["cal1"]})
    assert result["events"][0]["id"] == "e1"
    assert result["truncated"] is True
    assert result["event_ids_available"] is True
    assert result["timezone"] == "America/New_York"
    request = calls[-1]
    assert request["args"]["calendar_ids"] == ["cal1"]
    assert request["args"]["limit"] == 200
    assert request["args"]["start"].endswith("+00:00")
    result = await core.call("dot", "reminders_due", {"days": 31, "list_ids": ["list1"]})
    assert result["reminders"][0]["due"].endswith("-05:00")
    assert result["due_dates_available"] is True
    assert calls[-1]["args"]["list_ids"] == ["list1"]
    assert "due_before" in calls[-1]["args"]
    assert not fake_backends[1]
    assert not any(c["method"] == "request_access" for c in calls)
    assert "NEVER_RETURN" not in json.dumps(result)


@pytest.mark.parametrize(
    "entity,name,ids",
    [
        ("events", "calendar_upcoming", "calendar_ids"),
        ("reminders", "reminders_due", "list_ids"),
    ],
)
@pytest.mark.parametrize("authorization", ["not_determined", "denied", "restricted", "write_only"])
async def test_unavailable_authorization_falls_back(
    core, socket_helper, entity, name, ids, authorization
):
    state, calls, _ = socket_helper
    state["status"][entity] = authorization
    result = await core.call("dot", name, {"days": 31})
    assert "FrameServer" in result["coverage"]
    assert len(calls) == 1 and calls[0]["method"] == "status"
    filtered = await core.call("dot", name, {ids: ["id"]})
    assert filtered["error"]["code"] == "backend_unavailable"


async def test_helper_missing_and_revoked_during_read(core, socket_helper):
    state, calls, path = socket_helper
    state["events.list"] = {"ok": False, "error": {"code": "unauthorized", "message": "PRIVATE"}}
    assert "FrameServer" in (await core.call("dot", "calendar_upcoming", {}))["coverage"]
    path.unlink()
    assert "FrameServer" in (await core.call("dot", "reminders_due", {}))["coverage"]
    result = await core.call("dot", "calendar_list_calendars", {})
    assert result["error"]["code"] == "backend_down"
    assert all(c["method"] != "request_access" for c in calls)


async def test_list_discovery_and_doctor_do_not_prompt(core, socket_helper):
    state, calls, _ = socket_helper
    assert (await core.call("dot", "calendar_list_calendars", {}))["calendars"][0]["id"] == "cal1"
    assert (await core.call("dot", "reminders_list_lists", {}))["lists"][0]["id"] == "list1"
    result = await core.call("dot", "ops_switchboard_status", {})
    assert result["backends"]["eventkit"]["authorization"] == {
        "events": "full_access",
        "reminders": "full_access",
    }
    state["status"]["reminders"] = "denied"
    assert (await core.backends.eventkit.probe())["status"] == "degraded"
    assert all(c["method"] != "request_access" for c in calls)


CREATE = {"calendar_id": "cal1", "title": "Dinner", "start": EVENT["start"], "end": EVENT["end"]}


@pytest.mark.parametrize(
    "name,args",
    [
        ("calendar_create_event", CREATE),
        ("reminders_add", {"list_id": "list1", "title": "Milk"}),
        ("reminders_complete", {"reminder_id": "r1"}),
    ],
)
async def test_writes_fail_closed_and_scope_checked(core, config, socket_helper, name, args):
    _, calls, _ = socket_helper
    assert (await core.call("dot", name, args))["error"]["code"] == "resource_denied"
    assert calls == []
    allow(config, calendars=["cal1"], lists=["list1"])
    assert (await core.call("agy", name, args))["error"]["code"] == "scope_denied"
    assert calls == []
    assert name not in {tool["name"] for tool in core.tools("agy")}
    result = await core.call("dot", name, args)
    assert result["status"] == "ok"
    assert "NEVER_RETURN" not in json.dumps(result)
    if name == "reminders.complete":
        assert calls[-1]["args"] == {"reminder_id": "r1", "list_ids": ["list1"]}
        assert result["reminder"]["completed"] is True
    # Live policy reload must revoke the next write; it cannot reuse discovery rights.
    allow(config)
    before = len(calls)
    assert (await core.call("dot", name, args))["error"]["code"] == "resource_denied"
    assert len(calls) == before
    records = core.audit.tail()
    assert any(r["capability"] == name and r["outcome"] == "error" for r in records)


async def test_defaults_only_apply_to_empty_lists(core, config, socket_helper):
    _, calls, _ = socket_helper
    allow(config, calendar="cal1", list="list1")
    assert (await core.call("dot", "calendar_create_event", CREATE))["status"] == "ok"
    assert (await core.call("dot", "reminders_add", {"list_id": "list1", "title": "Milk"}))[
        "status"
    ] == "ok"
    allow(config, calendars=["other"], lists=["other"], calendar="cal1", list="list1")
    assert (await core.call("dot", "calendar_create_event", CREATE))["error"][
        "code"
    ] == "resource_denied"
    result = await core.call("dot", "reminders_complete", {"reminder_id": "r1"})
    assert result["error"]["code"] == "eventkit_resource_denied"
    assert calls[-1]["args"]["list_ids"] == ["other"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("calendar_upcoming", {"calendar_ids": []}),
        ("reminders_due", {"list_ids": [True]}),
        ("calendar_create_event", {**CREATE, "title": "x" * 201}),
        ("calendar_create_event", {**CREATE, "notes": "x" * 2001}),
        ("calendar_create_event", {**CREATE, "alarms_minutes": [-1]}),
        ("calendar_create_event", {**CREATE, "alarms_minutes": [True]}),
        ("reminders_add", {"list_id": "list1", "title": "Milk", "priority": 10}),
        ("reminders_complete", {"reminder_id": "r1", "list_ids": ["override"]}),
    ],
)
async def test_write_schema_and_internal_allowlist_not_caller_controlled(
    core, socket_helper, name, args
):
    assert (await core.call("dot", name, args))["error"]["code"] == "invalid_arguments"
    assert socket_helper[1] == []


@pytest.mark.parametrize("action", ["status", "request-access"])
async def test_owner_cli_authentication_and_no_capability_route(
    config, core, socket_helper, action
):
    _, calls, _ = socket_helper
    for client in ("dot", "codex"):
        with pytest.raises(CapabilityError, match="Local owner"):
            await cli.execute(
                cli.parser().parse_args(["--client", client, "eventkit", action]), config
            )
    assert not calls
    result = await cli.execute(cli.parser().parse_args(["eventkit", action]), config)
    assert result["status"] == "ok"
    assert calls[-1]["method"] == ("request_access" if action == "request-access" else "status")
    assert (await core.call("owner", "eventkit.request_access", {}))["error"][
        "code"
    ] == "unknown_capability"
    config.token_dir.joinpath("owner.token").chmod(0o644)
    with pytest.raises(ValueError):
        await cli.execute(cli.parser().parse_args(["eventkit", action]), config)


@pytest.mark.parametrize(
    "body",
    [
        b"{}\n",
        b'{"ok":true,"result":[]}\n',
        b'{"ok":false,"error":[]}\n',
        b'{"ok":false,"error":{"code":"PRIVATE","message":"SECRET"}}\n',
        b'{"ok":true,"result":{"x":NaN}}\n',
        b'{"ok":true,"result":{}}',
        b'{"ok":true,"result":{},"ok":false}\n',
        b"x" * (MAX_RESPONSE + 1) + b"\n",
    ],
)
async def test_bad_socket_response_is_sanitized(config, socket_helper, body):
    state, _, _ = socket_helper
    state["status"] = body
    with pytest.raises(CapabilityError) as error:
        await EventKit(config).call("status")
    assert error.value.code == "backend_invalid"
    assert "PRIVATE" not in str(error.value) and "SECRET" not in str(error.value)


async def test_timeout_and_unsafe_socket_modes(config, socket_helper):
    state, _, path = socket_helper
    state["status"] = "wait"
    with pytest.raises(CapabilityError) as error:
        await EventKit(config, timeout=0.03).call("status")
    assert error.value.code == "backend_down"
    path.chmod(0o666)
    with pytest.raises(CapabilityError) as error:
        await EventKit(config).call("status")
    assert error.value.code == "backend_invalid"
    path.chmod(0o600)
    path.parent.chmod(0o755)
    with pytest.raises(CapabilityError) as error:
        await EventKit(config).call("status")
    assert error.value.code == "backend_invalid"
    path.parent.chmod(0o700)
    link = path.parent / "link.sock"
    link.symlink_to(path.name)
    config.backends["eventkit"]["socket"] = str(link)
    with pytest.raises(CapabilityError) as error:
        await EventKit(config).call("status")
    assert error.value.code == "backend_invalid"


async def test_no_automatic_write_retry_or_backend_error_leak(core, config, socket_helper):
    state, calls, _ = socket_helper
    allow(config, calendars=["cal1"])
    state["events.create"] = {"ok": False, "error": {"code": "unauthorized", "message": "PRIVATE"}}
    result = await core.call("dot", "calendar_create_event", CREATE)
    assert result["error"]["code"] == "eventkit_unauthorized"
    assert "PRIVATE" not in json.dumps(result)
    assert len(calls) == 1
    with pytest.raises(CapabilityError):
        await core.backends.eventkit.call("request_access")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "settings",
    [
        {"socket": "relative.sock"},
        {"socket": 12},
        {"socket": ""},
        {"socket": "/" + "x" * 104},
        {"url": "http://127.0.0.1:8095"},
        {"enabled": "true"},
    ],
)
def test_bad_eventkit_config(config, settings):
    import yaml

    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"backends": {"eventkit": settings}})
    )
    with pytest.raises(ValueError):
        Config.load()


@pytest.mark.parametrize(
    "domain,key,value",
    [
        ("calendar", "default_calendar", []),
        ("reminders", "default_list", 1),
        ("calendar", "calendars", "cal1"),
        ("reminders", "lists", [False]),
    ],
)
def test_bad_allowlists_fail_closed(config, domain, key, value):
    change_policy(config, lambda p: p["allowlists"].update({domain: {key: value}}))
    with pytest.raises(CapabilityError):
        Policy(config.config_dir / "policy.yaml").load()


async def test_request_access_denial_is_degraded(config, socket_helper):
    state, _, _ = socket_helper
    state["request_access"]["events"] = "denied"
    result = await cli.execute(cli.parser().parse_args(["eventkit", "request-access"]), config)
    assert result["status"] == "degraded"
    assert result["events"] == "denied"


async def test_long_ids_round_trip_verbatim(core, socket_helper):
    state, _, _ = socket_helper
    identifier = "calendar-" + "x" * 500
    state["calendars.list"]["calendars"][0]["id"] = identifier
    result = await core.call("dot", "calendar_list_calendars", {})
    assert result["calendars"][0]["id"] == identifier


@pytest.mark.parametrize(
    "value", [[], {"events": []}, {"events": "PRIVATE", "reminders": "full_access"}]
)
async def test_invalid_status_is_not_disclosed(config, socket_helper, value):
    socket_helper[0]["status"] = {"ok": True, "result": value}
    result = await EventKit(config).probe()
    assert result["status"] == "down"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "method,key,value,capability",
    [
        ("events.list", "events", "bad", "calendar_upcoming"),
        ("events.list", "events", [{}], "calendar_upcoming"),
        ("reminders.list", "reminders", [{}] * 201, "reminders_due"),
        ("calendars.list", "calendars", [{"id": True}], "calendar_list_calendars"),
    ],
)
async def test_invalid_resources_fail_structurally(
    core, socket_helper, method, key, value, capability
):
    socket_helper[0][method][key] = value
    assert (await core.call("dot", capability, {}))["error"]["code"] == "backend_invalid"


@pytest.mark.parametrize(
    "capability,args",
    [
        ("calendar_list_calendars", {}),
        ("reminders_list_lists", {}),
        ("calendar_create_event", CREATE),
        ("reminders_add", {"list_id": "list1", "title": "Milk"}),
        ("reminders_complete", {"reminder_id": "r1"}),
    ],
)
async def test_disabled_helper_never_falls_back_for_new_tools(core, config, capability, args):
    allow(config, calendars=["cal1"], lists=["list1"])
    assert (await core.call("dot", capability, args))["error"]["code"] == "backend_disabled"


@pytest.mark.parametrize(
    "reply,expected",
    [
        ({"http_status": 200, "latency_ms": 12.5}, "up"),
        ({"http_status": 503, "latency_ms": 12.5}, "degraded"),
        ({"error": "refused"}, "down"),
        ({"error": "timeout"}, "down"),
        ({"error": "tls"}, "down"),
        ({"error": "unreachable", "local_network_denied": True}, "unknown"),
        ({"error": "local_network_denied"}, "unknown"),
        ({"http_status": "PRIVATE", "latency_ms": -1}, "unknown"),
        ({"ok": False, "error": {"code": "unauthorized", "message": "PRIVATE"}}, "unknown"),
    ],
)
async def test_lan_probes_use_helper_and_output_schema(core, socket_helper, reply, expected):
    from switchboard.config import validate_probes

    state, calls, _ = socket_helper
    state["net.probe"] = reply
    core.config.probes = validate_probes([{"name": "lan", "url": "http://172.20.0.2/health"}])
    result = await core.call("dot", "ops_health", {})
    assert result["services"][0]["status"] == expected
    if expected == "unknown":
        assert result["summary"]["unknown"] == 1
        assert result["services"][0]["reason"] == "local network permission needed"
    assert calls == [{"method": "net.probe", "args": {"url": "http://172.20.0.2/health"}}]
    assert "PRIVATE" not in json.dumps(result)
    assert "172.20" not in json.dumps(result)


async def test_unreachable_helper_is_unknown_and_loopback_stays_direct(core, socket_helper):
    from aiohttp import web

    from switchboard.config import validate_probes

    from .conftest import running_app

    _, calls, path = socket_helper
    path.unlink()
    async with running_app(web.Application()) as url:
        core.config.probes = validate_probes(
            [
                {"name": "local", "url": url},
                {"name": "lan", "url": "http://172.20.0.1/"},
            ]
        )
        result = await core.call("dot", "ops_health", {})
    by_name = {row["name"]: row for row in result["services"]}
    assert by_name["local"]["status"] == "up"
    assert by_name["lan"]["status"] == "unknown"
    assert not calls


async def test_net_request_access_is_local_owner_only(config, socket_helper):
    from switchboard.config import validate_probes

    state, calls, _ = socket_helper
    state["net.request_access"] = {"http_status": 200, "latency_ms": 1}
    config.probes = validate_probes(
        [
            {"name": "local", "url": "http://127.0.0.1/"},
            {"name": "first", "url": "http://172.20.0.1/"},
            {"name": "second", "url": "http://172.20.0.2/"},
        ]
    )
    with pytest.raises(CapabilityError, match="Local owner"):
        await cli.execute(
            cli.parser().parse_args(["--client", "dot", "net", "request-access"]), config
        )
    assert not calls
    result = await cli.execute(cli.parser().parse_args(["net", "request-access"]), config)
    assert result["status"] == "ok"
    assert calls == [{"method": "net.request_access", "args": {"url": "http://172.20.0.1/"}}]
    from switchboard.domains import build_registry

    assert not any("request_access" in name for name in build_registry().capabilities)


@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize(
    "tool,args",
    [
        ("reminders_complete", {"reminder_id": "r1"}),
        ("reminders_add", {"list_id": "list1", "title": "Milk"}),
        ("calendar_create_event", CREATE),
    ],
)
async def test_native_writes_subtract_denials_from_one_snapshot(
    core, config, socket_helper, monkeypatch, default, tool, args
):
    _, calls, _ = socket_helper
    allow(
        config,
        calendars=[] if default else ["cal1"],
        lists=[] if default else ["list1"],
        calendar="cal1",
        list="list1",
    )
    change_policy(
        config,
        lambda p: (
            p["allowlists"]["calendar"].update(deny_calendars=["cal1"]),
            p["allowlists"]["reminders"].update(deny_lists=["list1"]),
        ),
    )
    snapshot = core.policy.load()
    loads = []

    def load():
        loads.append(True)
        return snapshot

    monkeypatch.setattr(core.policy, "load", load)
    result = await core.call("dot", tool, args)
    assert result["error"]["code"] == "resource_denied"
    assert not calls
    # Core checks scope twice; the handler reads one resource snapshot.
    assert len(loads) == 3
    snapshot["allowlists"]["calendar"]["deny_calendars"] = []
    snapshot["allowlists"]["reminders"]["deny_lists"] = []
    loads.clear()
    assert (await core.call("dot", tool, args))["status"] == "ok"
    assert len(loads) == 3  # Successful adds/create must not reload resource policy either.


async def test_completion_forwards_only_effective_lists(core, config, socket_helper):
    _, calls, _ = socket_helper
    allow(config, lists=["list1", "denied"])
    change_policy(config, lambda p: p["allowlists"]["reminders"].update(deny_lists=["denied"]))
    assert (await core.call("dot", "reminders_complete", {"reminder_id": "r1"}))["status"] == "ok"
    assert calls[-1]["args"]["list_ids"] == ["list1"]


async def test_native_execution_deadline_is_a_structured_timeout(core, socket_helper):
    responses, _, _ = socket_helper
    responses["reminders.list"] = {"ok": False, "error": {"code": "timeout", "message": "PRIVATE"}}
    result = await core.call("dot", "reminders_due", {})
    assert result["error"]["code"] == "eventkit_timeout"
    assert result["outcome"] == "unavailable"
    assert result["error"]["retryable"] is True
    assert "PRIVATE" not in json.dumps(result)


async def test_remote_hosts_go_through_the_helper(core, socket_helper, tmp_path):
    """launchd Python can't open LAN sockets; real remote calls must use the helper."""
    from switchboard import hostcmd
    from switchboard.config import validate_remote_hosts, validate_services
    from switchboard.services import Services

    state, calls, _ = socket_helper
    files = {}
    for key in ("key_file", "known_hosts_file"):
        path = tmp_path / key
        path.write_text("fake")
        path.chmod(0o600)
        files[key] = str(path)
    core.config.remote_hosts = validate_remote_hosts(
        {"remotehost": {"address": "172.20.0.2", "user": "pi", **files}}
    )
    core.config.services = validate_services(
        {"homebridge": {"runtime": {"remotehost": "homebridge"}}}, [], core.config.remote_hosts
    )
    core.services = Services(core)  # the default runner: hostcmd.run, i.e. not faked
    assert core.services.runner is hostcmd.run
    state["remote.ssh"] = {"exit_code": 0, "stdout": json.dumps({"ok": True, "version": "2"})}
    from switchboard import remote

    assert (await remote.call(core, "remotehost", "version"))["version"] == "2"
    assert calls[-1] == {
        "method": "remote.ssh",
        "args": {"host": "172.20.0.2", "user": "pi", "request": "version", **files},
    }
    state["remote.ssh"] = {"exit_code": 255, "stdout": ""}
    with pytest.raises(CapabilityError) as error:
        await remote.call(core, "remotehost", "snapshot")
    assert error.value.code == "backend_down"
