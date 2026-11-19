#!/bin/sh
# Owner-installed Claude Code SessionStart hook. No settings are changed by this file.
exec /usr/bin/python3 "$(dirname "$0")/session_brief.py" "${SWITCHBOARD_SB:-$HOME/.local/share/switchboard/current/.venv/bin/sb}"
