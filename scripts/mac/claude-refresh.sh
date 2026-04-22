#!/bin/bash
# Extract Claude Code OAuth credentials from the macOS keychain and push
# them to one or more drydock Harbors. Called by the
# com.drydock.claude-refresh launchd agent every 6h.
#
# The remote-control process inside a drydock refreshes its tokens in
# memory but never writes them back to ~/.claude/.credentials.json.
# File consumers (other desks requesting the secret via RequestCapability,
# init scripts bind-mounting /run/secrets/claude_credentials) therefore
# go stale after ~8h. The Mac keychain is the only place where fresh
# tokens reliably exist — so we re-extract and re-push on a cron.
#
# Config lives at ~/.drydock/claude-refresh.conf, one Harbor per line:
#     root@5.78.146.141:infra
#     root@5.78.146.141:auction-crawl
#     steven@my-mac-harbor.tailnet:dev-sandbox
# Format: <ssh-target>:<desk-name>. Comments (#) and blank lines ignored.
#
# Exit codes: 0 on success for every Harbor, 1 if any push failed.

set -u

CONF="${HOME}/.drydock/claude-refresh.conf"
LOG="${HOME}/.drydock/claude-refresh.log"

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(timestamp) $*" >> "$LOG"; }

mkdir -p "$(dirname "$LOG")"
log "=== claude-refresh start ==="

if ! [ -f "$CONF" ]; then
    log "no config at $CONF; nothing to do"
    exit 0
fi

# Extract credentials ONCE; reuse across all targets.
CREDS=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null) || {
    log "ERROR: keychain extraction failed — Claude Code-credentials not found"
    exit 1
}
if [ -z "$CREDS" ]; then
    log "ERROR: keychain returned empty credentials"
    exit 1
fi

# Account state still lives on disk; optional but usually sent alongside.
STATE_FILE="${HOME}/.claude.json"
HAS_STATE=0
[ -f "$STATE_FILE" ] && HAS_STATE=1

fail=0
while IFS= read -r line; do
    line="${line%%#*}"   # strip comments
    line="${line## }"    # trim leading space
    line="${line%% }"    # trim trailing space
    [ -z "$line" ] && continue

    ssh_target="${line%%:*}"
    desk="${line##*:}"
    if [ -z "$ssh_target" ] || [ -z "$desk" ] || [ "$ssh_target" = "$line" ]; then
        log "SKIP malformed config line: $line"
        continue
    fi

    log "pushing to $ssh_target desk=$desk"
    if echo "$CREDS" | ssh "$ssh_target" "ws secret set $desk claude_credentials" >/dev/null 2>&1; then
        log "  claude_credentials: OK"
    else
        log "  ERROR: claude_credentials push failed"
        fail=1
        continue
    fi

    if [ "$HAS_STATE" = 1 ]; then
        if ssh "$ssh_target" "ws secret set $desk claude_account_state" < "$STATE_FILE" >/dev/null 2>&1; then
            log "  claude_account_state: OK"
        else
            log "  ERROR: claude_account_state push failed"
            fail=1
        fi
    fi
done < "$CONF"

log "=== claude-refresh done (exit=$fail) ==="
exit $fail
