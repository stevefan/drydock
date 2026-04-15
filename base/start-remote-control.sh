#!/bin/bash
set -euo pipefail

LOG_FILE="/tmp/claude-remote-control.log"
REMOTE_CONTROL_NAME="${REMOTE_CONTROL_NAME:-Claude Dev}"

echo "Starting Claude Code remote-control server..." | tee "$LOG_FILE"

# Claude remote-control requires claude.ai subscription auth. ANTHROPIC_API_KEY
# (per-call API metering) does NOT satisfy the subscription check. The correct
# credential is a long-lived subscription-backed token produced by
# `claude setup-token`, consumed via CLAUDE_CODE_OAUTH_TOKEN. Store it as a
# drydock secret (`ws secret set <ws> claude_code_token`) and push to the host.
if [ -e /run/secrets/claude_code_token ] && [ ! -r /run/secrets/claude_code_token ]; then
    echo "ERROR: /run/secrets/claude_code_token exists but is unreadable by $(whoami) (uid $(id -u))." | tee -a "$LOG_FILE"
    echo "  Likely cause: secret file not chowned to container uid on this host." | tee -a "$LOG_FILE"
    echo "  Fix on host: chown 1000:1000 ~/.drydock/secrets/<ws_id>/claude_code_token" | tee -a "$LOG_FILE"
fi
if [ -r /run/secrets/claude_code_token ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat /run/secrets/claude_code_token)"
    echo "Loaded CLAUDE_CODE_OAUTH_TOKEN from /run/secrets/claude_code_token" | tee -a "$LOG_FILE"
else
    echo "WARNING: /run/secrets/claude_code_token not present; remote-control will loop with \"must be logged in\" until the secret is set + pushed." | tee -a "$LOG_FILE"
fi

while true; do
    # The outer sandbox (firewall, scoped secrets, isolated filesystem) is the
    # security boundary; inside-desk permission prompts are friction, not defense.
    claude remote-control --name "$REMOTE_CONTROL_NAME" --permission-mode bypassPermissions >> "$LOG_FILE" 2>&1 || true
    EXIT_CODE=$?
    echo "$(date): Remote control exited with code $EXIT_CODE. Restarting in 5s..." | tee -a "$LOG_FILE"
    sleep 5
done
