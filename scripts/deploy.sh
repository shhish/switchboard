#!/bin/bash
# Deploy a committed Switchboard revision as an immutable release, then switch to it.
#
#   scripts/deploy.sh             deploy HEAD of the current checkout (must be clean, tests must pass)
#   scripts/deploy.sh --rollback  switch back to the previous release
#   scripts/deploy.sh --status    show the current and available releases
#
# Releases live in ~/.local/share/switchboard/releases/<sha>. "current" is a symlink.
# The LaunchAgent, every MCP client registration and the skill symlinks point at
# current/, so agents editing a checkout never change what is running.
set -euo pipefail

# shellcheck source=/dev/null
. "$(dirname -- "$0")/deploy-env.sh"

ROOT="${SWITCHBOARD_DEPLOY_ROOT:-$HOME/.local/share/switchboard}"
RELEASES="$ROOT/releases"
CURRENT="$ROOT/current"
LABEL="${SWITCHBOARD_LABEL:-com.example.switchboard}"
# Pin the minor version: Homebrew's floating python3 jumps majors on any brew upgrade.
# Bump deliberately (the deploy runs the full suite on the new interpreter).
PYTHON="${SWITCHBOARD_PYTHON:-/opt/homebrew/opt/python@3.15/bin/python3.15}"
KEEP=5

health() {
  for _ in $(seq 1 20); do
    curl -fsS -m 2 http://127.0.0.1:18800/health >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

restart() {
  # launchd sometimes defers a freshly loaded job on this Mac; kickstart is reliable.
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
}

# Release names, newest first (names are commit SHAs; mtime orders them).
releases_newest_first() {
  [ -d "$RELEASES" ] || return 0
  find "$RELEASES" -mindepth 1 -maxdepth 1 -type d ! -name '*.partial' -exec stat -f '%m %N' {} + |
    sort -rn | while read -r _ path; do basename "$path"; done
}

switch_to() {
  ln -sfn "$1" "$CURRENT.tmp" && mv -fh "$CURRENT.tmp" "$CURRENT"
}

case "${1:-}" in
  --status)
    echo "current: $(readlink "$CURRENT" 2>/dev/null || echo none)"
    releases_newest_first | sed 's/^/release: /'
    exit 0
    ;;
  --rollback)
    now="$(basename "$(readlink "$CURRENT")")"
    prev=""
    while read -r name; do
      [ "$name" = "$now" ] || { prev="$name"; break; }
    done < <(releases_newest_first)
    [ -n "$prev" ] || { echo "no previous release" >&2; exit 1; }
    switch_to "$RELEASES/$prev"
    restart
    if ! health; then
      echo "rollback target unhealthy" >&2
      exit 1
    fi
    echo "rolled back to $prev"
    exit 0
    ;;
esac

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
[ -z "$(git -c maintenance.auto=false status --porcelain)" ] || {
  echo "working tree not clean; commit first" >&2
  exit 1
}
SHA="$(git -c maintenance.auto=false rev-parse --short=12 HEAD)"
DEST="$RELEASES/$SHA"

if [ ! -x "$DEST/.venv/bin/sb" ]; then
  echo "testing $SHA"
  .venv/bin/pytest -q
  .venv/bin/ruff check .
  # A venv records absolute paths, so build in place; a failed build is removed.
  rm -rf "$DEST"
  mkdir -p "$DEST"
  trap 'rm -rf "$DEST"' ERR
  git -c maintenance.auto=false archive "$SHA" | tar -x -C "$DEST"
  "$PYTHON" -m venv "$DEST/.venv"
  "$DEST/.venv/bin/pip" install -q --disable-pip-version-check "$DEST"
  "$DEST/.venv/bin/sb" manifest >/dev/null
  trap - ERR
fi

prev="$(readlink "$CURRENT" 2>/dev/null || true)"
switch_to "$DEST"
restart
if health; then
  echo "deployed $SHA"
else
  echo "release $SHA is unhealthy" >&2
  if [ -n "$prev" ]; then
    switch_to "$prev"
    restart
    echo "restored $(basename "$prev")" >&2
  fi
  exit 1
fi

# Expose every skill in the release to each agent, so a new skill is never forgotten.
for dir in "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.openclaw/skills" "$HOME/.gemini/config/skills"; do
  [ -d "$dir" ] || continue
  for skill in "$CURRENT"/skills/*/; do
    ln -sfn "$CURRENT/skills/$(basename "$skill")" "$dir/$(basename "$skill")"
  done
done

# Keep every agent's global instructions pointing at Switchboard (docs/agent-rules.md).
python3 "$CURRENT/scripts/sync-agent-rules.py" || echo "warning: agent rules not synced" >&2

# Keep the newest $KEEP releases, never the current one.
releases_newest_first | tail -n +$((KEEP + 1)) | while read -r old; do
  [ "$RELEASES/$old" = "$(readlink "$CURRENT")" ] || rm -rf "${RELEASES:?}/$old"
done
