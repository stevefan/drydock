#!/bin/bash
set -euo pipefail

LOG_FILE="/tmp/claude-remote-control.log"
REMOTE_CONTROL_NAME="${REMOTE_CONTROL_NAME:-Claude Dev}"

echo "Starting Claude Code remote-control server..." | tee "$LOG_FILE"

while true; do
    # The outer sandbox (firewall, scoped secrets, isolated filesystem) is the
    # security boundary; inside-desk permission prompts are friction, not defense.
    claude remote-control --name "$REMOTE_CONTROL_NAME" --permission-mode bypassPermissions >> "$LOG_FILE" 2>&1 || true
    EXIT_CODE=$?
    echo "$(date): Remote control exited with code $EXIT_CODE. Restarting in 5s..." | tee -a "$LOG_FILE"
    sleep 5
done
