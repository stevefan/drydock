#!/bin/bash
# Sync Claude Code auth state from drydock secrets into the claude-code-config volume.
#
# Claude Code's `claude remote-control` (server mode surfaced in claude.ai
# Remote Sessions) requires FULL-SCOPE claude.ai OAuth state — not a
# setup-token (inference-only) and not ANTHROPIC_API_KEY. That state lives
# in two files on the machine where `claude auth login` was run:
#   ~/.claude/.credentials.json  — OAuth access + refresh tokens
#   ~/.claude.json               — account state (organizationUuid, userID,
#                                  projects-trust, etc.)
#
# This script materializes that state inside the container by copying from
# drydock secrets if present:
#   /run/secrets/claude_credentials    → ~/.claude/.credentials.json
#   /run/secrets/claude_account_state  → ~/.claude/.claude.json (plus auto-
#                                        marking /workspace as trusted)
#
# The target is the `claude-code-config` named volume, so the state persists
# across container restarts AND is shared by every desk on the same host —
# auth once per host, all desks inherit.
#
# Set up the secrets on the authenticated machine (typically your Mac):
#   ws secret set <desk> claude_credentials   < ~/.claude/.credentials.json
#   ws secret set <desk> claude_account_state < ~/.claude.json
#   ws secret push <desk> --to <host>
#
# Absence is silent (this script is a no-op); warnings go to the log if a
# secret exists but is unreadable (the classic Linux-host uid papercut).

set -euo pipefail

LOG_FILE="/tmp/claude-auth-sync.log"
CLAUDE_DIR="$HOME/.claude"
mkdir -p "$CLAUDE_DIR"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"; }

sync_secret() {
    local secret="/run/secrets/$1"
    local target="$2"
    local human="$3"
    if [ -e "$secret" ] && [ ! -r "$secret" ]; then
        log "WARNING: $secret exists but is unreadable by $(whoami) (uid $(id -u)). chown 1000:1000 on host."
        return 0
    fi
    if [ ! -r "$secret" ]; then
        return 0
    fi
    cp "$secret" "$target"
    chmod 0600 "$target"
    log "$human → $target"
}

sync_secret claude_credentials   "$CLAUDE_DIR/.credentials.json" "Claude OAuth credentials"
sync_secret claude_account_state "$CLAUDE_DIR/.claude.json"      "Claude account state"

# Mark /workspace as trusted in the projects dict. Claude Code requires a
# per-directory trust acknowledgement before remote-control will start
# ("Workspace not trusted. Please run `claude` in /workspace first..."), and
# the transplanted account state only has trust entries for the source
# machine's paths.
if [ -f "$CLAUDE_DIR/.claude.json" ]; then
    python3 - <<'PY' 2>>"$LOG_FILE" || log "WARNING: could not mark /workspace trusted (see log)"
import json, os
p = os.path.expanduser("~/.claude/.claude.json")
try:
    d = json.load(open(p))
except Exception as e:
    raise SystemExit(f"could not load {p}: {e}")
projects = d.setdefault("projects", {})
ws = projects.setdefault("/workspace", {})
changed = False
if not ws.get("hasTrustDialogAccepted"):
    ws["hasTrustDialogAccepted"] = True
    changed = True
if not ws.get("hasCompletedProjectOnboarding"):
    ws["hasCompletedProjectOnboarding"] = True
    changed = True
if changed:
    json.dump(d, open(p, "w"), indent=2)
    print("marked /workspace trusted")
PY
fi
