"""Compression only invokes isolated fake scripts, never media-ctrl or live services."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from switchboard.config import DEFAULT_BACKENDS, Config
from switchboard.core import Core
from switchboard.media_compress import MINIMAL_PATH, NOTE

from .conftest import change_policy, domain_payload

FAKE_SCRIPT = """import hashlib
import json
import os
import sys
import time
from pathlib import Path

state = Path(os.environ["MEDIA_CTRL_STATE_DIR"])
options = json.loads((state / "options.json").read_text())
with (state / "calls.jsonl").open("a") as output:
    record = {"argv": sys.argv, "env": dict(os.environ), "pid": os.getpid()}
    output.write(json.dumps(record) + "\\n")
mode = options.get("mode", "ok")
if mode == "timeout":
    time.sleep(30)
if mode == "exit2":
    print("PRIVATE backend error", file=sys.stderr)
    sys.exit(2)
if mode == "exit1":
    print("PRIVATE backend error", file=sys.stderr)
    sys.exit(1)
if mode == "malformed":
    print("PRIVATE non-json")
    sys.exit(0)
if mode == "multiple":
    print('{}\\n{}')
    sys.exit(0)
if mode in ("oversize", "oversize_stderr"):
    output = sys.stderr if mode == "oversize_stderr" else sys.stdout
    output.write("X" * 1000000)
    output.flush()
    time.sleep(30)
if mode == "raw":
    print(options["raw"])
    sys.exit(0)
if mode == "not_found":
    print('{"status":"not_found"}')
    sys.exit(0)
identifier = "compress-" + hashlib.sha256(sys.argv[-1].encode()).hexdigest()[:24]
path = sys.argv[-1] if sys.argv[-2] == "--path" else str(state / "library/Movie.mkv")
if sys.argv[1] == "status":
    result = {"request_id": sys.argv[-1], "path": path,
              "status": options.get("status", "skipped"),
              "reason": "protected_hdr_dv", "updated_at": "2026-10-28T12:00:00Z"}
else:
    result = {"request_id": identifier, "path": path, "status": "requested", "lane": "compression"}
result.update(options.get("override", {}))
print(json.dumps(result))
"""


@pytest.fixture
async def compression(config, tmp_path, monkeypatch):
    state = tmp_path / "compress-state"
    state.mkdir()
    (state / "library").mkdir()
    script = tmp_path / "compress_request.py"
    script.write_text(FAKE_SCRIPT)
    (state / "options.json").write_text("{}")
    config.backends["media_compress"].update(
        enabled=True, python=sys.executable, script=str(script), state_dir=str(state), timeout_s=2
    )

    def policy(data):
        for client in ("dot", "owner", "codex"):
            data["clients"][client]["scopes"].extend(["media:compress", "media:compress_read"])
        data["allowlists"]["media"] = {
            "allow_all": True,
            "compress_roots": [str(state / "library")],
        }

    change_policy(config, policy)
    for name in ("MEDIA_TEST", "HOMELAB_TEST", "PYTHONPATH", "PRIVATE_TOKEN"):
        monkeypatch.setenv(name, "PRIVATE")
    core = Core(config)
    try:
        yield core, state, script
    finally:
        await core.close()


def options(state, **data):
    (state / "options.json").write_text(json.dumps(data))


def calls(state):
    path = state / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def admit(core, args=None, client="dot"):
    args = (
        {
            "path": str(
                Path(core.config.backends["media_compress"]["state_dir"]) / "library/Movie.mkv"
            )
        }
        if args is None
        else args
    )
    pending = await core.call(client, "media_compress", args)
    assert pending["status"] == "approval_required", pending
    assert core.resolve("owner", pending["approval_id"], True)["status"] == "approved"
    return await core.call(
        client, "media_compress", {**args, "approval_id": pending["approval_id"]}
    )


async def test_approval_argv_environment_and_status(compression, monkeypatch):
    core, state, script = compression
    args = {"path": str(state / "library/Movie.mkv")}
    pending = await core.call("dot", "media_compress", args)
    assert pending["status"] == "approval_required"
    assert not calls(state)
    core.resolve("owner", pending["approval_id"], True)
    invocation = {**args, "approval_id": pending["approval_id"]}
    result = await core.call("dot", "media_compress", invocation)
    assert result["status"] == "requested"
    assert result["note"] == NOTE
    first = calls(state)[0]
    assert first["argv"] == [str(script), "admit", "--path", args["path"]]
    # macOS/Python may add these during interpreter startup. The env passed to
    # exec itself is asserted exactly below; no inherited variables reach it.
    runtime_env = dict(first["env"])
    for key in ("LC_CTYPE", "__CF_USER_TEXT_ENCODING"):
        runtime_env.pop(key, None)
    assert runtime_env == {"PATH": MINIMAL_PATH, "MEDIA_CTRL_STATE_DIR": str(state)}
    # Verify the interpreter too, as sys.argv only contains the script and its arguments.
    original = asyncio.create_subprocess_exec
    observed = []

    async def capture(*args, **kwargs):
        observed.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    status = await core.call("dot", "media_compress_status", {"request_id": result["request_id"]})
    assert observed[0][0] == (
        sys.executable,
        str(script),
        "status",
        "--request-id",
        result["request_id"],
    )
    assert observed[0][1]["env"] == {"PATH": MINIMAL_PATH, "MEDIA_CTRL_STATE_DIR": str(state)}
    assert domain_payload(status) == {
        "request_id": result["request_id"],
        "path": result["path"],
        "status": "skipped",
        "reason": "protected_hdr_dv",
        "updated_at": "2026-10-28T12:00:00Z",
    }
    assert (await core.call("dot", "media_compress", invocation))["error"][
        "code"
    ] == "approval_invalid"
    audit = core.audit.path.read_text()
    assert "PRIVATE" not in audit
    assert "media_compress_status" in audit


async def test_path_exact_argv_duplicate_receipt_and_persistence(compression, monkeypatch):
    core, state, script = compression
    path = str(state / "library/Movie with spaces;$(no shell).mkv")
    original = asyncio.create_subprocess_exec
    observed = []

    async def capture(*args, **kwargs):
        observed.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    first = await admit(core, {"path": path})
    again = await admit(core, {"path": path})
    assert domain_payload(first) == domain_payload(again)
    assert observed == [(sys.executable, str(script), "admit", "--path", path)] * 2
    assert len(core.storage.db.execute("SELECT * FROM compress_requests").fetchall()) == 1
    await core.close()
    reopened = Core(core.config)
    try:
        result = await reopened.call("dot", "media_compress_list", {})
        assert result["requests"][0]["request_id"] == first["request_id"]
        assert result["requests"][0]["path"] == path
    finally:
        await reopened.close()


async def test_path_duplicate_cross_client_visibility_and_list(compression):
    core, state, _ = compression
    first = await admit(core)
    assert domain_payload(first) == domain_payload(await admit(core))
    assert len(core.storage.db.execute("SELECT * FROM compress_requests").fetchall()) == 1
    key = {"request_id": first["request_id"]}
    for request_id in (first["request_id"], "compress-" + "0" * 24):
        denied = await core.call("codex", "media_compress_status", {"request_id": request_id})
        assert denied["error"]["code"] == "resource_denied"
    assert len(calls(state)) == 2
    assert (await core.call("owner", "media_compress_status", key))["status"] == "skipped"
    assert (await core.call("codex", "media_compress_list", {}))["requests"] == []
    shared = await admit(core, client="codex")
    assert domain_payload(shared) == domain_payload(first)
    assert (await core.call("codex", "media_compress_status", key))["status"] == "skipped"
    second = await admit(core, {"path": str(state / "library/Other.mkv")})
    before = len(calls(state))
    listed = await core.call("dot", "media_compress_list", {"limit": 1})
    assert [row["request_id"] for row in listed["requests"]] == [second["request_id"]]
    assert len((await core.call("owner", "media_compress_list", {}))["requests"]) == 3
    assert len(calls(state)) == before
    assert (await core.call("dot", "media_compress_list", {"limit": 51}))["error"][
        "code"
    ] == "invalid_arguments"


@pytest.mark.parametrize(
    "mode,code",
    [
        ("exit2", "compress_rejected"),
        ("exit1", "compress_failed"),
        ("malformed", "compress_invalid"),
        ("multiple", "compress_invalid"),
        ("timeout", "compress_timeout"),
        ("oversize", "compress_output_limit"),
        ("oversize_stderr", "compress_output_limit"),
    ],
)
async def test_command_failures_redacted_and_reaped(compression, mode, code):
    core, state, _ = compression
    options(state, mode=mode)
    if mode == "timeout":
        core.config.backends["media_compress"]["timeout_s"] = 0.15
    result = await admit(core)
    assert result["error"]["code"] == code
    assert "PRIVATE" not in json.dumps(result) + core.audit.path.read_text()
    assert not core.storage.db.execute("SELECT * FROM compress_requests").fetchall()
    with pytest.raises(ProcessLookupError):
        os.kill(calls(state)[0]["pid"], 0)


async def test_not_found(compression):
    core, state, _ = compression
    options(state, mode="not_found")
    assert domain_payload(await admit(core)) == {"status": "not_found", "note": NOTE}
    assert (await core.call("dot", "media_compress_list", {}))["requests"] == []


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "compress-" + "A" * 24},
        {"request_id": "bad"},
        {"request_id": 42},
        {"lane": "installer"},
        {"status": "encoding"},
        {"path": "relative"},
        {"path": "/bad\x00path"},
        {"private": "PRIVATE"},
        {"path": None},
    ],
)
async def test_strict_receipt_validation(compression, override):
    core, state, _ = compression
    options(state, override=override)
    assert (await admit(core))["error"]["code"] == "compress_invalid"
    assert not core.storage.db.execute("SELECT * FROM compress_requests").fetchall()


@pytest.mark.parametrize(
    "raw", ["[]", "null", '{"status":"not_found","status":"not_found"}', '{"status":NaN}']
)
async def test_strict_json(compression, raw):
    core, state, _ = compression
    options(state, mode="raw", raw=raw)
    assert (await admit(core))["error"]["code"] == "compress_invalid"


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": "compress-" + "0" * 24},
        {"status": "unknown"},
        {"path": "relative"},
        {"updated_at": None},
        {"updated_at": {}},
        {"reason": "PRIVATE\x00invalid"},
        {"reason": "x" * 201},
        {"reason": None},
    ],
)
async def test_status_validation(compression, override):
    core, state, _ = compression
    receipt = await admit(core)
    options(state, override=override)
    result = await core.call("dot", "media_compress_status", {"request_id": receipt["request_id"]})
    assert result["error"]["code"] == "compress_invalid"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "status",
    [
        "requested",
        "queued",
        "encoding",
        "encoded_pending_install",
        "installed",
        "skipped",
        "attention",
        "failed",
    ],
)
async def test_status_fixed_fields(compression, status):
    core, state, _ = compression
    receipt = await admit(core)
    options(
        state,
        status=status,
        override={
            "private": "PRIVATE",
            "reason": "insufficient_savings_at_target",
            "updated_at": 12345,
        },
    )
    result = await core.call("dot", "media_compress_status", {"request_id": receipt["request_id"]})
    assert result["status"] == status
    assert result["reason"] == "insufficient_savings_at_target"
    assert result["updated_at"] == 12345
    assert "private" not in result


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"tmdb_id": 1, "path": "/Volumes/server/Movie.mkv"},
        {"tmdb_id": 0},
        {"tmdb_id": 10**9 + 1},
        {"tmdb_id": True},
        {"tmdb_id": "42"},
        {"path": "relative"},
        {"path": "/bad\x00path"},
        {"path": "/" + "a" * 1024},
        {"tmdb_id": 42, "extra": "no"},
    ],
)
async def test_invalid_inputs_before_approval(compression, args):
    core, state, _ = compression
    assert (await core.call("dot", "media_compress", args))["error"]["code"] == "invalid_arguments"
    assert not calls(state)


async def test_path_policy_denies_escape_missing_roots_and_reload(compression):
    core, state, _ = compression
    library = state / "library"
    (library / "escape").symlink_to(state)
    for path in (
        state / "movie.mkv",
        state / "library-other/movie.mkv",
        library / "../movie.mkv",
        library / "escape/movie.mkv",
    ):
        result = await core.call("dot", "media_compress", {"path": str(path)})
        assert result["error"]["code"] == "resource_denied"
    path_args = {"path": str(library / "Movie.mkv")}
    pending = await core.call("dot", "media_compress", path_args)
    core.resolve("owner", pending["approval_id"], True)
    change_policy(core.config, lambda p: p["allowlists"]["media"].pop("compress_roots"))
    result = await core.call(
        "dot", "media_compress", {**path_args, "approval_id": pending["approval_id"]}
    )
    assert result["error"]["code"] == "resource_denied"
    assert not calls(state)


async def test_disabled_visible_and_no_approval(compression):
    core, state, _ = compression
    core.config.backends["media_compress"]["enabled"] = False
    names = {tool["name"] for tool in core.tools("dot")}
    assert {"media_compress", "media_compress_status", "media_compress_list"} <= names
    for name, args in (
        ("media_compress", {"tmdb_id": 42}),
        ("media_compress_status", {"request_id": "compress-" + "0" * 24}),
    ):
        result = await core.call("dot", name, args)
        assert result["error"] == {
            "code": "compress_unavailable",
            "message": "Per-title compression is not enabled yet",
            "retryable": False,
        }
    assert not calls(state)
    assert not core.storage.db.execute("SELECT * FROM approvals").fetchall()
    assert (await core.call("dot", "media_compress_list", {}))["requests"] == []


async def test_scope_denial_hidden_and_direct(compression):
    core, state, _ = compression
    for name, args in (
        ("media_compress", {"tmdb_id": 42}),
        ("media_compress_status", {"request_id": "compress-" + "0" * 24}),
        ("media_compress_list", {}),
    ):
        assert name not in {tool["name"] for tool in core.tools("agy")}
        assert (await core.call("agy", name, args))["error"]["code"] == "scope_denied"
    assert not calls(state)


async def test_doctor_never_executes(compression):
    core, state, script = compression
    assert (await core.backends.probe("media_compress"))["status"] == "ok"
    script.unlink()
    assert (await core.backends.probe("media_compress"))["status"] == "down"
    assert (await core.call("dot", "media_compress", {"tmdb_id": 42}))["error"][
        "code"
    ] == "compress_down"
    core.config.backends["media_compress"]["enabled"] = False
    assert (await core.backends.probe("media_compress"))["status"] == "disabled"
    assert not calls(state)


@pytest.mark.parametrize(
    "settings",
    [
        {"python": "relative"},
        {"script": "relative"},
        {"state_dir": "relative"},
        {"timeout_s": 0},
        {"timeout_s": 61},
        {"timeout_s": True},
        {"timeout_s": float("nan")},
        {"enabled": True, "script": "/nonexistent-compression-script"},
        {"enabled": True, "python": "/tmp"},
        {"url": "http://127.0.0.1:1234"},
        {"enabled": "yes"},
    ],
)
def test_config_validation(config, settings):
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"backends": {"media_compress": settings}})
    )
    with pytest.raises(ValueError):
        Config.load()


def test_config_defaults_and_examples(config):
    assert Config.load().backends["media_compress"] == DEFAULT_BACKENDS["media_compress"]
    policy = yaml.safe_load(Path("config/policy.example.yaml").read_text())
    for client in set(policy["clients"]) - {"cortex", "deepseek", "dream"}:
        assert {"media:compress", "media:compress_read"} <= set(policy["clients"][client]["scopes"])
    assert policy["allowlists"]["media"]["compress_roots"] == [
        "/Volumes/server",
        "/Volumes/server2",
        "/Volumes/server3",
    ]
    example = yaml.safe_load(Path("config/config.example.yaml").read_text())
    assert example["backends"]["media_compress"] == DEFAULT_BACKENDS["media_compress"]


def library(core, state, monkeypatch, movies):
    """Fake media-api /movies/list (read-only) for TMDB resolution."""
    original = core.backends.get

    async def get(name, path, **kwargs):
        if name == "media" and path == "/movies/list":
            return {"movies": movies}
        return await original(name, path, **kwargs)

    monkeypatch.setattr(core.backends, "get", get)


async def test_tmdb_admission_authorizes_library_folder_before_admit(compression, monkeypatch):
    core, state, script = compression
    folder = str(state / "library")
    library(core, state, monkeypatch, [{"tmdb_id": 42, "has_file": True, "path": folder}])
    result = await admit(core, {"tmdb_id": 42})
    assert result["status"] == "requested", result
    assert calls(state)[-1]["argv"][-2:] == ["--tmdb-id", "42"]


async def test_tmdb_outside_roots_is_refused_without_calling_admit(compression, monkeypatch):
    core, state, _ = compression
    library(
        core, state, monkeypatch, [{"tmdb_id": 42, "has_file": True, "path": "/outside/Movies"}]
    )
    result = await admit(core, {"tmdb_id": 42})
    assert result["status"] == "error" and result["error"]["code"] == "resource_denied"
    assert not calls(state)  # admit never ran


async def test_tmdb_not_in_library_is_not_found_without_admit(compression, monkeypatch):
    core, state, _ = compression
    library(core, state, monkeypatch, [{"tmdb_id": 7, "has_file": True, "path": "/x"}])
    result = await admit(core, {"tmdb_id": 42})
    assert result["status"] == "not_found"
    assert not calls(state)
