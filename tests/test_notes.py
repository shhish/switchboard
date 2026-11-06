"""Notes use fake helper sockets only, never a real vault or installed app."""

import json
from pathlib import Path

import pytest
import yaml

from switchboard import cli
from switchboard.config import DEFAULT_BACKENDS, Config
from switchboard.errors import CapabilityError
from switchboard.notes import Notes

from .conftest import change_policy
from .test_eventkit import socket_helper  # noqa: F401

ROW = {
    "path": "work/note.md",
    "title": "A note",
    "snippet": "one line",
    "modified": "2026-11-01T12:00:00Z",
}


@pytest.fixture
async def notes_helper(config, socket_helper, tmp_path, monkeypatch):  # noqa: F811
    # Only lexical configuration checks run in Python; this vault is never opened.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config.backends["notes"].update(
        enabled=True, root=str(tmp_path / "vault"), exclude=["personal/"]
    )
    config.backends["eventkit"]["enabled"] = False
    state, calls, path = socket_helper
    state["notes.search"] = {"results": [ROW], "searched": 3, "truncated": False}
    state["notes.get"] = {
        "path": ROW["path"],
        "title": ROW["title"],
        "modified": ROW["modified"],
        "content": "# A note\n\nUntrusted text\n",
        "truncated": False,
    }
    return state, calls, path


async def test_search_then_get_passes_config_and_preserves_content(core, config, notes_helper):
    _, calls, _ = notes_helper
    search = await core.call("dot", "notes_search", {"query": "A note"})
    assert search["results"] == [ROW]
    assert search["searched"] == 3 and search["limit"] == 10
    assert calls[-1] == {
        "method": "notes.search",
        "args": {
            "root": config.backends["notes"]["root"],
            "exclude": ["personal/"],
            "query": "A note",
            "limit": 10,
        },
    }
    note = await core.call("dot", "notes_get", {"path": ROW["path"]})
    assert note["content"] == "# A note\n\nUntrusted text\n"
    assert note["path"] == ROW["path"]
    assert calls[-1]["method"] == "notes.get"
    assert "request_access" not in json.dumps(calls)


async def test_python_filters_excluded_and_invalid_helper_paths(core, notes_helper):
    state, _, _ = notes_helper
    state["notes.search"]["results"] = [
        {**ROW, "path": "personal/hidden.md"},
        {**ROW, "path": "PERSONAL/hidden.md"},
        {**ROW, "path": "../escape.md"},
        ROW,
    ]
    assert (await core.call("dot", "notes_search", {"query": "note"}))["results"] == [ROW]
    state["notes.get"]["path"] = "personal/hidden.md"
    result = await core.call("dot", "notes_get", {"path": ROW["path"]})
    assert result["error"]["code"] == "backend_invalid"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("notes_search", {"query": " "}),
        ("notes_search", {"query": "a", "limit": 21}),
        ("notes_search", {"query": "a", "limit": True}),
        ("notes_search", {"query": "a", "root": "/tmp"}),
        *[
            ("notes_get", {"path": path})
            for path in ["../x.md", "/x.md", "a/../x.md", "x.txt", "a//x.md", "./x.md", "a\\b.md"]
        ],
    ],
)
async def test_invalid_arguments_never_reach_helper(core, notes_helper, tool, args):
    assert (await core.call("dot", tool, args))["error"]["code"] == "invalid_arguments"
    assert notes_helper[1] == []


async def test_scope_and_exclusions_precede_helper(core, config, notes_helper):
    _, calls, _ = notes_helper
    for path in ["personal/hidden.md", "PERSONAL/hidden.md"]:
        assert (await core.call("dot", "notes_get", {"path": path}))["error"][
            "code"
        ] == "resource_denied"
    change_policy(config, lambda p: p["clients"]["dot"].update(scopes=[]))
    assert (await core.call("dot", "notes_search", {"query": "a"}))["error"][
        "code"
    ] == "scope_denied"
    assert "notes_search" not in {t["name"] for t in core.tools("dot")}
    assert not calls


async def test_notes_doctor_never_sends_root_or_reads_vault(core, config, notes_helper):
    _, calls, path = notes_helper
    assert (await core.backends.probe("notes"))["status"] == "ok"
    assert calls == [{"method": "status", "args": {}}]
    config.backends["notes"]["enabled"] = False
    assert (await core.backends.probe("notes"))["status"] == "disabled"
    assert len(calls) == 1
    assert (await core.call("dot", "notes_search", {"query": "a"}))["error"][
        "code"
    ] == "backend_disabled"
    config.backends["notes"]["enabled"] = True
    path.unlink()
    assert (await core.backends.probe("notes"))["status"] == "unavailable"


async def test_local_owner_access_uses_one_search_and_discards_contents(config, notes_helper):
    state, calls, _ = notes_helper
    state["notes.search"]["results"] = [{**ROW, "snippet": "PRIVATE_NOTE_CONTENT"}]
    with pytest.raises(CapabilityError, match="Local owner"):
        await cli.execute(
            cli.parser().parse_args(["--client", "dot", "notes", "request-access"]), config
        )
    assert not calls
    result = await cli.execute(cli.parser().parse_args(["notes", "request-access"]), config)
    assert result == {"status": "ok"}
    assert len(calls) == 1 and calls[0]["method"] == "notes.search"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "value",
    [None, [], {"results": "bad"}, {"results": [ROW], "searched": True, "truncated": False}],
)
async def test_malformed_helper_results_are_bounded_errors(core, notes_helper, value):
    notes_helper[0]["notes.search"] = {"ok": True, "result": value}
    assert (await core.call("dot", "notes_search", {"query": "note"}))["error"][
        "code"
    ] == "backend_invalid"


async def test_notes_timeout_and_permission_errors_do_not_leak_or_retry(core, notes_helper):
    state, calls, _ = notes_helper
    for code in ["timeout", "unauthorized"]:
        state["notes.search"] = {"ok": False, "error": {"code": code, "message": "PRIVATE"}}
        result = await core.call("dot", "notes_search", {"query": "note"})
        assert result["error"]["code"] == "notes_" + code
        assert "PRIVATE" not in json.dumps(result)
    assert len(calls) == 2
    assert core.registry.capabilities["notes_search"].timeout_s == 20
    assert core.backends.notes.timeout == 20
    with pytest.raises(CapabilityError):
        await Notes(core.config).call("notes.write")


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": True},
        {"enabled": True, "root": "/tmp/vault"},
        {"root": "~/vault"},
        {"root": "relative"},
        {"exclude": ["../secret/"]},
        {"exclude": ["/secret/"]},
        {"exclude": [""]},
        {"exclude": "secret/"},
        {"exclude": [False]},
        {"exclude": ["a//b"]},
        {"enabled": "yes"},
        {"socket": "/tmp/other"},
    ],
)
def test_invalid_notes_config(config, settings):
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"backends": {"notes": settings}})
    )
    with pytest.raises(ValueError):
        Config.load()


def test_notes_config_is_lexical_and_policy_example(config, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert Config.load().backends["notes"] == DEFAULT_BACKENDS["notes"]
    settings = {
        "enabled": True,
        "root": str(tmp_path / "nonexistent-vault"),
        "exclude": ["personal/", "archive/old"],
    }
    (config.config_dir / "config.yaml").write_text(
        yaml.safe_dump({"backends": {"notes": settings}})
    )
    assert Config.load().backends["notes"] == settings
    for root in [str(tmp_path), str(tmp_path) + "/", str(tmp_path / "vault/../other")]:
        settings["root"] = root
        (config.config_dir / "config.yaml").write_text(
            yaml.safe_dump({"backends": {"notes": settings}})
        )
        with pytest.raises(ValueError):
            Config.load()
