#!/bin/sh
# Owner-installed Claude Code SessionStart hook. No settings are changed by this file.
exec /usr/bin/python3 - "${SWITCHBOARD_SB:-$HOME/.local/share/switchboard/current/.venv/bin/sb}" <<'PY'
import json
import subprocess
import sys

try:
    result = subprocess.run(
        [sys.argv[1], "--client", "claude", "memory", "brief"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False,
    )
    brief = result.stdout.strip()[:6000]
    if result.returncode == 0 and brief:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": brief,
        }}))
except Exception:
    pass
PY
