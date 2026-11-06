"""Public bundled skills over both MCP eras; no installed skills or private files."""

import json

import pytest

from switchboard.errors import CapabilityError
from switchboard.guidance import MAX_SKILL_BYTES, MAX_TASK_CHARS, SKILLS_ROOT, SkillCatalog
from switchboard.protocol import Protocol

from .test_protocol import message, post


@pytest.mark.parametrize("modern", [True, False])
async def test_skill_prompts_and_resources_both_eras(server, config, modern):
    core, _, _ = server
    # agy has only jobs scopes: public guidance must not require a capability scope.
    _, result = await post(server, config, message("prompts/list", modern=modern), client="agy")
    prompts = result["result"]["prompts"]
    expected = SkillCatalog().load()
    assert {p["name"] for p in prompts} == set(expected)
    assert len(expected) == len(list(SKILLS_ROOT.glob("*/SKILL.md")))
    assert [p["name"] for p in prompts] == sorted(expected)
    _, resources = await post(
        server, config, message("resources/list", modern=modern), client="agy"
    )
    resources = resources["result"]["resources"]
    assert {r["uri"] for r in resources} == {s.uri for s in expected.values()}
    for prompt in prompts:
        skill = expected[prompt["name"]]
        assert prompt["description"] == skill.description
        assert prompt["arguments"][0]["name"] == "task"
        assert prompt["arguments"][0]["required"] is False
        for arguments in ({}, {"task": "check the lamp"}):
            status, reply = await post(
                server,
                config,
                message("prompts/get", modern=modern, name=skill.name, arguments=arguments),
                client="agy",
            )
            assert status == 200
            text = skill.body + ("\n\nTask: check the lamp" if arguments else "")
            assert reply["result"]["messages"] == [
                {"role": "user", "content": {"type": "text", "text": text}}
            ]
        status, reply = await post(
            server, config, message("resources/read", modern=modern, uri=skill.uri), client="agy"
        )
        assert status == 200
        assert reply["result"]["contents"] == [
            {"uri": skill.uri, "mimeType": "text/markdown", "text": skill.markdown}
        ]
    rows = core.audit.tail(200)
    assert {r["capability"] for r in rows} == {
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
    }
    assert all(row["decision"] == "allowed" and row["client"] == "agy" for row in rows)
    assert "check the lamp" not in json.dumps(rows)


@pytest.mark.parametrize("modern", [True, False])
async def test_guidance_capabilities_both_eras(server, config, modern):
    body = (
        message("server/discover")
        if modern
        else message(
            "initialize",
            modern=False,
            protocolVersion="2025-03-26",
            capabilities={},
            clientInfo={"name": "test", "version": "1"},
        )
    )
    _, reply = await post(server, config, body)
    assert reply["result"]["capabilities"]["prompts"] == {}
    assert reply["result"]["capabilities"]["resources"] == {}


@pytest.mark.parametrize(
    "method,params,code",
    [
        ("prompts/list", {"cursor": "next"}, -32602),
        ("resources/list", {"extra": True}, -32602),
        ("prompts/get", {}, -32602),
        ("prompts/get", {"name": "../config"}, -32602),
        ("prompts/get", {"name": "switchboard", "arguments": {"other": "x"}}, -32602),
        ("prompts/get", {"name": "switchboard", "arguments": {"task": True}}, -32602),
        (
            "prompts/get",
            {"name": "switchboard", "arguments": {"task": "x" * (MAX_TASK_CHARS + 1)}},
            -32602,
        ),
        ("resources/read", {"uri": "switchboard://skills/../config"}, -32002),
        ("resources/read", {"uri": "file:///private/config.yaml"}, -32002),
        ("resources/read", {"uri": None}, -32602),
    ],
)
async def test_bad_guidance_requests_are_audited(core, method, params, code):
    reply = await Protocol(core).handle(message(method, **params), "agy")
    assert reply["error"]["code"] == code
    row = core.audit.tail()[-1]
    assert row["capability"] == method and row["error_code"] in {"not_found", "invalid_arguments"}


async def test_guidance_authentication_and_fail_closed_policy(server, config):
    core, url, session = server
    response = await session.post(
        url + "/mcp",
        json=message("prompts/list"),
        headers={
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status == 401
    with pytest.raises(CapabilityError):
        core.guidance("unknown", "prompts/list", {})
    row = core.audit.tail()[-1]
    assert row["client"] == "unknown" and row["decision"] == "denied"
    # Valid transport auth with a client removed from policy still fails closed.
    from .conftest import change_policy

    change_policy(config, lambda p: p["clients"].pop("agy"))
    _, reply = await post(server, config, message("resources/list"), client="agy")
    assert reply["error"]["code"] == -32000


def test_loader_is_release_relative_and_rejects_unsafe_files(tmp_path, monkeypatch):
    outside = tmp_path / "secret.md"
    outside.write_text("PRIVATE secret config")
    root = tmp_path / "skills"
    root.mkdir()
    good = root / "public"
    good.mkdir()
    text = "---\ndescription: Public guide\n---\n\nUse safe tools.\n"
    (good / "SKILL.md").write_text(text)
    linked_file = root / "linked-file"
    linked_file.mkdir()
    (linked_file / "SKILL.md").symlink_to(outside)
    (root / "linked-dir").symlink_to(good, target_is_directory=True)
    for name, contents in (
        ("oversize", text + "x" * MAX_SKILL_BYTES),
        ("malformed", "---\ndescription: [invalid\n---\nbody"),
        ("no-description", "---\nname: other\n---\nbody"),
        ("no-end", "---\ndescription: text\nbody"),
    ):
        directory = root / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(contents)
    monkeypatch.chdir(tmp_path)
    assert "switchboard" in SkillCatalog().load()  # independent of cwd
    catalog = SkillCatalog(root)
    assert list(catalog.load()) == ["public"]
    assert catalog.load()["public"].markdown == text
    assert SkillCatalog(root / "missing").load() == {}
    link = tmp_path / "skills-link"
    link.symlink_to(root, target_is_directory=True)
    assert SkillCatalog(link).load() == {}


def test_deployed_release_finds_skills_beside_its_venv(tmp_path, monkeypatch):
    import sys

    from switchboard import guidance

    release = tmp_path / "release"
    (release / "skills" / "demo").mkdir(parents=True)
    (release / ".venv").mkdir()
    site = tmp_path / "site" / "lib" / "python3" / "site-packages" / "switchboard"
    site.mkdir(parents=True)
    monkeypatch.setattr(guidance, "__file__", str(site / "guidance.py"))
    monkeypatch.setattr(sys, "prefix", str(release / ".venv"))
    assert guidance.release_skills_root() == (release / "skills").resolve()
