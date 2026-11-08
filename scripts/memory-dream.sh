#!/bin/sh
# Owner-installed nightly reflection. Models run here, never in the capability service.
umask 077
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit 0
SB=${SWITCHBOARD_SB:-$HOME/.local/share/switchboard/current/.venv/bin/sb}
PYTHON=${SWITCHBOARD_PYTHON:-$(dirname -- "$SB")/python}
if [ ! -x "$PYTHON" ]; then
  printf '%s\n' 'memory-dream: Python unavailable; skipped'
  exit 0
fi
"$PYTHON" "$SCRIPT_DIR/memory-dream.py" || printf '%s\n' 'memory-dream: runner unavailable; skipped'
exit 0
