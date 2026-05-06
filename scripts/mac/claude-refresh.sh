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

# Warm up the keychain before extracting. Claude Code Desktop only
# refreshes the OAuth tokens it stores in the keychain when something
# actively uses them; if Mac has been idle for hours, the keychain
# holds an already-expired token. Run a no-op `claude -p` with a
# 30s timeout so it triggers an in-process refresh and writes the
# new tokens back to keychain. Failure here is non-fatal — we still
# push whatever the keychain has.
#
# launchd runs with a sparse PATH and won't see claude in nvm-managed
# locations. Look for the binary in a few well-known nvm/Volta paths
# and Homebrew, then fall back to PATH lookup.
CLAUDE_BIN=""
for p in \
    "${HOME}/.nvm/versions/node"/*/bin/claude \
    "${HOME}/.volta/bin/claude" \
    /opt/homebrew/bin/claude \
    /usr/local/bin/claude
do
    if [ -x "$p" ]; then CLAUDE_BIN="$p"; break; fi
done
if [ -z "$CLAUDE_BIN" ] && command -v claude >/dev/null 2>&1; then
    CLAUDE_BIN=$(command -v claude)
fi

if [ -n "$CLAUDE_BIN" ]; then
    if ! perl -e 'alarm 30; exec @ARGV' "$CLAUDE_BIN" -p ":" >/dev/null 2>&1; then
        log "WARN: keychain warm-up via '$CLAUDE_BIN -p :' failed or timed out (will push current keychain anyway)"
    else
        log "keychain warmed via $CLAUDE_BIN"
    fi
else
    log "WARN: claude CLI not found in nvm/volta/homebrew/PATH; skipping keychain warm-up"
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
    if echo "$CREDS" | ssh "$ssh_target" "drydock secret set $desk claude_credentials" >/dev/null 2>&1; then
        log "  claude_credentials: OK"
    else
        log "  ERROR: claude_credentials push failed"
        fail=1
        continue
    fi

    if [ "$HAS_STATE" = 1 ]; then
        if ssh "$ssh_target" "drydock secret set $desk claude_account_state" < "$STATE_FILE" >/dev/null 2>&1; then
            log "  claude_account_state: OK"
        else
            log "  ERROR: claude_account_state push failed"
            fail=1
        fi
    fi
done < "$CONF"

log "=== claude-refresh done (exit=$fail) ==="
exit $fail
