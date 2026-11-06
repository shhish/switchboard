#!/bin/sh
set -eu

# shellcheck source=/dev/null
. "$(dirname -- "$0")/deploy-env.sh"
REPO=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PACKAGE="$REPO/native/SwitchboardEventKit"
APP="$REPO/native/build/Switchboard EventKit.app"
IDENTITY=${SWITCHBOARD_SIGN_IDENTITY:--}
if [ "$IDENTITY" = "-" ]; then
    echo 'Warning: ad-hoc re-signs invalidate the TCC grant. Use a stable SWITCHBOARD_SIGN_IDENTITY for deployment.' >&2
fi
# Keep SwiftPM caches and all build outputs inside this worktree.
mkdir -p "$REPO/native/build/cache" "$REPO/native/build/module-cache"
export CLANG_MODULE_CACHE_PATH="$REPO/native/build/module-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$CLANG_MODULE_CACHE_PATH"
swift build --package-path "$PACKAGE" --cache-path "$REPO/native/build/cache" --disable-sandbox -c release
BIN=$(swift build --package-path "$PACKAGE" --cache-path "$REPO/native/build/cache" --disable-sandbox -c release --show-bin-path)
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# Package a fixed compiled script. Never generate source from request data.
/usr/bin/osacompile -o "$APP/Contents/Resources/Things.scpt" "$PACKAGE/Sources/EventKitBridge/Resources/Things.applescript"
cp -R "$BIN/SwitchboardEventKit_EventKitBridge.bundle" "$APP/Contents/Resources/"
cp "$BIN/SwitchboardEventKit" "$APP/Contents/MacOS/SwitchboardEventKit"
cp "$PACKAGE/Info.plist" "$APP/Contents/Info.plist"
if [ -n "${SWITCHBOARD_BUNDLE_ID:-}" ]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $SWITCHBOARD_BUNDLE_ID" "$APP/Contents/Info.plist"
fi
/usr/bin/codesign --force --options runtime --entitlements "$PACKAGE/Entitlements.plist" --sign "$IDENTITY" "$APP"
/usr/bin/codesign --verify --strict "$APP"
printf '%s\n' "$APP"
