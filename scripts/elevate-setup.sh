#!/bin/bash
# Opens macOS's own password box once, so the owner can turn on root commands without a
# terminal. Switchboard runs this (as the owner, never as root) when the Server app pairs and
# the root helper isn't installed yet. The box's message names the iPhone and its pairing
# code; the password goes to macOS, never to this script. On OK, elevate-install.sh runs as
# root and pins exactly that code's key.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/.local/state/switchboard-elevate"
PENDING="$STATE/pending-key.json"
LOCK="$STATE/setup.lock"
LOG="$HOME/Library/Logs/switchboard/elevate-setup.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "$(date -u +%FT%TZ) setup start"

[ -S /var/run/com.switchboard.elevate.sock ] && { echo "already installed"; exit 0; }
[ -f "$PENDING" ] || { echo "no pending key"; exit 1; }
# One password box at a time; a lock left by a killed run expires after 70 minutes.
if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +70 2>/dev/null)" ]; then rmdir "$LOCK"; fi
mkdir "$LOCK" 2>/dev/null || { echo "a password box is already open"; exit 0; }
trap 'rmdir "$LOCK"' EXIT

NAME="$(/usr/bin/plutil -extract name raw -o - "$PENDING" | tr -cd 'A-Za-z0-9 ._-' | cut -c1-40)"
X963="$(/usr/bin/plutil -extract x963 raw -o - "$PENDING")"
CODE="$(printf %s "$X963" | /usr/bin/base64 -d | /usr/bin/shasum -a 256 | cut -c1-16 | sed 's/..../& /g; s/ $//')"

if /usr/bin/osascript - "$HERE/elevate-install.sh" "$USER" "$CODE" "$NAME" <<'APPLESCRIPT'
on run argv
	set installer to item 1 of argv
	set owner to item 2 of argv
	set code to item 3 of argv
	set phone to item 4 of argv
	set message to "Server wants to turn on root commands, approved with Face ID on " & phone & " (code " & code & "). Type your password once to allow it."
	with timeout of 3600 seconds
		do shell script (quoted form of installer) & " --owner " & (quoted form of owner) & " --expect " & (quoted form of code) with prompt message with administrator privileges
	end timeout
end run
APPLESCRIPT
then
  echo "$(date -u +%FT%TZ) installed"
else
  echo "$(date -u +%FT%TZ) password box cancelled or install failed"
fi
