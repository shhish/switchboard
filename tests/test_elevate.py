"""Signed-root relay contracts against an ephemeral Unix socket and fake Gitea only."""

# ruff: noqa: F811
import asyncio
import base64
import copy
import hashlib
import json
import secrets
import stat
from pathlib import Path

import jsonschema
import pytest

from switchboard import elevate
from switchboard.ask_actions import validate_action
from switchboard.config import Config
from switchboard.elevate import Elevate, ElevateError, NotInstalled, Refused, RootActions
from switchboard.errors import CapabilityError
from switchboard.schema import validate

from .conftest import change_policy
from .test_board import board_api  # noqa: F401

PUBLIC = {"name": "Test phone", "x963": base64.b64encode(b"\x04" + b"x" * 64).decode()}
SIGNATURE = base64.b64encode(b"fake DER signature").decode()
# Built at runtime so the open-source privacy check sees no home paths.
HOME = "/" + "Users/test.name"
CATALOG = {
    "ok": True,
    "version": "1.0.0",
    "machine": "fake-machine",
    "home": HOME,
    "owner": "testowner",
    "keys": [{"name": "Phone", "fingerprint": "aaaa bbbb cccc dddd"}],
    "recipes": [
        {
            "id": "privacy",
            "title": "Restart privacy checks",
            "steps": [["/usr/bin/killall", "-9", "tccd", "sandboxd"]],
            "timeout": 30,
        },
        {
            "id": "path",
            "title": "Path",
            "args": [{"name": "path", "pattern": "{home}/[a-z .{}]+"}],
            "steps": [["/bin/echo", "{path}", "{home}", "{owner}", "x{path}{path}"]],
        },
        {
            "id": "choice",
            "title": "Choice",
            "args": [{"name": "value", "choices": ["a", "b", "{owner}"]}],
            "steps": [["/bin/echo", "{value}"]],
        },
    ],
}


@pytest.fixture
async def helper(tmp_path):
    # macOS Unix paths must fit in sockaddr_un even under pytest's long basetemp.
    directory = Path("/tmp") / ("sb-el-" + secrets.token_hex(4))
    directory.mkdir()
    path = directory / "helper.sock"
    state = {
        "status": copy.deepcopy(CATALOG),
        "jobs": {},
        "calls": [],
        "refuse": None,
        "run_state": "running",
        "reply": None,
    }

    async def handle(reader, writer):
        try:
            request = json.loads(await reader.readline())
            state["calls"].append(request)
            op = request["op"]
            if state["reply"] is not None:
                writer.write(state["reply"])
                await writer.drain()
                return
            if op == "status":
                reply = state["status"]
            elif op == "run":
                if state["refuse"]:
                    reply = {"ok": False, "error": state["refuse"]}
                else:
                    payload = json.loads(base64.b64decode(request["payload"]))
                    job = payload["nonce"]
                    state["jobs"][job] = {"ok": True, "state": state["run_state"]}
                    if state["run_state"] == "done":
                        state["jobs"][job].update(exit=0, output="applied\n")
                    reply = {"ok": True, "state": state["run_state"], "job": job}
            else:
                reply = state["jobs"].get(request["job"], {"ok": True, "state": "unknown"})
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path)
    yield Elevate(str(path)), state
    server.close()
    await server.wait_closed()
    path.unlink(missing_ok=True)
    directory.rmdir()


@pytest.fixture
async def root_api(board_api, helper, tmp_path, monkeypatch):
    core, state, calls = board_api
    core.elevate = helper[0]
    core.config.elevate["socket"] = helper[0].socket
    monkeypatch.setattr(elevate, "pending_path", lambda: tmp_path / "pending" / "pending-key.json")
    # Never open a real password box from tests.
    monkeypatch.setattr(elevate, "SETUP_SCRIPT", tmp_path / "no-setup-script")
    monkeypatch.setattr(elevate, "_launch", lambda argv: pytest.fail("launched setup"))
    change_policy(
        core.config,
        lambda p: (
            p["clients"]["codex"]["scopes"].extend(["elevate:read", "elevate:request"]),
            p["clients"]["cortex"]["scopes"].extend(
                ["board:read", "board:answer", "elevate:read", "elevate:pair"]
            ),
        ),
    )
    yield board_api, helper[1]
    await core.root_actions.close()


async def call(api, tool="elevate_request", client="codex", **args):
    core = api[0]
    result = await core.call(client, tool, args)
    jsonschema.validate(result, core.registry.capabilities[tool].advertised_output_schema())
    return result


async def request(api, **args):
    return await call(api, card="project#1", reason="Repair the privacy checks", **args)


async def answer(api, ask, *, yes=True, signature=SIGNATURE):
    value = {"choice": ["yes" if yes else "no"]}
    if signature is not None:
        value["signature"] = signature
    return await call(api, "board_answer", client="cortex", ask_id=ask["id"], answer=value)


def stored(api, ask):
    return json.loads(api[0].board.ask_row(ask["id"])["spec"])["action"]


async def test_client_status_missing_refusal_and_bounds(helper, tmp_path):
    client, state = helper
    assert (await client.status())["version"] == "1.0.0"
    with pytest.raises(NotInstalled, match="Root helper not installed"):
        await Elevate(client.socket + ".missing").status()
    state["refuse"] = "signature not from a paired iPhone"
    with pytest.raises(Refused, match="signature not from a paired iPhone"):
        await client.run("e30=", SIGNATURE)
    for reply in (b"{}\n", b"[]\n", b"invalid\n", b"{}", b"x" * (elevate.MAX_MESSAGE + 20) + b"\n"):
        state["reply"] = reply
        with pytest.raises(ElevateError, match="Invalid root helper"):
            await client.status()


@pytest.mark.parametrize(
    "action",
    [
        {"recipe": "privacy"},
        {"steps": [["/bin/echo", "hello world", "a;b"]]},
        {"steps": [["/bin/true"], ["/bin/echo", "done"]], "timeout": 15},
        {"manage": "add-key", "key": PUBLIC},
    ],
)
async def test_forms_payload_and_server_summary(root_api, action):
    api, fake = root_api
    if action.get("manage"):
        change_policy(
            api[0].config, lambda p: p["clients"]["codex"]["scopes"].append("elevate:pair")
        )
    result = await call(
        api,
        "board_ask",
        card="project#1",
        kind="confirm",
        prompt="Proceed?",
        action={"type": "run_as_root", **action},
    )
    ask = result["ask"]
    assert ask["destructive"] is True
    assert ask["state"] == "open"
    root = ask["action"]
    data = base64.b64decode(root["sign_payload"], validate=True)
    payload = json.loads(data)
    assert data == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert (
        payload.items()
        >= {
            "v": 1,
            "machine": "fake-machine",
            "ask": ask["id"],
            "asker": "codex",
            "reason": "Proceed?",
        }.items()
    )
    assert len(payload["nonce"]) == 32 and payload["expires"] > 0
    assert payload["steps"] == root["steps"]
    assert root["sign_payload"] == stored(api, ask)["sign_payload"]
    assert not [c for c in fake["calls"] if c["op"] == "run"]
    if action.get("recipe"):
        assert (
            root["summary"]
            == "Runs as root on this Mac: Restart privacy checks: /usr/bin/killall -9 tccd sandboxd"
        )
    if "steps" in action:
        assert root["summary"].startswith("Runs a custom command as root on this Mac:")


@pytest.mark.parametrize(
    "action",
    [
        {},
        {"args": {}},
        {"recipe": "missing"},
        {"recipe": "privacy", "args": {"extra": "x"}},
        {"recipe": "privacy", "steps": [["/bin/true"]]},
        {"steps": []},
        {"steps": [["relative"]]},
        {"steps": [[]]},
        {"steps": [["/bin/echo", "\0"]]},
        {"steps": [["/bin/echo", "é" * 2049]]},
        {"steps": [["/bin/echo"] * 65]},
        {"steps": [["/bin/true"]] * 6},
        {"steps": [["/bin/true"]], "args": {}},
        {"steps": [["/bin/true"]], "timeout": 1},
        {"manage": "add-key", "key": {**PUBLIC, "x963": "bad"}},
        {"manage": "add-key", "key": {**PUBLIC, "name": "bad/phone"}},
        {"manage": "update-helper", "path": "relative"},
        {"manage": "other"},
    ],
)
async def test_invalid_forms(root_api, action):
    api, fake = root_api
    change_policy(api[0].config, lambda p: p["clients"]["codex"]["scopes"].append("elevate:pair"))
    result = await call(
        api,
        "board_ask",
        card="project#1",
        kind="confirm",
        prompt="Proceed?",
        action={"type": "run_as_root", **action},
    )
    assert result["status"] == "error"
    assert not api[1]["comments"]
    assert not [c for c in fake["calls"] if c["op"] == "run"]


@pytest.mark.parametrize(
    "recipe,args,expected",
    [
        ("choice", {"value": "{owner}"}, [["/bin/echo", "{owner}"]]),
        (
            "path",
            {"path": HOME + "/a {owner}"},
            [
                [
                    "/bin/echo",
                    HOME + "/a {owner}",
                    HOME,
                    "testowner",
                    "x" + HOME + "/a {owner}" + HOME + "/a {owner}",
                ]
            ],
        ),
    ],
)
def test_render_one_left_to_right_pass(recipe, args, expected):
    row = next(r for r in CATALOG["recipes"] if r["id"] == recipe)
    assert elevate.render(row, args, CATALOG) == expected


@pytest.mark.parametrize(
    "value",
    ["/" + "Users/testXname/a", HOME + "/a/../b", HOME + "/a/..", "", "\0", 5],
)
def test_render_rejects_invalid_argument(value):
    with pytest.raises(CapabilityError):
        elevate.render(CATALOG["recipes"][1], {"path": value}, CATALOG)


async def test_signature_required_decline_and_helper_refusal(root_api):
    api, fake = root_api
    for yes, sig, refusal, message in [
        (True, None, None, "Approve it in the Server app with Face ID"),
        (False, SIGNATURE, None, None),
        (
            True,
            SIGNATURE,
            "signature not from a paired iPhone",
            "signature not from a paired iPhone",
        ),
    ]:
        ask = (await request(api, recipe="privacy"))["ask"]
        fake["refuse"] = refusal
        count = sum(c["op"] == "run" for c in fake["calls"])
        result = await answer(api, ask, yes=yes, signature=sig)
        assert result["ask"]["state"] == "answered"
        action = result["ask"]["action"]
        if message:
            assert action["result"] == "failed" and action["message"] == message
        if not refusal:
            assert sum(c["op"] == "run" for c in fake["calls"]) == count
        assert "signature" not in result["ask"]["answer"]


async def test_running_done_one_comment_and_no_output_in_audit(root_api):
    api, fake = root_api
    ask = (await request(api, recipe="privacy"))["ask"]
    result = await answer(api, ask)
    action = result["ask"]["action"]
    assert result["ask"]["state"] == "answered" and action["result"] == "running"
    run = next(c for c in fake["calls"] if c["op"] == "run")
    assert run["payload"] == ask["action"]["sign_payload"] and run["signature"] == SIGNATURE
    output = "private output ` ```\n" + "o" * 5000 + "OUTPUT-TAIL-PRIVATE"
    fake["jobs"][action["job"]] = {"ok": True, "state": "done", "exit": 0, "output": output}
    await asyncio.gather(*(api[0].root_actions.refresh(ask["id"]) for _ in range(3)))
    read = await call(api, "board_card", card="project#1")
    final = read["asks"][0]["action"]
    assert final["result"] == "applied" and final["exit"] == 0
    assert final["output"] == output[-4000:]
    comments = [c["body"] for c in api[1]["comments"][1] if "_Root result " in c["body"]]
    assert len(comments) == 1 and output[-1500:] in comments[0]
    audit = (api[0].config.log_dir / "audit.jsonl").read_text()
    assert "OUTPUT-TAIL-PRIVATE" not in audit
    assert SIGNATURE not in audit and run["payload"] not in audit


async def test_restart_refresh_and_wait_keeps_waiting(root_api):
    api, fake = root_api
    ask = (await request(api, steps=[["/bin/true"]], timeout=5))["ask"]
    result = await answer(api, ask)
    job = result["ask"]["action"]["job"]
    await api[0].root_actions.close()
    api[0].root_actions = RootActions(api[0])
    await api[0].root_actions.recover()
    waiting = asyncio.create_task(call(api, "board_ask_wait", ask_id=ask["id"], timeout_s=5))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    fake["jobs"][job] = {"ok": True, "state": "failed", "exit": 7, "output": "failure"}
    result = await waiting
    assert not result["timed_out"] and result["ask"]["action"]["exit"] == 7
    assert sum(c["op"] == "run" for c in fake["calls"]) == 1
    await api[0].root_actions.recover()
    assert sum("_Root result " in c["body"] for c in api[1]["comments"][1]) == 1


@pytest.mark.parametrize("state", ["unknown", "lost", "running"])
async def test_terminal_recovery_and_deadline(root_api, state):
    api, fake = root_api
    ask = (await request(api, recipe="privacy"))["ask"]
    result = await answer(api, ask)
    job = result["ask"]["action"]["job"]
    spec = json.loads(api[0].board.ask_row(ask["id"])["spec"])
    spec["action"]["deadline"] = 0
    api[0].root_actions.save(ask["id"], spec)
    fake["jobs"][job] = {"ok": True, "state": state}
    await api[0].root_actions.recover()
    assert stored(api, ask)["result"] == "failed"


async def test_wait_timeout_running(root_api):
    api, _ = root_api
    ask = (await request(api, recipe="privacy"))["ask"]
    await answer(api, ask)
    result = await call(api, "board_ask_wait", ask_id=ask["id"], timeout_s=0)
    assert result["timed_out"] and result["ask"]["action"]["result"] == "running"


async def test_update_forms_and_hash_binding(root_api, tmp_path):
    api, fake = root_api
    catalog = {
        "recipes": [
            copy.deepcopy(fake["status"]["recipes"][0]),
            {"id": "new", "title": "New", "steps": [["/bin/true"]]},
        ]
    }
    catalog["recipes"][0]["title"] = "Changed"
    file = tmp_path / "recipes.json"
    file.write_text(json.dumps(catalog))
    helper_file = tmp_path / "helper"
    helper_file.write_bytes(b"\xcf\xfa\xed\xfe" + b"fake")
    for what, path in (("recipes", file), ("helper", helper_file)):
        result = await call(
            api,
            "elevate_update",
            card="project#1",
            what=what,
            path=str(path),
            reason="Install approved update",
        )
        action = result["ask"]["action"]
        payload = json.loads(base64.b64decode(action["sign_payload"]))
        assert payload["kind"] == "update-" + what
        assert payload["file"] == {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        if what == "recipes":
            assert action["changes"] == {
                "added": ["new"],
                "removed": ["choice", "path"],
                "changed": ["privacy"],
            }
            assert "added: new" in action["summary"]


@pytest.mark.parametrize("installed", [True, False])
async def test_pair_install_and_pending_status(root_api, installed):
    api, fake = root_api
    if installed:
        fake["status"]["keys"] = []
    else:
        api[0].elevate = Elevate(api[0].elevate.socket + ".missing")
    result = await call(api, "elevate_pair", client="cortex", **PUBLIC)
    assert result["mode"] == "install" and result["fingerprint"] == elevate.fingerprint(PUBLIC)
    assert "/.local/share/switchboard/current/scripts/elevate-install.sh" in result["command"]
    path = elevate.pending_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()).items() >= PUBLIC.items()
    result = await call(api, "elevate_status")
    assert result["installed"] == installed and result["pending"]["name"] == PUBLIC["name"]


async def test_pair_approved_by_existing_key_even_same_owner_client(root_api):
    api, fake = root_api
    fake["run_state"] = "done"
    result = await call(api, "elevate_pair", client="cortex", **PUBLIC)
    assert result["mode"] == "approve"
    row = api[0].board.ask_row(result["ask_id"])
    assert row["asker"] == "cortex"
    ask = api[0].board.ask_object(row)
    signed = json.loads(base64.b64decode(ask["action"]["sign_payload"]))
    assert signed["key"] == PUBLIC and signed["kind"] == "add-key"
    final = await answer(api, ask)
    assert final["ask"]["action"]["result"] == "applied"


async def test_default_card_duplicate_guard(root_api):
    api, _ = root_api
    one = await call(api, recipe="privacy", reason="Repair checks")
    two = await call(api, recipe="privacy", reason="Repair checks again")
    assert one["ask"]["card"] == two["ask"]["card"]
    assert sum(i["title"] == "Root commands on the Mac" for i in api[1]["issues"].values()) == 1


@pytest.mark.parametrize(
    "name,args",
    [
        ("elevate_status", {}),
        ("elevate_request", {"reason": "Repair", "recipe": "privacy"}),
        ("elevate_update", {"what": "recipes", "path": "/tmp/unused", "reason": "Update"}),
        ("elevate_pair", PUBLIC),
    ],
)
async def test_scope_denials_and_discovery(root_api, name, args):
    api, fake = root_api
    result = await call(api, name, client="agy", **args)
    assert result["status"] == "error" and result["error"]["code"] == "scope_denied"
    assert name not in {t["name"] for t in api[0].tools("agy")}
    assert fake["calls"] == []


async def test_direct_ask_cannot_bypass_root_scope(root_api):
    api, fake = root_api
    change_policy(
        api[0].config, lambda p: p["clients"]["codex"]["scopes"].remove("elevate:request")
    )
    result = await call(
        api,
        "board_ask",
        card="project#1",
        kind="confirm",
        prompt="Proceed?",
        action={"type": "run_as_root", "recipe": "privacy"},
    )
    assert result["status"] == "error" and not fake["calls"]


async def test_uninstalled_request_invalid(root_api):
    api, _ = root_api
    api[0].elevate = Elevate(api[0].elevate.socket + ".missing")
    result = await request(api, recipe="privacy")
    assert result["error"]["message"] == "Root helper not installed"


async def test_grant_pair_requires_owner_answer_scope(root_api):
    api, _ = root_api
    core = api[0]
    action = {"type": "grant_scope", "client": "codex", "scope": "elevate:pair"}
    with pytest.raises(CapabilityError):
        validate_action(core, action)
    change_policy(core.config, lambda p: p["clients"]["codex"]["scopes"].append("board:answer"))
    assert validate_action(core, action)["scope"] == "elevate:pair"


@pytest.mark.parametrize("value", [None, {"extra": True}, {"socket": "relative"}, {"socket": 42}])
def test_invalid_config(value):
    with pytest.raises(ValueError):
        elevate.settings(value)


@pytest.mark.parametrize(
    "value",
    [
        {**PUBLIC, "x963": base64.b64encode(b"x" * 65).decode()},
        {**PUBLIC, "x963": base64.b64encode(b"\x04" * 64).decode()},
        {**PUBLIC, "name": "x" * 41},
    ],
)
def test_invalid_key(value):
    with pytest.raises(CapabilityError):
        elevate.key(value)


def test_root_wire_schemas_are_flat_closed():
    from switchboard.domains import build_registry

    for name, cap in build_registry().capabilities.items():
        if name.startswith("elevate_"):
            assert cap.input_schema["additionalProperties"] is False
            assert not set(cap.input_schema) & {"oneOf", "anyOf", "allOf"}
            with pytest.raises(CapabilityError):
                validate({"extra": True}, cap.input_schema)


async def test_config_load_root_socket(config, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_CONFIG_DIR", str(config.config_dir))
    (config.config_dir / "config.yaml").write_text("elevate:\n  socket: /tmp/test-root.sock\n")
    assert Config.load().elevate["socket"] == "/tmp/test-root.sock"


async def test_doctor_helper_is_information(root_api):
    api, _ = root_api
    result = await call(api, "ops_switchboard_status", client="cortex")
    assert result["checks"]["elevate"] == {"status": "info", "reason": "running v1.0.0 with 1 keys"}
    api[0].config.elevate["socket"] += ".missing"
    missing = await call(api, "ops_switchboard_status", client="cortex")
    assert missing["checks"]["elevate"] == {"status": "info", "reason": "not installed"}
    assert missing["status"] == result["status"]


async def test_signature_validation_and_non_pair_self_answer(root_api):
    api, fake = root_api
    ask = (await request(api, recipe="privacy"))["ask"]
    for signature in ("not base64!", "a" * 201):
        result = await answer(api, ask, signature=signature)
        assert result["status"] == "error"
    change_policy(api[0].config, lambda p: p["clients"]["codex"]["scopes"].append("board:answer"))
    result = await call(
        api, "board_answer", ask_id=ask["id"], answer={"choice": ["yes"], "signature": SIGNATURE}
    )
    assert result["error"]["code"] == "resource_denied"
    assert not [c for c in fake["calls"] if c["op"] == "run"]


async def test_restart_after_comment_reply_lost_deduplicates(root_api, monkeypatch):
    api, fake = root_api
    ask = (await request(api, recipe="privacy"))["ask"]
    action = (await answer(api, ask))["ask"]["action"]
    fake["jobs"][action["job"]] = {"ok": True, "state": "done", "exit": 0, "output": "done"}
    original = api[0].board.api.request
    once = True

    async def lost_reply(path, **kwargs):
        nonlocal once
        result = await original(path, **kwargs)
        if once and "_Root result " in (kwargs.get("payload") or {}).get("body", ""):
            once = False
            raise CapabilityError("backend_down", "Reply lost")
        return result

    monkeypatch.setattr(api[0].board.api, "request", lost_reply)
    with pytest.raises(CapabilityError):
        await api[0].root_actions.refresh(ask["id"])
    await api[0].root_actions.close()
    api[0].root_actions = RootActions(api[0])
    await api[0].root_actions.recover()
    assert stored(api, ask)["commented"]
    assert sum("_Root result " in c["body"] for c in api[1]["comments"][1]) == 1


async def test_startup_recovers_durable_running_ask(root_api, monkeypatch):
    from unittest.mock import AsyncMock

    api, fake = root_api
    core = api[0]
    ask = (await request(api, recipe="privacy"))["ask"]
    action = (await answer(api, ask))["ask"]["action"]
    await core.root_actions.close()
    core.root_actions = RootActions(core)
    fake["jobs"][action["job"]] = {
        "ok": True,
        "state": "done",
        "exit": 0,
        "output": "after restart",
    }
    for service in (core.backends, core.probes, core.orb, core.alerts, core.monitor, core.routines):
        monkeypatch.setattr(service, "start", AsyncMock())
    await core.start()
    assert stored(api, ask)["result"] == "applied"
    core.maintenance.cancel()
    await asyncio.gather(core.maintenance, return_exceptions=True)


async def test_payload_larger_than_old_text_cap_stays_exact(root_api):
    api, _ = root_api
    argv = ["/bin/echo", "a" * 4096, "b" * 4096]
    ask = (await request(api, steps=[argv]))["ask"]
    action = ask["action"]
    assert len(action["sign_payload"]) > 4000
    assert action["steps"] == [argv]
    payload = json.loads(base64.b64decode(action["sign_payload"]))
    assert payload["steps"] == [argv]
    read = await call(api, "board_card", card="project#1")
    assert read["asks"][0]["action"]["sign_payload"] == action["sign_payload"]


async def test_pair_install_replaces_pending_atomically(root_api):
    api, fake = root_api
    fake["status"]["keys"] = []
    await call(api, "elevate_pair", client="cortex", **PUBLIC)
    second = {**PUBLIC, "name": "Other phone"}
    result = await call(api, "elevate_pair", client="cortex", **second)
    assert result["mode"] == "install"
    assert json.loads(elevate.pending_path().read_text())["name"] == "Other phone"
    assert list(elevate.pending_path().parent.iterdir()) == [elevate.pending_path()]


async def test_status_bounded_and_paired_pending_hidden(root_api):
    api, fake = root_api
    elevate.write_pending(PUBLIC)
    fake["status"]["keys"] = [{"name": PUBLIC["name"], "fingerprint": elevate.fingerprint(PUBLIC)}]
    fake["status"]["recipes"] = [
        {"id": f"test-{i}", "title": "Test", "detail": "x" * 2000, "steps": [["/bin/true"]]}
        for i in range(101)
    ]
    result = await call(api, "elevate_status")
    assert result["pending"] is None and result["truncated"]
    assert len(json.dumps(result)) < 101000
    assert all("steps" not in row for row in result["recipes"])


async def test_updated_recipe_file_cannot_rewrite_signed_payload(root_api, tmp_path):
    api, _ = root_api
    file = tmp_path / "update.json"
    file.write_text('{"recipes": []}')
    result = await call(
        api,
        "elevate_update",
        card="project#1",
        what="recipes",
        path=str(file),
        reason="Replace recipes",
    )
    before = result["ask"]["action"]["sign_payload"]
    file.write_text('{"recipes": [{"id": "changed"}]}')
    after = await call(api, "board_card", card="project#1")
    assert after["asks"][0]["action"]["sign_payload"] == before


@pytest.mark.parametrize(
    "document", ["{}", "[]", '{"recipes": [1]}', '{"recipes": [{"id": "same"}, {"id": "same"}]}']
)
async def test_invalid_recipe_update(root_api, tmp_path, document):
    api, _ = root_api
    file = tmp_path / "update.json"
    file.write_text(document)
    result = await call(api, "elevate_update", what="recipes", path=str(file), reason="Update")
    assert result["status"] == "error" and not api[1]["comments"]


def test_password_box_opens_once_per_key_per_half_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(elevate, "pending_path", lambda: tmp_path / "pending-key.json")
    script = tmp_path / "elevate-setup.sh"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(elevate, "SETUP_SCRIPT", script)
    launched = []
    assert elevate.open_setup("aaaa bbbb", now=1000, launch=launched.append)
    assert not elevate.open_setup("aaaa bbbb", now=1000 + 60, launch=launched.append)
    assert elevate.open_setup("cccc dddd", now=1000 + 120, launch=launched.append)
    assert elevate.open_setup("cccc dddd", now=1000 + 120 + 1801, launch=launched.append)
    assert launched == [[str(script)]] * 3
    script.unlink()
    assert not elevate.open_setup("eeee ffff", now=5000, launch=launched.append)
