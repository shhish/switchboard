"""Public bundled skills over both MCP eras; no installed skills or private files."""

import hashlib
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
    assert reply["result"]["capabilities"]["extensions"] == {"io.modelcontextprotocol/skills": {}}


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
        ("skills/list", {"cursor": "next"}, -32602),
        ("skills/list", {"extra": True}, -32602),
        ("skills/get", {}, -32602),
        ("skills/get", {"uri": "skill://unknown/SKILL.md"}, -32602),
        ("skills/get", {"uri": "switchboard://skills/switchboard"}, -32602),
        ("skills/get", {"uri": "skill://../config/SKILL.md"}, -32602),
        ("skills/get", {"uri": None}, -32602),
        ("skills/get", {"uri": 123}, -32602),
        ("skills/get", {"uri": "skill://switchboard/SKILL.md", "extra": 1}, -32602),
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


@pytest.mark.parametrize("modern", [True, False])
async def test_skills_extension_in_protocol(core, modern):
    discover_msg = (
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
    reply = await Protocol(core).handle(discover_msg, "agy")
    assert reply["result"]["capabilities"]["extensions"]["io.modelcontextprotocol/skills"] == {}
    assert reply["result"]["capabilities"]["resources"] == {}

    list_reply = await Protocol(core).handle(message("skills/list", modern=modern), "agy")
    list_result = list_reply["result"]
    if modern:
        assert list_result["resultType"] == "complete"
        assert list_result["ttlMs"] == 300000
        assert list_result["cacheScope"] == "private"
    else:
        assert "resultType" not in list_result
        assert "ttlMs" not in list_result
        assert "cacheScope" not in list_result

    skills = list_result["skills"]
    expected = SkillCatalog().load()
    assert len(skills) == len(expected)
    assert [s["frontmatter"]["name"] for s in skills] == sorted(expected)

    for entry in skills:
        name = entry["frontmatter"]["name"]
        skill = expected[name]
        expected_uri = f"skill://{name}/SKILL.md"
        assert entry["uri"] == expected_uri
        assert entry["frontmatter"]["description"] == skill.description
        assert len(entry["resources"]) == 1
        res = entry["resources"][0]
        assert res["uri"] == expected_uri

        read_reply = await Protocol(core).handle(
            message("resources/read", modern=modern, uri=expected_uri), "agy"
        )
        contents = read_reply["result"]["contents"]
        assert len(contents) == 1
        assert contents[0]["uri"] == expected_uri
        assert contents[0]["mimeType"] == "text/markdown"
        assert contents[0]["text"] == skill.markdown

        raw_bytes = contents[0]["text"].encode("utf-8")
        expected_digest = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
        assert res["digest"] == expected_digest
        assert res["size"] == len(raw_bytes)

        get_reply = await Protocol(core).handle(
            message("skills/get", modern=modern, uri=expected_uri), "agy"
        )
        get_result = get_reply["result"]
        if modern:
            assert get_result["resultType"] == "complete"
            assert get_result["ttlMs"] == 300000
            assert get_result["cacheScope"] == "private"
        else:
            assert "resultType" not in get_result
            assert "ttlMs" not in get_result
            assert "cacheScope" not in get_result
        assert get_result["skill"] == entry

    sb_read_reply = await Protocol(core).handle(
        message("resources/read", modern=modern, uri=expected["switchboard"].uri), "agy"
    )
    assert sb_read_reply["result"]["contents"] == [
        {
            "uri": expected["switchboard"].uri,
            "mimeType": "text/markdown",
            "text": expected["switchboard"].markdown,
        }
    ]

    rows = core.audit.tail(200)
    assert "skills/list" in {r["capability"] for r in rows}
    assert "skills/get" in {r["capability"] for r in rows}
    assert all(
        r["decision"] == "allowed" for r in rows if r["capability"] in ("skills/list", "skills/get")
    )


async def test_skills_extension_denied_client(core):
    with pytest.raises(CapabilityError):
        core.guidance("unknown", "skills/list", {})
    with pytest.raises(CapabilityError):
        core.guidance("unknown", "skills/get", {"uri": "skill://switchboard/SKILL.md"})
    with pytest.raises(CapabilityError):
        await core.mcp_capabilities("unknown")

    list_reply = await Protocol(core).handle(message("skills/list"), "unknown")
    assert list_reply["error"]["code"] == -32000

    get_reply = await Protocol(core).handle(
        message("skills/get", uri="skill://switchboard/SKILL.md"), "unknown"
    )
    assert get_reply["error"]["code"] == -32000


@pytest.mark.parametrize("modern", [True, False])
async def test_skills_extension_both_eras(server, config, modern):
    core, _, _ = server
    status, result = await post(server, config, message("skills/list", modern=modern), client="agy")
    assert status == 200
    skills = result["result"]["skills"]
    expected = SkillCatalog().load()
    assert len(skills) == len(expected)
    entry = next(s for s in skills if s["frontmatter"]["name"] == "switchboard")
    uri = "skill://switchboard/SKILL.md"
    assert entry["uri"] == uri
    status, read_reply = await post(
        server, config, message("resources/read", modern=modern, uri=uri), client="agy"
    )
    assert status == 200
    text = read_reply["result"]["contents"][0]["text"]
    assert entry["resources"][0]["digest"] == f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"
    status, get_reply = await post(
        server, config, message("skills/get", modern=modern, uri=uri), client="agy"
    )
    assert status == 200
    assert get_reply["result"]["skill"] == entry
