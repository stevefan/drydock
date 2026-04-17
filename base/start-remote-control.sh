#!/bin/bash
set -euo pipefail

LOG_FILE="/tmp/claude-remote-control.log"
REMOTE_CONTROL_NAME="${REMOTE_CONTROL_NAME:-Claude Dev}"

# Sync Claude auth state from drydock secrets into ~/.claude/ if the secrets
# are present. No-op if not. Done here (rather than in postStartCommand) so
# downstream projects don't have to update their devcontainer.json to pick
# up this behavior — they just inherit via drydock-base.
if [ -x /usr/local/bin/sync-claude-auth.sh ]; then
    /usr/local/bin/sync-claude-auth.sh || echo "WARNING: sync-claude-auth.sh failed; see /tmp/claude-auth-sync.log" | tee -a "$LOG_FILE"
fi

# Materialize AWS credentials from drydock secrets into ~/.aws/ if present.
# No-op when no AWS secrets are seeded (desk doesn't use AWS).
if [ -x /usr/local/bin/sync-aws-auth.sh ]; then
    /usr/local/bin/sync-aws-auth.sh || echo "WARNING: sync-aws-auth.sh failed; see /tmp/aws-auth-sync.log" | tee -a "$LOG_FILE"
fi

echo "Starting Claude Code remote-control server..." | tee "$LOG_FILE"

# Claude Code auth precedence for remote-control:
#   1. If ~/.claude/.credentials.json exists (full-scope OAuth state, from
#      claude auth login or our sync-claude-auth.sh secret transplant),
#      USE IT. Do NOT set CLAUDE_CODE_OAUTH_TOKEN — claude prefers the env
#      var and the setup-token it represents is explicitly rejected by
#      remote-control ("limited to inference-only for security reasons").
#   2. Otherwise, if /run/secrets/claude_code_token is present, export it
#      for any non-remote-control claude invocations (scripted --print,
#      interactive shells) that might run inside the desk — even though
#      remote-control itself will still fail in this path.
if [ -e /run/secrets/claude_code_token ] && [ ! -r /run/secrets/claude_code_token ]; then
    echo "ERROR: /run/secrets/claude_code_token exists but is unreadable by $(whoami) (uid $(id -u))." | tee -a "$LOG_FILE"
    echo "  Fix on host: chown 1000:1000 ~/.drydock/secrets/<ws_id>/claude_code_token" | tee -a "$LOG_FILE"
fi
if [ -f "$HOME/.claude/.credentials.json" ]; then
    echo "Using $HOME/.claude/.credentials.json for auth (full OAuth scope)." | tee -a "$LOG_FILE"
    unset CLAUDE_CODE_OAUTH_TOKEN  # paranoid — ensure it doesn't leak from a parent env
elif [ -r /run/secrets/claude_code_token ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat /run/secrets/claude_code_token)"
    echo "Loaded CLAUDE_CODE_OAUTH_TOKEN (inference scope — does NOT satisfy remote-control)." | tee -a "$LOG_FILE"
fi

# remote-control-specific credential check
if [ ! -f "$HOME/.claude/.credentials.json" ]; then
    echo "WARNING: $HOME/.claude/.credentials.json missing. remote-control will loop." | tee -a "$LOG_FILE"
    echo "  Fix: on an authenticated machine (e.g. your Mac after \`claude auth login\`):" | tee -a "$LOG_FILE"
    echo "    ws secret set <desk> claude_credentials    < ~/.claude/.credentials.json" | tee -a "$LOG_FILE"
    echo "    ws secret set <desk> claude_account_state  < ~/.claude.json" | tee -a "$LOG_FILE"
    echo "    ws secret push <desk> --to <host>" | tee -a "$LOG_FILE"
    echo "  Then ws create --force (or restart the container) so sync-claude-auth.sh runs." | tee -a "$LOG_FILE"
fi

while true; do
    # The outer sandbox (firewall, scoped secrets, isolated filesystem) is the
    # security boundary; inside-desk permission prompts are friction, not defense.
    claude remote-control --name "$REMOTE_CONTROL_NAME" --permission-mode bypassPermissions >> "$LOG_FILE" 2>&1 || true
    EXIT_CODE=$?
    echo "$(date): Remote control exited with code $EXIT_CODE. Restarting in 5s..." | tee -a "$LOG_FILE"
    sleep 5
done
