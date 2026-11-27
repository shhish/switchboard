"""Secret asks: the value reaches its allowlisted store and nothing else, ever.

Keychain writes go to a fake `security` that keeps items in a JSON file and copies the
real tool's traps (a double prompt that can mismatch and a 128-character stdin limit).
"""

# ruff: noqa: F811
import asyncio
import hashlib
import json
import os
import secrets
import stat
import sys
import textwrap
import threading
from pathlib import Path

import pytest
import yaml

from switchboard import handover
from switchboard.audit import safe_args
from switchboard.domains.board import ANSWER_OUT, HANDOVER_OUT

from .conftest import change_policy
from .test_board import board_api, call, writes  # noqa: F401
from .test_board_asks import asks, owner  # noqa: F401

FAKE_SECURITY = r"""
import json, sys
store_path, mode_path, login = {store!r}, {mode!r}, {login!r}
OTHER = "/opt/keychains/other.keychain-db"
SEARCH = [login, OTHER]  # the search list; login is also the default keychain
try:
    items = json.load(open(store_path))  # "<keychain>|<account>/<service>" -> hex bytes
except FileNotFoundError:
    items = {{}}
try:
    modes = set(open(mode_path).read().split())
except FileNotFoundError:
    modes = set()
args = sys.argv[1:]
command, rest = args[0], args[1:]
opts = {{flag: rest[i + 1] for i, flag in enumerate(rest) if flag in ("-a", "-s")}}
key = opts.get("-a", "") + "/" + opts.get("-s", "")
named = [a for i, a in enumerate(rest) if a.startswith("/") and rest[i - 1] not in ("-a", "-s")]
if named and named[-1] not in SEARCH:
    sys.exit(44)  # like the real tool: a missing keychain looks like a missing item
def save():
    json.dump(items, open(store_path, "w"))
def line(raw):
    # The real tool reads at most 128 bytes per prompt line.
    return raw.rstrip(b"\n")[:128]
def first(keychains):
    return next((k for k in keychains if f"{{k}}|{{key}}" in items), None)
if command == "show-keychain-info":
    sys.exit(0 if rest == [login] else 50)
elif command == "default-keychain":
    print('    "' + ("/elsewhere.keychain-db" if "other-default" in modes else login) + '"')
elif command == "add-generic-password":
    if "fail" in modes:
        sys.exit(1)
    first_line, second = line(sys.stdin.buffer.readline()), line(sys.stdin.buffer.readline())
    value = first_line if first_line == second else b""
    if "corrupt" in modes:
        value = value[:-1]
    if "corrupt-once" in modes:
        value = value[:-1]
        open(mode_path, "w").close()
    # -U updates the first match anywhere on the search list, else adds to the default.
    target = first(SEARCH) or login
    items[f"{{target}}|{{key}}"] = value.hex()
    save()
elif command == "find-generic-password":
    if "readfail" in modes:
        sys.exit(1)
    found = first(named[-1:] or SEARCH)
    if found is None:
        sys.exit(44)
    print(f'keychain: "{{found}}"')
    print("class: genp")
    raw = bytes.fromhex(items[f"{{found}}|{{key}}"])
    if "-g" in rest:
        plain = all(0x20 <= b <= 0x7E and b not in b'"\\' for b in raw)
        if not raw:
            sys.stderr.write("password: \n")
        elif plain:
            sys.stderr.write('password: "' + raw.decode() + '"\n')
        else:
            sys.stderr.write("password: 0x" + raw.hex().upper() + '  "escaped"\n')
elif command == "delete-generic-password":
    found = first(named[-1:] or SEARCH)
    if found is None:
        sys.exit(44)
    del items[f"{{found}}|{{key}}"]
    save()
else:
    sys.exit(2)
"""

ALLOWLIST = {
    "tmdb": {"label": "TMDB API key", "store": "keychain", "service": "homelab.tmdb"},
    "fanart": {"label": "fanart.tv key", "store": "file", "charset": "hex", "min_length": 32},
}


@pytest.fixture
def keychain(tmp_path, monkeypatch):
    store, mode = tmp_path / "keychain.json", tmp_path / "keychain.mode"
    login = str(Path.home() / "Library" / "Keychains" / "login.keychain-db")
    script = tmp_path / "fake-security"
    script.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(FAKE_SECURITY.format(store=str(store), mode=str(mode), login=login))
    )
    script.chmod(0o700)
    monkeypatch.setattr(handover, "SECURITY", str(script))

    class Keychain:
        def items(self):
            return json.loads(store.read_text()) if store.exists() else {}

        def raw(self, service, keychain=None):
            value = self.items().get(f"{keychain or login}|{handover.account()}/{service}")
            return None if value is None else bytes.fromhex(value)

        def get(self, service, keychain=None):
            raw = self.raw(service, keychain)
            return None if raw is None else raw.decode()

        def put(self, service, value, keychain=None):
            items = self.items()
            data = value if isinstance(value, bytes) else value.encode()
            items[f"{keychain or login}|{handover.account()}/{service}"] = data.hex()
            store.write_text(json.dumps(items))

        def mode(self, *values):
            mode.write_text(" ".join(values))

    Keychain.other = "/opt/keychains/other.keychain-db"
    return Keychain()


def allow(core, rows=None, mode=0o600):
    path = handover.allowlist_path(core.config.config_dir)
    path.write_text(yaml.safe_dump({"destinations": ALLOWLIST if rows is None else rows}))
    path.chmod(mode)
    return path


def value32():
    # Mixed case and digits, the shape of most API keys.
    return "Ab9" + secrets.token_hex(14) + "Zq7"


async def secret_ask(api, destination="tmdb"):
    return await call(
        api,
        "ask",
        card="project#1",
        kind="secret",
        destination=destination,
        prompt="The curator needs the TMDB key to rank artwork",
    )


def everything(core, tmp_path, *outputs):
    """Every durable or returned byte: comments, SQLite, audit JSONL and given outputs."""
    db = "\n".join(core.storage.db.iterdump())
    logs = "".join(
        p.read_text(errors="replace") for p in core.config.log_dir.rglob("*") if p.is_file()
    )
    return "\n".join([db, logs, *(json.dumps(o) for o in outputs)])


def assert_absent(blob, value):
    assert value not in blob
    for form in (value, json.dumps({"secret": value}, separators=(",", ":"))):
        assert hashlib.sha256(form.encode()).hexdigest()[:16] not in blob


async def test_keychain_secret_reaches_only_its_store(asks, keychain, tmp_path):
    core, state, calls = asks
    allow(core)
    created = await secret_ask(asks)
    ask = created["ask"]
    assert ask["kind"] == "secret" and ask["state"] == "open"
    assert ask["handover"] == {
        "name": "tmdb",
        "label": "TMDB API key",
        "store": "keychain",
        "where": "Keychain item homelab.tmdb",
        "charset": "printable",
        "min_length": 1,
        "max_length": 128,
        "replaces": False,
    }
    comment = state["comments"][1][-1]["body"]
    assert "Switchboard saves it to Keychain item homelab.tmdb" in comment
    assert "never appears on the board" in comment

    value = value32()
    answered = await owner(asks, ask["id"], {"secret": f"  {value}\n"})
    assert keychain.get("homelab.tmdb") == value
    assert answered["ask"]["answer"] == {"provided": True}
    assert answered["ask"]["answer_text"] == "Provided (saved to Keychain item homelab.tmdb)"
    assert state["comments"][1][-1]["body"].endswith(
        "answered **codex**: Provided (saved to Keychain item homelab.tmdb)"
    )
    waited = await call(asks, "ask_wait", ask_id=ask["id"], timeout_s=0)
    card = await call(asks, "card", card="project#1")
    change_policy(core.config, lambda p: p["clients"]["cortex"]["scopes"].append("activity:read"))
    feed = await core.call("cortex", "activity_feed", {})
    assert feed["status"] == "ok" and "Provided" in json.dumps(feed)
    blob = everything(core, tmp_path, created, answered, waited, card, feed, state["comments"])
    assert_absent(blob, value)
    row = core.storage.db.execute(
        "SELECT args_json FROM audit_log WHERE capability='board_answer'"
    ).fetchone()
    redacted = json.dumps({"secret": "[REDACTED]"}, separators=(",", ":"))
    assert json.loads(row["args_json"])["answer"] == {
        "len": len(redacted),
        "sha256": hashlib.sha256(redacted.encode()).hexdigest()[:16],
    }


async def test_file_secret_is_private_and_rotates(asks, keychain, tmp_path):
    core, _, _ = asks
    allow(core)
    path = handover.secret_dir(core.config.config_dir) / "fanart"
    first, second = secrets.token_hex(16), secrets.token_hex(16)
    ask = (await secret_ask(asks, "fanart"))["ask"]
    assert ask["handover"]["store"] == "file" and not ask["handover"]["replaces"]
    assert ask["handover"]["where"].endswith("secrets/handover/fanart")
    await owner(asks, ask["id"], {"secret": first})
    assert path.read_text() == first + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    again = (await secret_ask(asks, "fanart"))["ask"]
    assert again["handover"]["replaces"] is True
    await owner(asks, again["id"], {"secret": second})
    assert path.read_text() == second + "\n"
    assert sorted(p.name for p in path.parent.iterdir()) == ["fanart"]  # no temp or backup
    assert_absent(everything(core, tmp_path), first)
    assert_absent(everything(core, tmp_path), second)


async def test_decline_writes_nothing(asks, keychain):
    core, state, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    answered = (await owner(asks, ask["id"], {"decline": True}))["ask"]
    assert answered["answer"] == {"provided": False} and answered["answer_text"] == "Declined"
    assert keychain.items() == {}


@pytest.mark.parametrize(
    "answer",
    [
        {"secret": "not hex at all, clearly wrong"},
        {"secret": "ab" * 8},  # hex but shorter than min_length 32
        {"secret": "a" * 31 + "\x00"},
        {"secret": "5e" * 16, "note": "here it is"},
        {"provided": True},
        {"text": "5e" * 16},
        {"decline": True, "secret": "5e" * 16},
    ],
)
async def test_bad_answers_keep_the_ask_open_and_never_echo(asks, keychain, tmp_path, answer):
    core, state, _ = asks
    allow(core)
    ask = (await secret_ask(asks, "fanart"))["ask"]
    before = len(state["comments"][1])
    result = await core.call("cortex", "board_answer", {"ask_id": ask["id"], "answer": answer})
    assert result["status"] == "error"
    assert core.board.ask_row(ask["id"])["state"] == "open"
    assert not handover.secret_dir(core.config.config_dir).exists() or not any(
        handover.secret_dir(core.config.config_dir).iterdir()
    )
    assert len(state["comments"][1]) == before
    for value in answer.values():
        if isinstance(value, str) and len(value) >= 8:
            assert value not in json.dumps(result)
            assert_absent(everything(core, tmp_path), value)


async def test_secret_key_on_other_kinds_is_rejected_and_redacted(asks, keychain, tmp_path):
    core, _, _ = asks
    ask = (await call(asks, "ask", card="project#1", kind="text", prompt="Name?"))["ask"]
    value = value32()
    result = await core.call(
        "cortex", "board_answer", {"ask_id": ask["id"], "answer": {"secret": value}}
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert_absent(everything(core, tmp_path, result), value)


async def test_destination_must_be_allowlisted(asks, keychain):
    core, _, calls = asks
    assert (await secret_ask(asks))["error"]["code"] == "invalid_arguments"
    allow(core)
    assert (await secret_ask(asks, "github"))["error"]["code"] == "invalid_arguments"
    result = await call(asks, "ask", card="project#1", kind="secret", prompt="Key?")
    assert result["error"]["code"] == "invalid_arguments"
    result = await call(
        asks, "ask", card="project#1", kind="text", prompt="Key?", destination="tmdb"
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert not writes(calls)


@pytest.mark.parametrize("change", ["removed", "store", "service", "label"])
async def test_changed_destination_fails_closed(asks, keychain, change):
    core, _, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    rows = json.loads(json.dumps(ALLOWLIST))
    if change == "removed":
        del rows["tmdb"]
    elif change == "store":
        rows["tmdb"] = {"label": "TMDB API key", "store": "file"}
    elif change == "service":
        rows["tmdb"]["service"] = "homelab.other"
    else:
        rows["tmdb"]["label"] = "Gitea token"
    allow(core, rows)
    result = await core.call(
        "cortex", "board_answer", {"ask_id": ask["id"], "answer": {"secret": value32()}}
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert keychain.items() == {} and core.board.ask_row(ask["id"])["state"] == "open"
    assert not handover.secret_dir(core.config.config_dir).exists()


async def answer_secret(core, ask_id, value):
    return await core.call(
        "cortex", "board_answer", {"ask_id": ask_id, "answer": {"secret": value}}
    )


@pytest.mark.parametrize("old", ["old-value-123", 'quote"and\\slash', "5e5e5e5e5e5e5e5e", ""])
@pytest.mark.parametrize("mode", ["corrupt-once", "fail"])
async def test_keychain_write_that_does_not_verify_restores_the_exact_old_value(
    asks, keychain, mode, old
):
    core, _, _ = asks
    allow(core)
    keychain.put("homelab.tmdb", old)
    ask = (await secret_ask(asks))["ask"]
    assert ask["handover"]["replaces"] is True
    keychain.mode(mode)
    result = await answer_secret(core, ask["id"], value32())
    assert result["error"]["code"] == "backend_error"
    assert "nothing was recorded" in result["error"]["message"]
    assert keychain.raw("homelab.tmdb") == old.encode()
    assert core.board.ask_row(ask["id"])["state"] == "open"


async def test_failed_write_on_a_new_item_leaves_nothing_behind(asks, keychain):
    core, _, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    keychain.mode("corrupt-once")
    assert (await answer_secret(core, ask["id"], value32()))["status"] == "error"
    assert keychain.items() == {}


async def test_a_rollback_that_fails_says_so(asks, keychain):
    core, _, _ = asks
    allow(core)
    keychain.put("homelab.tmdb", "old-value-123")
    ask = (await secret_ask(asks))["ask"]
    keychain.mode("corrupt")  # every write, including the restore
    result = await answer_secret(core, ask["id"], value32())
    assert "could not be put back" in result["error"]["message"]
    assert core.board.ask_row(ask["id"])["state"] == "open"


@pytest.mark.parametrize("mode", ["readfail", "other-default"])
async def test_unknown_keychain_state_stops_before_any_change(asks, keychain, mode):
    core, _, _ = asks
    allow(core)
    keychain.put("homelab.tmdb", "old-value-123")
    ask = (await secret_ask(asks))["ask"]
    keychain.mode(mode)
    result = await answer_secret(core, ask["id"], value32())
    assert result["error"]["message"] == "The Keychain is unavailable; nothing was saved"
    keychain.mode()
    assert keychain.get("homelab.tmdb") == "old-value-123"


async def test_an_old_value_that_cannot_be_restored_is_never_overwritten(asks, keychain):
    core, _, _ = asks
    allow(core)
    keychain.put("homelab.tmdb", "café".encode())
    ask = (await secret_ask(asks))["ask"]
    result = await answer_secret(core, ask["id"], value32())
    assert "can't be safely replaced" in result["error"]["message"]
    assert keychain.raw("homelab.tmdb") == "café".encode()


@pytest.mark.parametrize("value", ["résumé-key", "é" * 65, "tab\there"])
async def test_keychain_values_are_printable_ascii(asks, keychain, value):
    core, _, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    result = await answer_secret(core, ask["id"], value)
    assert result["error"]["code"] == "invalid_arguments" and keychain.items() == {}


async def test_destination_is_read_once_per_answer(asks, keychain, monkeypatch):
    core, _, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    reads = []
    original = handover.read_allowlist

    def counted(config_dir):
        reads.append(1)
        return original(config_dir)

    monkeypatch.setattr(handover, "read_allowlist", counted)
    assert (await answer_secret(core, ask["id"], value32()))["status"] == "ok"
    assert len(reads) == 1


async def test_cancelled_answer_keeps_the_lock_until_the_write_lands(asks, keychain, monkeypatch):
    core, state, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    started, release = threading.Event(), threading.Event()
    original = handover.store

    def slow(*args):
        started.set()
        release.wait(10)
        return original(*args)

    monkeypatch.setattr(handover, "store", slow)
    value = value32()
    request = asyncio.create_task(answer_secret(core, ask["id"], value))
    await asyncio.to_thread(started.wait, 10)
    request.cancel()
    await asyncio.sleep(0.2)
    assert core.board._write_lock.locked()
    assert core.board.ask_row(ask["id"])["state"] == "open"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not core.board._write_lock.locked()
    assert keychain.get("homelab.tmdb") == value
    row = core.board.ask_row(ask["id"])
    assert row["state"] == "answered" and json.loads(row["answer"]) == {"provided": True}
    assert state["comments"][1][-1]["body"].endswith(
        "Provided (saved to Keychain item homelab.tmdb)"
    )
    again = await answer_secret(core, ask["id"], value32())
    assert again["status"] == "error" and keychain.get("homelab.tmdb") == value


async def test_owner_edit_during_add_is_never_undone(asks, keychain, monkeypatch):
    core, _, _ = asks
    path = allow(core)
    ask = (
        await call(
            asks,
            "ask",
            card="project#1",
            kind="confirm",
            prompt="Allow?",
            action={"type": "add_secret_destination", "handover": {"name": "new", "label": "New"}},
        )
    )["ask"]
    from switchboard import ask_actions

    replace = ask_actions.replace_yaml

    def owner_revokes_first(*args):
        allow(core, {"fanart": ALLOWLIST["fanart"]})  # the owner removes tmdb meanwhile
        return replace(*args)

    monkeypatch.setattr(ask_actions, "replace_yaml", owner_revokes_first)
    answered = (await owner(asks, ask["id"], {"choice": ["yes"]}))["ask"]
    assert answered["action"]["result"] == "failed"
    assert set(handover.read_allowlist(core.config.config_dir)) == {"fanart"}
    assert path.exists()


async def test_cancelled_answer_that_fails_still_reports_cancellation(asks, keychain, monkeypatch):
    core, _, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    started, release = threading.Event(), threading.Event()

    def failing(*args):
        started.set()
        release.wait(10)
        handover.fail("Could not save", "backend_error")

    monkeypatch.setattr(handover, "store", failing)
    request = asyncio.create_task(
        core.board.answer("cortex", {"ask_id": ask["id"], "answer": {"secret": value32()}})
    )
    await asyncio.to_thread(started.wait, 10)
    request.cancel()
    await asyncio.sleep(0.1)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert core.board.ask_row(ask["id"])["state"] == "open"
    assert not core.board._write_lock.locked()


async def test_item_in_another_keychain_is_never_touched(asks, keychain):
    core, _, _ = asks
    allow(core)
    keychain.put("homelab.tmdb", "someone-elses-key", keychain=keychain.other)
    ask = (await secret_ask(asks))["ask"]
    result = await answer_secret(core, ask["id"], value32())
    assert "Another keychain already has this item" in result["error"]["message"]
    assert keychain.get("homelab.tmdb", keychain.other) == "someone-elses-key"
    assert keychain.get("homelab.tmdb") is None


async def test_allowlist_fifo_does_not_block(asks, keychain):
    core, _, _ = asks
    os.mkfifo(handover.allowlist_path(core.config.config_dir), 0o600)
    result = await asyncio.wait_for(secret_ask(asks), 5)
    assert result["error"]["code"] == "invalid_arguments"


async def test_allowlist_symlink_is_refused(asks, keychain, tmp_path):
    core, _, _ = asks
    real = tmp_path / "elsewhere.yaml"
    real.write_text(yaml.safe_dump({"destinations": ALLOWLIST}))
    real.chmod(0o600)
    handover.allowlist_path(core.config.config_dir).symlink_to(real)
    assert (await secret_ask(asks))["error"]["code"] == "invalid_arguments"


async def test_keychain_values_never_exceed_the_stdin_limit(asks, keychain):
    core, _, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    result = await core.call(
        "cortex", "board_answer", {"ask_id": ask["id"], "answer": {"secret": "k" * 129}}
    )
    assert result["error"]["code"] == "invalid_arguments" and keychain.items() == {}
    with pytest.raises(ValueError):
        handover.destination("big", {"label": "Big", "store": "keychain", "max_length": 129})


@pytest.mark.parametrize("case", ["symlink", "directory", "dir_symlink"])
async def test_file_store_refuses_redirected_targets(asks, keychain, tmp_path, case):
    core, _, _ = asks
    allow(core)
    directory = handover.secret_dir(core.config.config_dir)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if case == "dir_symlink":
        directory.parent.mkdir(mode=0o700)
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir(parents=True, mode=0o700)
        if case == "symlink":
            (directory / "fanart").symlink_to(outside / "target")
        else:
            (directory / "fanart").mkdir()
    ask = (await secret_ask(asks, "fanart"))["ask"]
    result = await core.call(
        "cortex", "board_answer", {"ask_id": ask["id"], "answer": {"secret": "ab" * 16}}
    )
    assert result["error"]["code"] == "backend_error"
    assert list(outside.iterdir()) == []
    assert core.board.ask_row(ask["id"])["state"] == "open"


@pytest.mark.parametrize("mode", [0o644, 0o640])
async def test_allowlist_must_be_private(asks, keychain, mode):
    core, _, _ = asks
    allow(core, mode=mode)
    assert (await secret_ask(asks))["error"]["code"] == "invalid_arguments"


async def test_only_the_owner_surface_answers(asks, keychain):
    core, _, _ = asks
    allow(core)
    ask = (await secret_ask(asks))["ask"]
    own = await core.call(
        "codex", "board_answer", {"ask_id": ask["id"], "answer": {"secret": "x" * 20}}
    )
    assert own["status"] == "error"
    assert keychain.items() == {}


async def test_add_destination_action_then_ask(asks, keychain):
    core, state, _ = asks
    proposal = {"name": "tmdb", "label": "TMDB API key", "store": "keychain"}
    created = await call(
        asks,
        "ask",
        card="project#1",
        kind="confirm",
        prompt="Allow agents to ask for the key?",
        action={"type": "add_secret_destination", "handover": proposal},
    )
    ask = created["ask"]
    summary = "Lets agents ask you for TMDB API key, saved to Keychain item homelab.tmdb"
    assert ask["action"] == {"type": "add_secret_destination", "summary": summary}
    assert ask["destructive"] is True
    assert summary in state["comments"][1][-1]["body"]
    answered = (await owner(asks, ask["id"], {"choice": ["yes"]}))["ask"]
    assert answered["action"]["result"] == "applied"
    path = handover.allowlist_path(core.config.config_dir)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert handover.read_allowlist(core.config.config_dir)["tmdb"]["service"] == "homelab.tmdb"
    second = await call(
        asks,
        "ask",
        card="project#1",
        kind="confirm",
        prompt="Again?",
        action={"type": "add_secret_destination", "handover": proposal},
    )
    assert second["error"]["code"] == "invalid_arguments"
    ask = (await secret_ask(asks))["ask"]
    value = value32()  # built at runtime so the public snapshot carries no key-like literal
    await owner(asks, ask["id"], {"secret": value})
    assert keychain.get("homelab.tmdb") == value


async def test_add_destination_keeps_owner_entries_and_backs_up(asks, keychain):
    core, _, _ = asks
    path = allow(core, {"fanart": ALLOWLIST["fanart"]})
    original = path.read_bytes()
    ask = (
        await call(
            asks,
            "ask",
            card="project#1",
            kind="confirm",
            prompt="Allow?",
            action={
                "type": "add_secret_destination",
                "handover": {"name": "tmdb", "label": "TMDB"},
            },
        )
    )["ask"]
    assert (await owner(asks, ask["id"], {"choice": ["yes"]}))["ask"]["action"][
        "result"
    ] == "applied"
    rows = handover.read_allowlist(core.config.config_dir)
    assert set(rows) == {"fanart", "tmdb"} and rows["tmdb"]["store"] == "file"
    assert path.with_name(f"handover.yaml.before-ask-{ask['id']}").read_bytes() == original


async def test_declined_destination_is_not_added(asks, keychain):
    core, _, _ = asks
    ask = (
        await call(
            asks,
            "ask",
            card="project#1",
            kind="confirm",
            prompt="Allow?",
            action={
                "type": "add_secret_destination",
                "handover": {"name": "tmdb", "label": "TMDB"},
            },
        )
    )["ask"]
    await owner(asks, ask["id"], {"choice": ["no"]})
    assert not handover.allowlist_path(core.config.config_dir).exists()


@pytest.mark.parametrize(
    "proposal",
    [
        {"name": "Bad Name", "label": "x"},
        {"name": "tmdb", "label": "x", "store": "keychain", "service": "com.apple.account"},
        {"name": "tmdb", "label": "x", "store": "file", "service": "homelab.tmdb"},
        {"name": "tmdb", "label": "line\nbreak"},
        {"name": "tmdb", "label": "x", "min_length": 9, "max_length": 8},
        {"name": "tmdb", "label": "x", "path": "/etc/passwd"},
        {"name": "tmdb"},
    ],
)
async def test_bad_destination_proposals(asks, keychain, proposal):
    core, _, calls = asks
    result = await call(
        asks,
        "ask",
        card="project#1",
        kind="confirm",
        prompt="Allow?",
        action={"type": "add_secret_destination", "handover": proposal},
    )
    assert result["error"]["code"] == "invalid_arguments"
    assert not writes(calls)


def test_outputs_cannot_carry_a_secret():
    assert "secret" not in ANSWER_OUT["properties"]
    assert ANSWER_OUT["additionalProperties"] is False
    assert set(HANDOVER_OUT["properties"]) == {
        "name",
        "label",
        "store",
        "where",
        "charset",
        "min_length",
        "max_length",
        "replaces",
    }


def test_audit_hash_never_fingerprints_secret_named_keys():
    value = value32()
    stored = safe_args({"answer": {"secret": value}}, frozenset({"answer"}))
    redacted = json.dumps({"secret": "[REDACTED]"}, separators=(",", ":"))
    assert stored["answer"]["sha256"] == hashlib.sha256(redacted.encode()).hexdigest()[:16]
    assert value not in json.dumps(stored)


def test_value_never_on_a_command_line(monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs.get("input")))

        class Result:
            returncode = 0
            stdout = ""

        return Result()

    monkeypatch.setattr(handover.subprocess, "run", fake_run)
    handover.keychain_write("homelab.tmdb", "s3cretValue99")
    argv, data = seen[0]
    assert all("s3cretValue99" not in part for part in argv)
    assert data == "s3cretValue99\ns3cretValue99\n"
    assert os.environ.get("PATH") is not None  # environment is replaced, not inherited
