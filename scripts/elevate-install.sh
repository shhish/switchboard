#!/bin/bash
# One-time install of the root helper (native/elevate).
#
# Normally nobody runs this by hand: when the Server app pairs, Switchboard opens macOS's own
# password box (scripts/elevate-setup.sh), whose message names the iPhone and its code, and
# runs this as root with --owner and --expect once the owner types their password.
#
#   sudo scripts/elevate-install.sh                  # by hand: shows the code and asks
#   sudo scripts/elevate-install.sh --reset-keys     # lost phone: pin the newly paired one
#   sudo scripts/elevate-install.sh --uninstall
#
# The phone's public key waits in ~/.local/state/switchboard-elevate/pending-key.json. After
# this, the helper updates itself, its recipes and its keys only through requests the phone
# signs with Face ID.
set -euo pipefail

LABEL=com.switchboard.elevate
BIN=/Library/PrivilegedHelperTools/$LABEL
SUPPORT="/Library/Application Support/SwitchboardElevate"
PLIST=/Library/LaunchDaemons/$LABEL.plist
HERE="$(cd "$(dirname "$0")/.." && pwd)"

MODE="" EXPECT="" OWNER="${SUDO_USER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall | --reset-keys) MODE="$1" ;;
    --owner) OWNER="$2"; shift ;;
    --expect) EXPECT="$2"; shift ;;
    *) echo "Unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
# The password box keeps the owner's HOME and TMPDIR; root's files never go there.
export HOME=/var/root TMPDIR=/private/tmp
[ -n "$OWNER" ] && [ "$OWNER" != root ] || { echo "Run with sudo from the owner's account." >&2; exit 1; }
OWNER_UID="$(id -u "$OWNER")"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"

if [ "$MODE" = --uninstall ]; then
  launchctl bootout system "$PLIST" 2>/dev/null || true
  rm -f "$PLIST" "$BIN" /var/run/$LABEL.sock
  rm -rf "$SUPPORT"
  echo "Removed the root helper."
  exit 0
fi

PENDING="$OWNER_HOME/.local/state/switchboard-elevate/pending-key.json"
KEYS_FILE="$SUPPORT/keys.json"
[ "$MODE" = --reset-keys ] && rm -f "$KEYS_FILE"
if [ ! -s "$KEYS_FILE" ] || ! grep -q x963 "$KEYS_FILE"; then
  [ -f "$PENDING" ] || { echo "No paired iPhone yet. In the Server app: Account > Mac admin > Pair this iPhone." >&2; exit 1; }
  # Parsed with the system's own tools; nothing from the owner's account is executed as root.
  NAME="$(/usr/bin/plutil -extract name raw -o - "$PENDING")"
  X963="$(/usr/bin/plutil -extract x963 raw -o - "$PENDING")"
  [ "$(printf %s "$X963" | /usr/bin/base64 -d | wc -c | tr -d ' ')" = 65 ] || { echo "Pending key is not a P-256 public key." >&2; exit 1; }
  CODE="$(printf %s "$X963" | /usr/bin/base64 -d | /usr/bin/shasum -a 256 | cut -c1-16 | sed 's/..../& /g; s/ $//')"
  if [ -n "$EXPECT" ]; then
    # The owner saw this code in the password box; pin only that exact key.
    [ "$CODE" = "$EXPECT" ] || { echo "Pending key changed since the password box; not paired." >&2; exit 1; }
  else
    echo
    echo "  Pairing \"$NAME\""
    echo "  Code: $CODE"
    echo
    read -r -p "Does the Server app show the same code? [y/N] " answer
    case "$answer" in y|Y|yes) ;; *) echo "Not paired."; exit 1 ;; esac
  fi
fi

echo "Building the helper..."
# Built as root in a root-only folder, so nothing running as the owner can swap the source
# or the binary between compiling and installing it.
# In sticky /private/tmp nobody else can rename or replace this root-owned folder.
BUILD="$(mktemp -d /private/tmp/elevate-build.XXXXXX)"
trap 'rm -rf "$BUILD"' EXIT
chown root:wheel "$BUILD"
chmod 0700 "$BUILD"
install -o root -g wheel -m 0600 "$HERE/native/elevate/main.swift" "$BUILD/main.swift"
install -o root -g wheel -m 0644 "$HERE/native/elevate/recipes.json" "$BUILD/recipes.json"
/usr/bin/xcrun swiftc -O -swift-version 5 -module-cache-path "$BUILD/cache" -o "$BUILD/helper" "$BUILD/main.swift"

launchctl bootout system "$PLIST" 2>/dev/null || true
install -d -o root -g wheel -m 0755 /Library/PrivilegedHelperTools "$SUPPORT"
install -d -o root -g wheel -m 0700 "$SUPPORT/results"
install -o root -g wheel -m 0755 "$BUILD/helper" "$BIN"
[ -f "$SUPPORT/recipes.json" ] || install -o root -g wheel -m 0644 "$BUILD/recipes.json" "$SUPPORT/recipes.json"
if [ ! -f "$SUPPORT/config.json" ]; then
  printf '{"owner_uid": %s, "machine": "%s"}\n' "$OWNER_UID" "$(uuidgen | tr -d - | tr A-F a-f)" > "$SUPPORT/config.json"
  chmod 0644 "$SUPPORT/config.json"
fi
if [ -n "${X963:-}" ]; then
  printf '{"keys": [{"name": "%s", "x963": "%s"}]}\n' "$(printf %s "$NAME" | tr -cd 'A-Za-z0-9 ._-' | cut -c1-40)" "$X963" > "$SUPPORT/keys.json"
  chmod 0644 "$SUPPORT/keys.json"
fi
chown -R root:wheel "$SUPPORT"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Standard</string>
</dict>
</plist>
PLIST
chown root:wheel "$PLIST"
chmod 0644 "$PLIST"
launchctl bootstrap system "$PLIST"
sleep 1
if [ -S "/var/run/$LABEL.sock" ]; then
  echo "Root helper running. Approvals from the Server app now run here."
else
  echo "Helper did not start; see /Library/Logs/SwitchboardElevate.log" >&2
  exit 1
fi
