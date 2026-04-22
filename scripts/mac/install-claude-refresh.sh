#!/bin/bash
# One-shot installer for the Claude OAuth refresh launchd agent.
#
# Writes ~/Library/LaunchAgents/com.drydock.claude-refresh.plist with
# the absolute path of this repo baked in, creates a stub config at
# ~/.drydock/claude-refresh.conf (which you then edit), and loads the
# agent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLIST_SRC="${REPO_ROOT}/scripts/mac/com.drydock.claude-refresh.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/com.drydock.claude-refresh.plist"
SCRIPT_PATH="${REPO_ROOT}/scripts/mac/claude-refresh.sh"
CONF="${HOME}/.drydock/claude-refresh.conf"

if [ ! -f "$PLIST_SRC" ]; then
    echo "missing $PLIST_SRC — are you running this from the drydock repo?" >&2
    exit 1
fi

chmod +x "$SCRIPT_PATH"

mkdir -p "$(dirname "$PLIST_DEST")"
sed "s|REPO_ROOT|${REPO_ROOT}|" "$PLIST_SRC" > "$PLIST_DEST"

mkdir -p "$(dirname "$CONF")"
if [ ! -f "$CONF" ]; then
    cat > "$CONF" <<'EOF'
# Drydock Claude OAuth refresh targets.
# One Harbor per line, format: <ssh-target>:<desk-name>
# Lines starting with # and blank lines are ignored.
#
# Example:
# root@5.78.146.141:infra
# root@5.78.146.141:auction-crawl
EOF
    echo "created stub config at $CONF — edit it before reloading the agent"
fi

# Reload so the latest plist + script are picked up.
launchctl unload -w "$PLIST_DEST" >/dev/null 2>&1 || true
launchctl load -w "$PLIST_DEST"

cat <<EOF

installed com.drydock.claude-refresh
  plist:  $PLIST_DEST
  script: $SCRIPT_PATH
  conf:   $CONF
  log:    ~/.drydock/claude-refresh.log

next: edit $CONF to list the Harbors + desks you want refreshed,
      then run \`launchctl kickstart -k gui/\$(id -u)/com.drydock.claude-refresh\`
      to fire a manual refresh.
EOF
