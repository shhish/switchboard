"""Stop hook: an agent may not stop with a card it claimed this session left unupdated.

Claude Code and Codex both call this with {"transcript_path", "session_id", "stop_hook_active"}
on stdin. It reads only this session's transcript: a card moved to In progress (board_move,
over MCP or `sb call`) with no later board_comment, board_move or board_ask for that card gets
one reminder per claim, as {"decision": "block", "reason": ...}. Anything unexpected exits 0
silently, so a broken hook never traps a session. Runs on the system Python 3.9.
"""

import json
import os
import re
import sys
from pathlib import Path

CLAIM = "board_move"
UPDATES = {"board_move", "board_comment", "board_ask"}
OUTPUTS = {"tool_result", "function_call_output", "custom_tool_call_output"}
CARD = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?([A-Za-z0-9_.-]+)#([0-9]+)$")
# Only a command that runs sb counts: the word sb (optionally a path, then --client X) at the
# start of a shell word, not text that merely mentions a call inside a string.
SB_CALL = re.compile(
    r"(?:^|[\s;&|(])(?:[\w.~/$-]*/)?sb\s+(?:--client[\s=]\S+\s+)?"
    r"call\s+(board_(?:move|comment|ask))\s+--json\s+'(\{[^']*\})'",
    re.M,
)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.M | re.S)
STATE = Path(os.environ.get("SWITCHBOARD_WRAPUP_STATE", "~/.local/state/switchboard/wrapup"))


def card_ref(value):
    match = CARD.match(str(value or "").strip())
    return f"{match.group(1).lower()}#{match.group(2)}" if match else None


def as_args(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def calls(node):
    """Yield (tool, args) for every board call recorded in one transcript line."""
    if isinstance(node, list):
        for item in node:
            yield from calls(item)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    kind = kind if isinstance(kind, str) else None
    if kind == "tool_use":  # Claude Code
        name = str(node.get("name") or "")
        tool = name.rsplit("__", 1)[-1]
        if tool in UPDATES:
            yield tool, as_args(node.get("input"))
        elif name == "Bash":
            yield from shell(as_args(node.get("input")).get("command"))
        return
    if kind == "McpToolCall":  # Codex
        tool = str(node.get("tool") or "").rsplit("__", 1)[-1]
        if tool in UPDATES:
            yield tool, as_args(node.get("arguments"))
        return
    if kind == "function_call":  # Codex direct tool call
        tool = str(node.get("name") or "").rsplit("__", 1)[-1]
        if tool in UPDATES:
            yield tool, as_args(node.get("arguments"))
        return
    if kind == "CommandExecution":  # Codex shell
        command = node.get("command")
        yield from shell(" ".join(command) if isinstance(command, list) else command)
        return
    if kind in OUTPUTS:  # what a tool printed is not a call
        return
    for value in node.values():
        yield from calls(value)


def shell(command):
    if not isinstance(command, str) or "board_" not in command:
        return
    for tool, raw in SB_CALL.findall(HEREDOC.sub(" ", command)):
        yield tool, as_args(raw)


def scan(path, seen):
    """Read the transcript from where the last stop left off; returns the updated state."""
    size = path.stat().st_size
    if not isinstance(seen.get("offset"), int) or seen["offset"] > size:
        seen = {"reminded": seen.get("reminded") or {}}  # new or rewritten transcript
    claimed, updated = seen.get("claimed") or {}, seen.get("updated") or {}
    order = int(seen.get("order") or 0)
    with open(path, "rb") as handle:
        handle.seek(seen.get("offset") or 0)
        offset = handle.tell()
        for raw in handle:
            if not raw.endswith(b"\n"):
                break  # a line still being written; read it next time
            offset += len(raw)
            if b"board_" not in raw:
                continue
            try:
                record = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            for tool, args in calls(record):
                ref = card_ref(args.get("card"))
                if not ref:
                    continue
                order += 1
                if tool == CLAIM and args.get("column") == "In progress":
                    claimed[ref] = order
                else:
                    updated[ref] = order
    return {**seen, "offset": offset, "order": order, "claimed": claimed, "updated": updated}


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("stop_hook_active"):
        return 0
    path = Path(str(event.get("transcript_path") or "")).expanduser()
    session = re.sub(r"[^A-Za-z0-9_-]", "", str(event.get("session_id") or ""))[:80]
    if not session or not path.is_file():
        return 0
    marker = STATE.expanduser() / f"{session}.json"
    try:
        seen = json.loads(marker.read_text())
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    state = scan(path, seen)
    claimed, updated = state["claimed"], state["updated"]
    reminded = state.setdefault("reminded", {})
    due = sorted(
        ref for ref, at in claimed.items() if updated.get(ref, 0) < at and reminded.get(ref) != at
    )
    for ref in due:
        reminded[ref] = claimed[ref]
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, marker)
    if not due:
        return 0
    cards = ", ".join(due)
    reason = (
        f"Before you stop: you moved {cards} to In progress in this session and haven't "
        "updated it since. Post one board_comment the owner can read without this transcript "
        "(done, blocked or needs you; what changed; the one next owner action, if any), then "
        "board_move it: Done when finished, Up next when pausing, Needs you only for something "
        "the owner must physically do. If you're mid-task waiting on the owner, a short progress "
        "comment is enough. This reminder shows once per claim."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def safe_main():
    try:
        return main()
    except Exception:  # a broken hook must never trap a session
        return 0


if __name__ == "__main__":
    sys.exit(safe_main())
