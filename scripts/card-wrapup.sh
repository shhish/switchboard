#!/bin/sh
# Stop hook for Claude Code (~/.claude/settings.json) and Codex (~/.codex/hooks.json): before
# an agent stops, a card it claimed this session must have a comment or a move. Reads only the
# session transcript the agent names on stdin; never calls Switchboard and never fails a stop.
exec /usr/bin/python3 "$(dirname "$0")/card_wrapup.py"
