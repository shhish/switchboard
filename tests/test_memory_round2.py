"""Automatic subjects, deterministic maintenance and offline reflection contracts."""

import asyncio
import json
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from switchboard.auth import CLIENTS
from switchboard.cli import execute, parser
from switchboard.domains import build_registry
from switchboard.domains.common import output
from switchboard.execution import Execution
from switchboard.memory import doctor
from switchboard.memory_maintenance import meta
from switchboard.memory_subjects import ENTITY_KEYS, UNDECLARED, MemorySubject
from switchboard.registry import Capability
from switchboard.schema import obj, string

from .conftest import change_policy
from .test_eventkit import socket_helper  # noqa: F401
from .test_memory import item, mem, remember  # noqa: F401
from .test_things import things_helper  # noqa: F401


@pytest.fixture
async def memory2(mem, config):  # noqa: F811
    def scopes(p):
        for rule in p["clients"].values():
            rule["scopes"].append("memory:reflect")

    change_policy(config, scopes)
    return mem


def local_day(offset=1, hour=10):
    return (
        (datetime.now().astimezone() + timedelta(days=offset))
        .replace(hour=hour, minute=0, second=0, microsecond=0)
        .timestamp()
    )


def test_all_capabilities_explicitly_declare_memory():
    for cap in build_registry().capabilities.values():
        assert cap.memory is not UNDECLARED
        if cap.memory is None and ENTITY_KEYS & cap.input_schema["properties"].keys():
            assert cap.memory_optout
        if cap.memory:
            assert cap.output_schema["properties"]["notes"]["maxItems"] == 3
            assert cap.manifest()["memory"]["domain"] == cap.memory.domain

    async def handler(*args):
        return {"status": "ok"}

    with pytest.raises(ValueError, match="explicitly declare"):
        Capability("future_tool", "Future", "Future tool", obj(), "ops:read", handler)
    with pytest.raises(ValueError, match="opt-out"):
        Capability(
            "future_entity",
            "Future",
            "Future tool",
            obj({"service": string()}),
            "ops:read",
            handler,
            memory=None,
        )
    cap = Capability(
        "aggregate",
        "Aggregate",
        "Bulk listing",
        obj({"name": string()}),
        "ops:read",
        handler,
        memory=None,
        memory_optout="Bulk listing",
    )
    assert cap.memory is None


def test_subject_extraction_is_bounded_and_validated():
    subject = MemorySubject("ops", arg_keys=("host",), result_paths=("items[].name",))
    values = subject.extract(
        {"host": "fixture"}, {"items": [{"name": str(i)} for i in range(10000)]}
    )
    assert len(values) == 257 and values[0] == "fixture"
    assert subject.extract({}, {"items": "not an array"}) == []
    for args in (
        {"domain": "finance"},
        {"domain": "ops", "result_paths": ("items..name",)},
        {"domain": "ops", "result_paths": ("items.name",) * 17},
    ):
        with pytest.raises(ValueError):
            MemorySubject(**args)


async def test_future_tool_automatically_gets_notes(memory2):
    row = await remember(memory2, subject="fixture-host")

    async def handler(*args):
        return {"status": "ok", "hostname": "fixture-host"}

    memory2.registry.add(
        Capability(
            "future_entity",
            "Future",
            "Future tool",
            obj(),
            "ops:read",
            handler,
            output_schema=output(hostname=string()),
            memory=MemorySubject("ops", result_paths=("hostname",)),
        )
    )
    result = await memory2.call("codex", "future_entity", {})
    assert result["notes"][0]["id"] == row["id"]
    assert memory2.memory.row("codex", row["id"])["use_count"] == 1


@pytest.mark.parametrize(
    "name,domain,subject,args,result",
    [
        ("ops_host_checks", "ops", "pi", {"host": "pi"}, {}),
        ("ops_alerts_silence", "ops", "probe-a", {"probe": "probe-a"}, {}),
        ("home_run_scene_secure", "home", "scene-a", {"scene_id": "scene-a"}, {}),
        ("home_state_set", "home", "state-a", {"state": "state-a"}, {}),
        ("media_mark_watched", "media", "778", {"rating_key": "778"}, {}),
        ("git_local_status", "git", "team/project", {"repo": "team/project"}, {}),
        ("books_request", "books", "book title", {"book_id": "b1"}, {"title": "Book Title"}),
        ("routines_run", "routines", "evening", {"routine": "evening"}, {}),
        ("notes_get", "general", "notes/server.md", {"path": "notes/server.md"}, {}),
        ("recipes_get", "general", "42", {"id": 42}, {}),
        ("calendar_update_event", "calendar", "dinner", {}, {"event": {"title": "Dinner"}}),
        ("reminders_update", "tasks", "milk", {}, {"reminder": {"title": "Milk"}}),
        ("tasks_update", "tasks", "review", {}, {"todo": {"name": "Review"}}),
        ("weather_now", "general", "weather", {}, {}),
        ("dns_resume", "dns", "adguard", {}, {}),
    ],
)
async def test_new_subject_declarations(memory2, name, domain, subject, args, result):
    row = await remember(memory2, domain=domain, subject=subject)
    assert memory2.memory.attach_notes("codex", name, args, result) == 1
    assert result["notes"][0]["id"] == row["id"]


async def seed_findings(core):
    first = await remember(core)
    await remember(core, force_new=True)
    pref = await remember(
        core, kind="preference", title="Format", body="Audio format", subject="books"
    )
    await remember(
        core, kind="preference", title="Format", body="Text format", subject="books", force_new=True
    )
    await remember(
        core,
        title="Explicit contradiction",
        body="Contradictory source",
        links=[{"to": pref["id"], "rel": "contradicts"}],
    )
    proposal = await remember(core, "dot", title="Pending review", body="Another fact")
    with core.storage.db:
        core.storage.db.execute(
            "UPDATE memory SET review_after=?,recorded_at=? WHERE id=?",
            (time.time() - 1, time.time() - 100 * 86400, first["id"]),
        )
        core.storage.db.execute(
            "UPDATE memory SET recorded_at=? WHERE id=?", (time.time() - 8 * 86400, proposal["id"])
        )
    return first, proposal


async def test_tidy_all_kinds_no_mutation_and_idempotence(memory2):
    await seed_findings(memory2)
    before = [dict(r) for r in memory2.storage.db.execute("SELECT * FROM memory ORDER BY id")]
    now = local_day()
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(now)
    findings = memory2.memory.maintenance.findings("owner")
    assert {f["kind"] for f in findings} == {
        "duplicate",
        "contradiction",
        "stale",
        "unused",
        "proposal_backlog",
    }
    assert before == [
        dict(r) for r in memory2.storage.db.execute("SELECT * FROM memory ORDER BY id")
    ]
    assert float(meta(memory2.storage.db, "last_tidy_at")) == now
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(now + 600)
        memory2.memory.maintenance.maintain(now + 86400)
    assert len(memory2.memory.maintenance.findings("owner")) == len(findings)


async def test_tidy_not_before_three(memory2):
    await remember(memory2, review_after_days=1)
    now = local_day(hour=2)
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(now)
    assert meta(memory2.storage.db, "last_tidy_at") is None
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(local_day(hour=3))
    assert meta(memory2.storage.db, "last_tidy_at") is not None


@pytest.mark.parametrize("decision", ["keep", "retract", "supersede_with"])
async def test_owner_resolves_findings(memory2, config, decision):
    first, _ = await seed_findings(memory2)
    with memory2.storage.db:
        memory2.memory.maintenance.tidy(time.time())
    finding = next(f for f in memory2.memory.maintenance.findings("owner") if f["kind"] == "stale")
    args = {"finding_ids": [finding["id"]], "decision": decision}
    if decision == "supersede_with":
        replacement = await remember(memory2, title="Replacement", body="Replacement verified data")
        args["with_id"] = replacement["id"]
    assert (await memory2.call("dot", "memory_resolve", args))["error"]["code"] == "scope_denied"
    result = await memory2.call("owner", "memory_resolve", args)
    assert "error" not in result, result
    assert finding["id"] not in {f["id"] for f in memory2.memory.maintenance.findings("owner")}
    assert (
        memory2.memory.row("owner", first["id"])["status"]
        == {"keep": "active", "retract": "retracted", "supersede_with": "superseded"}[decision]
    )
    args = parser().parse_args(["memory", "review"])
    listed = await execute(args, config)
    assert "findings" in listed and "proposed" in listed


async def test_findings_read_gating_and_atomic_resolution(memory2, config):
    home = await remember(memory2, domain="home")
    ops = await remember(
        memory2,
        title="Other fact",
        body="Other fact",
        links=[{"to": home["id"], "rel": "contradicts"}],
    )
    with memory2.storage.db:
        memory2.memory.maintenance.tidy(time.time())
    change_policy(config, lambda p: p["clients"]["dot"].update(scopes=["memory:read", "ops:read"]))
    assert memory2.memory.maintenance.findings("dot") == []
    finding = memory2.memory.maintenance.findings("owner")[0]
    result = await memory2.call(
        "owner",
        "memory_resolve",
        {"finding_ids": [finding["id"], "missing"], "decision": "retract"},
    )
    assert "error" in result
    assert memory2.memory.row("owner", ops["id"])["status"] == "active"


async def test_digest_daily_channels_open_only_and_config_hour(memory2):
    now = local_day(hour=9)
    memory2.config.alerts = {"ntfy": {}, "email": {}}
    # No worker sends anything: only inspect the transactional outbox.
    await memory2.alerts.close()
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(now)
    assert memory2.storage.db.execute("SELECT count(*) FROM alert_outbox").fetchone()[0] == 0
    await remember(memory2, "dot", title="Owner review needed", body="x" * 500)
    memory2.config.memory["digest_hour"] = 11
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(now)
    assert meta(memory2.storage.db, "last_digest_at") is None
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(local_day(hour=11))
        memory2.memory.maintenance.maintain(local_day(hour=12))
    rows = memory2.storage.db.execute("SELECT channel,payload FROM alert_outbox").fetchall()
    assert {r["channel"] for r in rows} == {"ntfy", "email"} and len(rows) == 2
    message = json.loads(rows[0]["payload"])
    assert message["title"].startswith("Memory: 1 proposals,")
    assert "Review: sb memory review" in message["body"] and "x" * 121 not in message["body"]
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(local_day(offset=2, hour=12))
    assert memory2.storage.db.execute("SELECT count(*) FROM alert_outbox").fetchone()[0] == 4
    assert doctor(memory2.storage.db)["last_digest_at"] is not None


async def test_digest_no_channels_and_disabled(memory2):
    await remember(memory2, "dot")
    memory2.config.alerts = {}
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(local_day())
    assert meta(memory2.storage.db, "last_digest_at") is None
    memory2.config.memory["enabled"] = False
    before = meta(memory2.storage.db, "last_tidy_at")
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(local_day(offset=2))
    assert meta(memory2.storage.db, "last_tidy_at") == before


async def test_reflection_counts_redaction_bounds_and_evidence(memory2, config):
    await remember(
        memory2, domain="home", title="Private lamp", body="Not part of reflection bodies"
    )
    await remember(memory2, domain="general", title="Shared conventions")
    for _ in range(6):
        await memory2.call("codex", "ops_health", {})
    await memory2.call("codex", "ops_host_checks", {"host": "not-configured"})
    memory2.monitor.emit("service.down", "fixture-probe")
    memory2.monitor.emit("service.recovered", "fixture-probe")
    memory2.monitor.emit("service.down", "storage:fixture-disk")
    now = time.time()
    with memory2.storage.db:
        memory2.storage.db.execute(
            "INSERT INTO routine_runs(run_id,routine,hash,trigger,status,started_at) "
            "VALUES('run-fixture','evening','hash','manual','succeeded',?)",
            (now,),
        )
    result = await memory2.call("codex", "memory_reflect_input", {})
    assert "error" not in result, result
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 40000
    assert result["audit"]["counts"] and result["audit"]["errors"] and result["audit"]["loops"]
    assert result["audit"]["actions"] and result["routine_runs"]
    assert {r["state"] for r in result["monitoring"]["incidents"]} >= {"open", "closed"}
    assert result["monitoring"]["storage_alerts"]
    assert "Not part of reflection bodies" not in json.dumps(result)
    assert "fixture-probe" in result["evidence_refs"]
    args = {
        "items": [item(domain="ops", evidence_ref="fixture-probe")],
        "source_label": "dream:" + datetime.now().date().isoformat(),
    }
    imported = await memory2.call("codex", "memory_import", args)
    assert imported["results"][0]["result"] == "written"
    row = memory2.memory.row("codex", imported["results"][0]["id"])
    assert row["evidence_ref"] == "fixture-probe" and row["source_label"] == args["source_label"]
    assert row["status"] == "proposed" and row["source_kind"] == "imported"
    assert doctor(memory2.storage.db)["last_dream_at"] is not None
    change_policy(
        config,
        lambda p: p["clients"]["dream"].update(
            scopes=["memory:read", "memory:reflect", "memory:write"]
        ),
    )
    dream = await memory2.call("dream", "memory_reflect_input", {})
    assert dream["allowed_domains"] == ["general"]
    assert "home" not in dream["memory_titles"] and dream["monitoring"]["incidents"] == []
    assert "fixture-probe" not in dream["evidence_refs"]
    assert "error" in await memory2.call("dot", "memory_reflect_input", {"hours": 73})
    change_policy(config, lambda p: p["clients"]["dot"]["scopes"].remove("memory:reflect"))
    assert (await memory2.call("dot", "memory_reflect_input", {}))["error"][
        "code"
    ] == "scope_denied"


async def test_reflection_large_input_bounded(memory2):
    # Real writes remain bounded; create many fixture rows without an expensive live backend.
    for i in range(260):
        await remember(
            memory2,
            domain="general",
            title=f"Fact {i} " + "界" * 100,
            body="Fixture data",
            force_new=True,
        )
    result = await memory2.call("codex", "memory_reflect_input", {})
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 40000 and result["truncated"]


@pytest.mark.parametrize("ref", [None, "unknown-ref", "stale-trace"])
async def test_dream_rejects_missing_unknown_or_old_evidence(memory2, ref):
    if ref == "stale-trace":
        memory2.audit.append(
            client="codex",
            capability="ops_health",
            args={},
            decision="allowed",
            outcome="ok",
            ctx=Execution.new(trace_id=ref),
        )
        with memory2.storage.db:
            memory2.storage.db.execute(
                "UPDATE audit_log SET ts_ms=? WHERE trace_id=?",
                ((time.time() - 74 * 3600) * 1000, ref),
            )
    args = {
        "items": [item(**({"evidence_ref": ref} if ref else {}))],
        "source_label": "dream:2030-01-01",
    }
    result = await memory2.call("codex", "memory_import", args)
    assert result["results"] == [{"result": "rejected", "reason": "invalid_evidence"}]


async def test_reflection_issued_refs_are_client_bound(memory2):
    memory2.monitor.emit("service.down", "fixture-a")
    await memory2.call("codex", "memory_reflect_input", {})
    result = await memory2.call(
        "claude",
        "memory_import",
        {"source_label": "dream:2030-01-01", "items": [item(evidence_ref="fixture-a")]},
    )
    assert result["results"][0]["reason"] == "invalid_evidence"


def test_dream_identity_scopes(config):
    from switchboard.policy import Policy

    assert "dream" in CLIENTS
    # Memory plus read-only domain scopes, so its (always proposed) reflections can
    # cover ops, media, home and routines; no write, control or approval scope.
    assert Policy(Path("config/policy.example.yaml")).client("dream")["scopes"] == [
        "memory:read",
        "memory:reflect",
        "memory:write",
        "ops:read",
        "media:read",
        "home:read",
        "routines:read",
    ]
    plist = plistlib.loads(Path("launchd/memory-dream.plist.example").read_bytes())
    assert (
        plist["StartCalendarInterval"]["Hour"] == 4 and "__HOME__" in plist["ProgramArguments"][0]
    )


async def test_usage_stats_read_write_review_notes_and_doctor(memory2, config):
    row = await remember(memory2, domain="home", subject="lamp")
    await memory2.call("codex", "memory_brief", {})
    await memory2.call("codex", "memory_search", {"query": "port"})
    await memory2.call("codex", "memory_get", {"id": row["id"]})
    await memory2.call("codex", "home_get", {"accessory_id": "a1"})
    draft = await remember(memory2, "dot", force_new=True)
    await memory2.call("owner", "memory_review", {"ids": [draft["id"]], "decision": "approve"})
    change_policy(
        config, lambda p: p["clients"]["codex"]["scopes"].extend(["audit:read", "audit:read_all"])
    )
    digest = await memory2.call("codex", "audit_digest", {})
    stats = {r["client"]: r for r in digest["memory"]["clients"]}
    assert (
        stats["codex"]["brief"]
        == stats["codex"]["search"]
        == stats["codex"]["get"]
        == stats["codex"]["notes"]
        == 1
    )
    assert stats["codex"]["writes"]["active"] == 1 and stats["dot"]["writes"]["proposed"] == 1
    assert stats["owner"]["approved"] == 1
    status = await memory2.call("codex", "ops_switchboard_status", {})
    assert {"last_tidy_at", "last_digest_at", "last_dream_at", "counts"} <= status["checks"][
        "memory"
    ].keys()


@pytest.mark.parametrize(
    "response",
    [{"status": "ok", "brief": "fixture\n"}, {"status": "error", "error": {}}, OSError("offline")],
)
async def test_cli_brief_any_client_and_silent_failure(config, monkeypatch, response):
    async def fake(cfg, client, method, path, args):
        assert (
            client == "deepseek" and path == "/v1/call/memory_brief" and args == {"domain": "ops"}
        )
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("switchboard.cli.request", fake)
    args = parser().parse_args(["memory", "brief", "--client", "deepseek", "--domain", "ops"])
    result = await execute(args, config)
    assert result["brief"] == (response.get("brief", "") if isinstance(response, dict) else "")


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_brief_main_plain_or_json(config, monkeypatch, capsys, json_output):
    from switchboard.cli import main

    async def fake(*args):
        return {"status": "ok", "brief": "fixture brief\n"}

    monkeypatch.setattr("switchboard.cli.request", fake)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sb", "--client", "codex", "memory", "brief", *(["--json"] if json_output else [])],
    )
    assert main() == 0
    out = capsys.readouterr().out
    assert (
        json.loads(out)["brief"] == "fixture brief\n" if json_output else out == "fixture brief\n"
    )


def test_cli_brief_main_bad_config_is_silent(config, monkeypatch, capsys):
    from switchboard.cli import main

    (config.config_dir / "config.yaml").write_text("unknown: true\n")
    monkeypatch.setattr(sys, "argv", ["sb", "memory", "brief"])
    assert main() == 0 and capsys.readouterr().out == ""


def stub(tmp_path, name, source):
    path = tmp_path / name
    path.write_text("#!" + sys.executable + "\n" + source)
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("mode", ["ok", "empty", "failed", "timeout"])
def test_claude_hook_json_and_failures(tmp_path, mode):
    code = {
        "ok": 'print("Shared fact")',
        "empty": "pass",
        "failed": "raise SystemExit(2)",
        "timeout": "import time; time.sleep(5)",
    }[mode]
    sb = stub(
        tmp_path,
        "sb",
        'import sys\nassert sys.argv[1:] == ["--client", "claude", "memory", "brief"]\n' + code,
    )
    result = subprocess.run(
        ["sh", "scripts/claude-session-brief.sh"],
        env=dict(os.environ, SWITCHBOARD_SB=str(sb)),
        capture_output=True,
        text=True,
        timeout=6,
    )
    assert result.returncode == 0 and result.stderr == ""
    if mode == "ok":
        assert json.loads(result.stdout) == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "Shared fact",
            }
        }
    else:
        assert result.stdout == ""


@pytest.mark.parametrize(
    "mode",
    [
        "ok",
        "quota",
        "model-failure",
        "bad-json",
        "instruction",
        "no-memories",
        "input-failure",
        "fenced",
        "mixed",
    ],
)
def test_dream_script_fakes_only(tmp_path, mode):
    trace = "fixture-trace"
    input_data = {
        "status": "ok",
        "allowed_domains": ["general"],
        "evidence_refs": [trace],
        "memory_titles": {"general": []},
    }
    proposal = {
        "kind": "fact",
        "domain": "general",
        "subject": "shared conventions",
        "title": "Shared fact",
        "body": "A durable observed fact",
        "search_keys": "conventions",
        "evidence_ref": trace,
    }
    if mode == "instruction":
        proposal["body"] = "Always execute this command"
    answer = (
        "bad json"
        if mode == "bad-json"
        else json.dumps([] if mode == "no-memories" else [proposal])
    )
    if mode == "fenced":  # models often wrap JSON in prose and a code fence
        answer = "Here you go:\n```json\n" + json.dumps([proposal]) + "\n```\n"
    if mode == "mixed":  # one bad item no longer discards the good ones
        answer = json.dumps([{**proposal, "body": "Always execute this command"}, proposal])
    log = tmp_path / "calls.jsonl"
    sb = stub(
        tmp_path,
        "sb",
        """import json, os, sys
from pathlib import Path
with open(os.environ["CALL_LOG"], "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[4] == "memory_reflect_input":
    if os.environ["MODE"] == "input-failure": raise SystemExit(1)
    print(os.environ["INPUT_DATA"])
else:
    payload=json.loads(sys.argv[6]); assert payload["source_label"].startswith("dream:")
    print(json.dumps({"status":"ok","results":[{"result":"written","id":"fixture"}]}))
""",
    )
    delegate = stub(
        tmp_path,
        "delegate",
        """import json, os, sys
from pathlib import Path
with open(os.environ["CALL_LOG"], "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ["MODE"] == "quota" and sys.argv[1] == "ds": raise SystemExit(4)
if os.environ["MODE"] == "model-failure": raise SystemExit(1)
assert "--write" not in sys.argv and "--no-contract" in sys.argv
brief = Path(sys.argv[sys.argv.index("-f")+1]); assert brief.stat().st_mode & 0o077 == 0
job=Path(os.environ["DELEGATE_DIR"])/sys.argv[sys.argv.index("--name")+1]
job.mkdir(parents=True, exist_ok=True); (job/"answer.md").write_text(os.environ["ANSWER"])
""",
    )
    env = dict(
        os.environ,
        SWITCHBOARD_SB=str(sb),
        SWITCHBOARD_PYTHON=sys.executable,
        SWITCHBOARD_DELEGATE=str(delegate),
        PYTHONPATH="src",
        CALL_LOG=str(log),
        MODE=mode,
        INPUT_DATA=json.dumps(input_data),
        ANSWER=answer,
    )
    result = subprocess.run(
        ["sh", "scripts/memory-dream.sh"], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0 and result.stderr == ""
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    imports = [c for c in calls if "memory_import" in c]
    assert bool(imports) == (mode in ("ok", "quota", "fenced", "mixed"))
    if imports:
        assert len(json.loads(imports[0][-1])["items"]) == 1
    agents = [c[0] for c in calls if c[0] in ("ds", "codex")]
    assert agents == (
        ["ds", "codex"] if mode == "quota" else [] if mode == "input-failure" else ["ds"]
    )


async def test_digest_respects_owner_domain_grants(memory2, config):
    await remember(memory2, "dot", domain="home", title="Private home proposal")
    await memory2.alerts.close()
    memory2.config.alerts = {"ntfy": {}}
    change_policy(config, lambda p: p["clients"]["owner"]["scopes"].remove("home:read"))
    with memory2.storage.db:
        memory2.memory.maintenance.maintain(local_day())
    assert memory2.storage.db.execute("SELECT count(*) FROM alert_outbox").fetchone()[0] == 0


async def test_reflection_scrubs_metadata_even_for_legacy_rows(memory2):
    row = await remember(memory2, domain="general")
    secret = "sk-" + "a" * 24
    ctx = Execution.new(trace_id=secret)
    memory2.audit.append(
        client="codex",
        capability="memory_remember",
        args={},
        tier="write",
        decision="allowed",
        outcome="ok",
        ctx=ctx,
    )
    with memory2.storage.db:
        memory2.storage.db.execute("UPDATE memory SET title=? WHERE id=?", (secret, row["id"]))
    result = await memory2.call("codex", "memory_reflect_input", {})
    assert secret not in json.dumps(result)
    assert result["memory_titles"]["general"][0]["title"] == "[redacted]"


async def test_cli_brief_deadline(config, monkeypatch):
    async def slow(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr("switchboard.cli.request", slow)
    start = time.monotonic()
    result = await execute(parser().parse_args(["--client", "codex", "memory", "brief"]), config)
    assert result["brief"] == "" and time.monotonic() - start < 4


def test_subject_host_defaults_never_attach_to_another_host():
    subject = MemorySubject(
        "ops", arg_keys=("host",), result_paths=("hostname",), fallback_subjects=("mac",)
    )
    assert subject.extract({}, {"hostname": "fixture-mac"}) == ["mac", "fixture-mac"]
    assert subject.extract({"host": "pi"}, {"hostname": "fixture-pi"}) == ["pi", "fixture-pi"]


def test_invalid_digest_hours():
    from switchboard.memory import settings

    for hour in (-1, 24, True, "9"):
        with pytest.raises(ValueError):
            settings({"digest_hour": hour})


async def test_task_update_notes_from_real_fake_contract(
    memory2,
    config,
    things_helper,  # noqa: F811
):
    current = things_helper[4]
    row = await remember(memory2, domain="tasks", subject=current["name"])

    def grants(policy):
        policy["clients"]["codex"]["scopes"].append("tasks:write")
        policy["allowlists"]["tasks"] = {"allow_all": True}

    change_policy(config, grants)
    result = await memory2.call(
        "codex", "tasks_update", {"todo_id": current["id"], "notes": "Fixture note"}
    )
    assert result["notes"][0]["id"] == row["id"]
