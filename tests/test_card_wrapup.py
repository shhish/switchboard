import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/card_wrapup.py"


def claude_call(tool, **args):
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": f"mcp__switchboard__{tool}", "input": args}]
        },
    }


def claude_bash(command):
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]
        },
    }


def claude_result(text):
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "content": [{"type": "text", "text": text}]}]
        },
    }


def codex_call(tool, **args):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "McpToolCall",
                "server": "switchboard",
                "tool": tool,
                "arguments": args,
            },
        },
    }


def run(tmp_path, records, *, session="s1", active=False, transcript=None):
    path = transcript or tmp_path / "t.jsonl"
    if transcript is None:
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
    event = {"transcript_path": str(path), "session_id": session, "stop_hook_active": active}
    out = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={"SWITCHBOARD_WRAPUP_STATE": str(tmp_path / "state"), "PATH": "/usr/bin:/bin"},
        timeout=10,
        check=True,
    )
    return json.loads(out.stdout) if out.stdout.strip() else None


def test_claimed_card_without_update_blocks_once(tmp_path):
    records = [claude_call("board_move", card="demo#4", column="In progress")]
    first = run(tmp_path, records)
    assert first["decision"] == "block"
    assert "demo#4" in first["reason"]
    assert run(tmp_path, records) is None  # one reminder per claim


def test_comment_or_move_after_claim_is_enough(tmp_path):
    claim = claude_call("board_move", card="demo#4", column="In progress")
    for update in (
        claude_call("board_comment", card="demo#4", body="Done"),
        claude_call("board_move", card="demo#4", column="Done"),
        claude_call("board_ask", card="acme/Demo#4", prompt="Ship?"),
    ):
        assert (
            run(tmp_path, [claim, update], session=update["message"]["content"][0]["name"]) is None
        )


def test_reclaim_after_update_needs_a_new_update(tmp_path):
    records = [
        claude_call("board_move", card="demo#4", column="In progress"),
        claude_call("board_comment", card="demo#4", body="paused"),
        claude_call("board_move", card="demo#4", column="In progress"),
    ]
    assert run(tmp_path, records)["decision"] == "block"


def test_stop_hook_active_and_other_cards_never_block(tmp_path):
    records = [claude_call("board_move", card="demo#4", column="In progress")]
    assert run(tmp_path, records, active=True) is None
    other = [
        claude_call("board_move", card="demo#4", column="In progress"),
        claude_call("board_comment", card="demo#5", body="unrelated"),
    ]
    assert run(tmp_path, other, session="s2")["reason"].count("demo#4") == 1


def test_sb_cli_and_codex_calls_count(tmp_path):
    cli = [
        claude_bash(
            "sb --client claude call board_move "
            """--json '{"card":"demo#7","column":"In progress"}'"""
        ),
    ]
    assert "demo#7" in run(tmp_path, cli)["reason"]
    codex = [
        codex_call("board_move", card="demo#8", column="In progress"),
        codex_call("board_comment", card="demo#8", body="done"),
        codex_call("board_move", card="demo#9", column="In progress"),
    ]
    reason = run(tmp_path, codex, session="codex")["reason"]
    assert "demo#9" in reason and "demo#8" not in reason


def test_tool_output_mentioning_calls_is_ignored(tmp_path):
    records = [claude_result('board_move {"card":"demo#4","column":"In progress"}')]
    assert run(tmp_path, records) is None


def test_missing_or_bad_input_exits_quietly(tmp_path):
    assert run(tmp_path, [], transcript=tmp_path / "missing.jsonl") is None
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json board_move\n{\n")
    assert run(tmp_path, [], transcript=bad) is None
    out = subprocess.run(
        [sys.executable, str(SCRIPT)], input="nope", capture_output=True, text=True, check=True
    )
    assert out.stdout == ""


def test_odd_records_and_incremental_reads(tmp_path):
    path = tmp_path / "t.jsonl"
    schema = {"type": "x", "tools": [{"type": {"type": "object"}, "board_move": 1}]}
    claim = claude_call("board_move", card="demo#4", column="In progress")
    path.write_text(json.dumps(schema) + "\n" + json.dumps(claim) + "\n")
    assert "demo#4" in run(tmp_path, [], transcript=path)["reason"]
    state = json.loads((tmp_path / "state/s1.json").read_text())
    assert state["offset"] == path.stat().st_size
    with path.open("a") as handle:  # a later turn: comment, plus a half-written line
        handle.write(json.dumps(claude_call("board_comment", card="demo#4", body="ok")) + "\n")
        handle.write('{"partial": "board_')
    assert run(tmp_path, [], transcript=path) is None
    with path.open("a") as handle:
        handle.write(
            'move"}\n'
            + json.dumps(claude_call("board_move", card="demo#6", column="In progress"))
            + "\n"
        )
    assert "demo#6" in run(tmp_path, [], transcript=path)["reason"]


def test_shell_text_that_only_mentions_a_call_is_ignored(tmp_path):
    args = "--json '" + json.dumps({"card": "demo#7", "column": "In progress"}) + "'"
    call = "sb --client claude call board_move " + args
    commands = (
        "python3 - <<'EOF'\ns = '" + call + "'\nEOF\necho done",  # a file edit in a heredoc
        "cat > notes.txt <<EOF\n" + call + "\nEOF",
        'echo "' + call + '"',  # quoted text
        "note=" + json.dumps(call),
    )
    for n, command in enumerate(commands):
        assert run(tmp_path, [claude_bash(command)], session=f"m{n}") is None, command
    real = "cd /tmp && ~/.local/bin/" + call
    assert "demo#7" in run(tmp_path, [claude_bash(real)], session="real")["reason"]
