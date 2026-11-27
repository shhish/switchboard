#!/bin/sh
# Codex SessionStart hook (~/.codex/hooks.json): the same board and memory brief as Claude's,
# read with Codex's own Switchboard credential. Codex asks the owner to trust it once (/hooks).
SWITCHBOARD_BRIEF_CLIENT=codex exec /usr/bin/python3 "$(dirname "$0")/session_brief.py" "${SWITCHBOARD_SB:-$HOME/.local/share/switchboard/current/.venv/bin/sb}"
