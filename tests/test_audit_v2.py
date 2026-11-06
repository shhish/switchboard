"""Audit v2: typed rows, causal context, safe arguments, hash chain, migration, tools."""

import json
import string
import time

from switchboard.audit import GENESIS, Audit, safe_args
from switchboard.execution import Execution
from switchboard.protocol import CAPABILITIES_KEY, MODERN_VERSION, VERSION_KEY, Protocol
from switchboard.storage import Storage
from tests.conftest import change_policy

# Fake credentials are built at runtime so the publish secret scan never sees a literal.
FAKE_HEX = string.hexdigits[:16]

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def grant(config, client, *scopes):
    change_policy(config, lambda d: d["clients"][client]["scopes"].extend(scopes))


def modern(method, params, meta=None):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **params,
            "_meta": {VERSION_KEY: MODERN_VERSION, CAPABILITIES_KEY: {}, **(meta or {})},
        },
    }


async def test_mcp_meta_becomes_correlation_context(core):
    meta = {
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
        "switchboard/session": "sess-42",
        "switchboard/turn": "turn-7",
        "io.modelcontextprotocol/clientInfo": {"name": "chatgpt", "version": "2026.11"},
    }
    message = modern("tools/call", {"name": "media_library_status", "arguments": {}}, meta)
    await Protocol(core).handle(message, "dot", MODERN_VERSION)
    row = core.audit.tail(1)[0]
    assert row["trace_id"] == TRACE and row["parent_span_id"] == "00f067aa0ba902b7"
    assert row["session_key"] == "sess-42" and row["turn_key"] == "turn-7"
    assert row["client_name"] == "chatgpt" and row["client_version"] == "2026.11"
    assert row["episode_id"] == "s:dot:sess-42"
    assert row["catalog_hash"] == core.catalog_version
    assert row["client"] == "dot"  # identity still comes from the token, never _meta


async def test_hostile_meta_is_ignored():
    ctx = Execution.from_meta(
        {
            "traceparent": "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
            "switchboard/session": "x" * 500,
            "switchboard/turn": "bad value\n",
            "io.modelcontextprotocol/clientInfo": "not-a-dict",
        }
    )
    assert ctx.trace_id != "0" * 32 and len(ctx.trace_id) == 32
    assert ctx.session_key is None and ctx.turn_key is None and ctx.client_name is None


def test_safe_args_keeps_ids_hashes_text_and_redacts_secrets():
    stored = safe_args(
        {
            "service": "sonarr",
            "job_id": "job_" + "a" * 32,
            "uuid": "550E8400-E29B-41D4-A716-446655440000",
            "reason": "x" * 500,
            "note": "use sk-abcdefghijklmnopqrstuvwxyz123456 here",
            "blob": "Ab3" * 20,
            "body": "short",
            "count": 3,
        },
        hash_fields=frozenset({"body"}),
    )
    assert stored["service"] == "sonarr" and stored["count"] == 3
    assert stored["job_id"].startswith("job_") and stored["uuid"].startswith("550E")
    assert stored["reason"]["len"] == 500 and "x" not in json.dumps(stored["reason"])
    assert stored["note"] == stored["blob"] == "[REDACTED:secret]"
    assert stored["body"] == {"len": 5, "sha256": stored["body"]["sha256"]}


async def test_secret_in_free_text_never_reaches_disk(core):
    await core.call("dot", "media_search", {"query": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"})
    assert "ghp_" not in json.dumps(core.audit.tail()) + core.audit.path.read_text()


async def test_error_and_deny_codes_are_typed(core):
    await core.call("agy", "media_library_status", {})
    row = core.audit.tail(1)[0]
    assert row["decision"] == "denied" and row["error_code"] == "scope_denied"
    assert row["deny_code"] == "scope_denied"
    await core.call("dot", "no_such_tool", {})
    assert core.audit.tail(1)[0]["error_code"] == "unknown_capability"


async def test_chain_verifies_and_detects_tampering(core):
    for _ in range(3):
        await core.call("dot", "media_library_status", {})
    assert core.audit.verify()["ok"]
    with core.storage.db:
        core.storage.db.execute(
            "UPDATE audit_log SET outcome='tampered' WHERE seq=(SELECT MIN(seq) FROM audit_log)"
        )
    result = core.audit.verify()
    assert not result["ok"] and result["broken_at_seq"] >= 1


async def test_episodes_group_quick_calls(core):
    await core.call("dot", "media_library_status", {})
    await core.call("dot", "media_library_status", {})
    await core.call("codex", "media_library_status", {})
    rows = core.audit.tail(3)
    assert rows[0]["episode_id"] == rows[1]["episode_id"] != rows[2]["episode_id"]


def test_legacy_rows_migrate_once_into_a_valid_chain(config):
    storage = Storage(config.state_dir)
    with storage.db:
        storage.db.execute(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL)"
        )
        for record in (
            {
                "ts": "2026-11-01T10:00:00+00:00",
                "client": "dot",
                "capability": "media_search",
                "args": {"query": "dune", "note": "token=" + FAKE_HEX[4:] + FAKE_HEX[:4]},
                "decision": "allowed",
                "outcome": "ok",
                "duration": 0.25,
            },
            {"ts": "not-a-date"},
            {
                "ts": "2026-11-01T10:01:00+00:00",
                "client": "orb",
                "capability": "orb_show",
                "args": "[REDACTED]",
                "decision": "allowed",
                "outcome": "ok",
            },
        ):
            storage.db.execute("INSERT INTO audit(record) VALUES (?)", (json.dumps(record),))
    audit = Audit(storage, config.log_dir)
    rows = audit.tail(10)
    # The malformed record is kept (length and hash only), never silently dropped.
    assert [r["capability"] for r in rows] == ["media_search", "legacy_unparsed", "orb_show"]
    assert set(rows[1]["args"]) == {"len", "sha256"}
    rows = [rows[0], rows[2]]
    assert rows[0]["args"]["query"] == "dune" and rows[0]["args"]["note"] == "[REDACTED:secret]"
    assert rows[0]["duration_ms"] == 250 and rows[1]["args"] == "[REDACTED]"
    result = audit.verify()
    assert rows[0]["prev_hash"] == GENESIS and result["ok"] and result["checked"] == 3
    assert result["head"] == rows[1]["entry_hash"]
    tables = {r[0] for r in storage.db.execute("SELECT name FROM sqlite_master")}
    assert "audit" not in tables and "audit_v1" in tables  # renamed, never dropped
    Audit(storage, config.log_dir)  # second start: nothing left to migrate
    assert len(audit.tail(10)) == 3
    storage.close()


async def test_retention_rolls_up_prunes_and_chain_still_verifies(core, monkeypatch):
    for _ in range(4):
        await core.call("dot", "media_library_status", {})
    old = int((time.time() - 200 * 86400) * 1000)
    with core.storage.db:
        core.storage.db.execute("UPDATE monitor_meta SET value=0 WHERE key='audit_maintained_at'")
    # Re-hash two rows as if written 200 days ago so the chain stays honest.
    rows = core.storage.db.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
    from switchboard.audit import COLUMNS, canonical, digest

    prev = GENESIS
    with core.storage.db:
        for index, row in enumerate(rows):
            data = {c: row[c] for c in COLUMNS}
            if index < 2:
                data["ts_ms"] = old
            data["prev_hash"] = prev
            entry = digest(canonical(data) + prev)
            core.storage.db.execute(
                "UPDATE audit_log SET ts_ms=?, prev_hash=?, entry_hash=? WHERE seq=?",
                (data["ts_ms"], prev, entry, row["seq"]),
            )
            prev = entry
    assert core.audit.verify()["ok"]
    assert core.audit.maintain(now=time.time())
    remaining = core.storage.db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert remaining == len(rows) - 2
    rollup = core.storage.db.execute("SELECT SUM(n) FROM audit_rollup").fetchone()[0]
    assert rollup == 2
    assert core.audit.verify()["ok"]
    await core.call("dot", "media_library_status", {})
    assert core.audit.verify()["ok"]
    assert not core.audit.maintain(now=time.time())  # at most hourly


async def test_query_shows_own_rows_unless_read_all(core, config):
    grant(config, "dot", "audit:read")
    grant(config, "codex", "audit:read", "audit:read_all")
    await core.call("dot", "media_library_status", {})
    await core.call("codex", "media_library_status", {})
    own = await core.call("dot", "audit_query", {"capability": "media_library_status"})
    assert own["see_all"] is False and {r["client"] for r in own["rows"]} == {"dot"}
    everyone = await core.call("codex", "audit_query", {"capability": "media_library_status"})
    assert everyone["see_all"] and {r["client"] for r in everyone["rows"]} == {"dot", "codex"}
    filtered = await core.call("dot", "audit_query", {"client": "codex"})
    assert all(r["client"] == "dot" for r in filtered["rows"])
    denied = await core.call("agy", "audit_query", {})
    assert denied["error"]["code"] == "scope_denied"


async def test_query_by_argument_value(core, config):
    grant(config, "codex", "audit:read", "audit:read_all")
    await core.call("dot", "media_search", {"query": "dune"})
    await core.call("dot", "media_search", {"query": "alien"})
    found = await core.call(
        "codex", "audit_query", {"arg_key": "query", "arg_value": "dune", "hours": 1}
    )
    assert [r["args"]["query"] for r in found["rows"]] == ["dune"]


async def test_trace_links_approval_request_answer_and_execution(core, config, fake_approval):
    core.registry.add(fake_approval)
    grant(config, "codex", "audit:read", "audit:read_all")
    pending = await core.call("dot", "test.perform", {"value": "a"})
    approval_id = pending["approval_id"]
    core.resolve("owner", approval_id, True)
    await core.call("dot", "test.perform", {"value": "a", "approval_id": approval_id})
    chain = await core.call("codex", "audit_trace", {"ref": approval_id})
    names = [(r["client"], r["capability"]) for r in chain["rows"]]
    assert ("dot", "test_perform") in names and ("owner", "approvals_approve") in names
    assert [r["decision"] for r in chain["rows"] if r["capability"] == "test_perform"] == [
        "approval_required",
        "allowed",
    ]
    missing = await core.call("codex", "audit_trace", {"ref": "nothing-matches"})
    assert missing["error"]["code"] == "not_found"


async def test_digest_reports_loops_errors_and_clients(core, config):
    grant(config, "codex", "audit:read", "audit:read_all")
    for _ in range(6):
        await core.call("dot", "media_search", {"query": "same"})
    await core.call("agy", "media_library_status", {})
    report = await core.call("codex", "audit_digest", {"hours": 1})
    assert any(
        loop["client"] == "dot" and loop["capability"] == "media_search" and loop["n"] >= 6
        for loop in report["loops"]
    )
    assert any(e["code"] == "scope_denied" and e["client"] == "agy" for e in report["errors"])
    assert {c["client"] for c in report["clients"]} >= {"dot", "agy"}
    assert report["catalog_changes"][0]["catalog_hash"] == core.catalog_version


async def test_cli_headers_carry_session(server, config, monkeypatch):
    from switchboard.cli import correlation_headers

    monkeypatch.setenv("SWITCHBOARD_SESSION", "delegate-job-1")
    monkeypatch.setenv("SWITCHBOARD_TURN", "bad\nvalue")
    assert correlation_headers() == {"X-Switchboard-Session": "delegate-job-1"}
    core, url, session = server
    from tests.conftest import bearer

    await session.post(
        url + "/v1/call/media_library_status",
        json={},
        headers={**bearer(config, "codex"), "X-Switchboard-Session": "delegate-job-1"},
    )
    row = core.audit.tail(1)[0]
    assert row["session_key"] == "delegate-job-1" and row["origin"] == "agent"
    verify = await session.get(url + "/v1/audit/verify", headers=bearer(config, "owner"))
    assert (await verify.json())["ok"] is True


def test_own_tokens_and_secret_keys_are_redacted():
    token = FAKE_HEX * 4  # Switchboard's own 64-hex bearer format
    stored = safe_args({"note": f"token is {token}", token: "x", "trace": "4bf92f3577b34da6"})
    assert stored["note"] == "[REDACTED:secret]" and "[REDACTED_KEY]" in stored
    assert stored["trace"] == "4bf92f3577b34da6"
    assert token not in json.dumps(stored)


async def test_caller_supplied_columns_are_scanned(core):
    token = FAKE_HEX * 4
    ctx = Execution.from_meta(
        {
            "switchboard/session": token,
            "io.modelcontextprotocol/clientInfo": {"name": "c", "version": "1"},
        }
    )
    await core.call("dot", token, {}, ctx)
    record = core.audit.tail(1)[0]
    assert token not in json.dumps(record) + core.audit.path.read_text()
    assert record["capability"] == "[REDACTED:secret]"


async def test_asserted_trace_cannot_splice_into_another_clients_chain(core, config):
    grant(config, "codex", "audit:read", "audit:read_all")
    await core.call("codex", "media_library_status", {})
    victim = core.audit.tail(1)[0]
    # A hostile client reuses the victim's trace and names it as the cause.
    meta = {
        "traceparent": f"00-{victim['trace_id']}-00f067aa0ba902b7-01",
        "switchboard/causation": victim["id"],
    }
    await core.call("dot", "media_search", {"query": "x"}, Execution.from_meta(meta))
    hostile = core.audit.tail(1)[0]
    chain = await core.call("codex", "audit_trace", {"ref": victim["id"]})
    assert {r["client"] for r in chain["rows"]} == {"codex"}
    own = await core.call("codex", "audit_trace", {"ref": hostile["id"]})
    assert {r["client"] for r in own["rows"]} == {"dot"}


async def test_deleting_anchored_history_is_detected(core):
    for _ in range(3):
        await core.call("dot", "media_library_status", {})
    tomorrow = time.time() + 86400
    assert core.audit.maintain(now=tomorrow)  # anchors "today" as a finished day
    assert core.audit.verify()["ok"]
    with core.storage.db:
        core.storage.db.execute("DELETE FROM audit_log WHERE seq=(SELECT MAX(seq) FROM audit_log)")
    assert not core.audit.verify()["ok"]


async def test_jsonl_rotates_on_first_write_of_a_new_day(core):
    from datetime import date, timedelta

    await core.call("dot", "media_library_status", {})
    yesterday = date.today() - timedelta(days=1)
    core.audit.jsonl_day = yesterday
    await core.call("dot", "media_library_status", {})
    rotated = core.audit.log_dir / f"audit-{yesterday.isoformat()}.jsonl"
    assert rotated.exists() and core.audit.path.read_text().count("\n") == 1


async def test_doctor_verifies_incrementally_from_checkpoint(core):
    from switchboard.doctor import audit_chain

    for _ in range(3):
        await core.call("dot", "media_library_status", {})
    full = core.audit.verify()
    await core.call("dot", "media_library_status", {})
    check = audit_chain(core.storage.db)
    assert check["status"] == "ok" and check["rows"] == 1 and full["checked"] >= 3
