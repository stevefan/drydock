#!/bin/bash
set -euo pipefail

LOG_FILE="/tmp/claude-remote-control.log"
REMOTE_CONTROL_NAME="${REMOTE_CONTROL_NAME:-Claude Dev}"

echo "Starting Claude Code remote-control server..." | tee "$LOG_FILE"

# Load CLAUDE_CODE_OAUTH_TOKEN from secret if present — useful for any non-
# remote-control claude invocations in this desk (scripted claude --print,
# interactive shells). IMPORTANT: Anthropic explicitly blocks long-lived
# tokens from `claude remote-control` ("limited to inference-only for security
# reasons") — the token does NOT satisfy the subscription check for server
# mode. Remote-control requires an interactive `claude auth login` (device
# flow), one-time per claude-code-config volume.
if [ -e /run/secrets/claude_code_token ] && [ ! -r /run/secrets/claude_code_token ]; then
    echo "ERROR: /run/secrets/claude_code_token exists but is unreadable by $(whoami) (uid $(id -u))." | tee -a "$LOG_FILE"
    echo "  Fix on host: chown 1000:1000 ~/.drydock/secrets/<ws_id>/claude_code_token" | tee -a "$LOG_FILE"
fi
if [ -r /run/secrets/claude_code_token ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat /run/secrets/claude_code_token)"
    echo "Loaded CLAUDE_CODE_OAUTH_TOKEN (inference scope — does not satisfy remote-control's full-scope check)" | tee -a "$LOG_FILE"
fi

# remote-control-specific credential check. If ~/.claude/.credentials.json
# doesn't exist in the claude-code-config volume, the server mode will loop
# forever. Surface the expected next step once per boot instead of silently
# letting the supervisor spin.
if [ ! -f "$HOME/.claude/.credentials.json" ]; then
    echo "WARNING: $HOME/.claude/.credentials.json missing. remote-control requires" | tee -a "$LOG_FILE"
    echo "  an interactive \`claude auth login\` (device flow) to populate it." | tee -a "$LOG_FILE"
    echo "  One-time per claude-code-config volume (shared across desks on this host)." | tee -a "$LOG_FILE"
    echo "  The supervisor loop below will keep failing until that login is done." | tee -a "$LOG_FILE"
fi

while true; do
    # The outer sandbox (firewall, scoped secrets, isolated filesystem) is the
    # security boundary; inside-desk permission prompts are friction, not defense.
    claude remote-control --name "$REMOTE_CONTROL_NAME" --permission-mode bypassPermissions >> "$LOG_FILE" 2>&1 || true
    EXIT_CODE=$?
    echo "$(date): Remote control exited with code $EXIT_CODE. Restarting in 5s..." | tee -a "$LOG_FILE"
    sleep 5
done
