#!/bin/bash
set -euo pipefail

LOG_FILE="/tmp/claude-remote-control.log"
REMOTE_CONTROL_NAME="${REMOTE_CONTROL_NAME:-Claude Dev}"

echo "Starting Claude Code remote-control server..." | tee "$LOG_FILE"

while true; do
    claude remote-control --name "$REMOTE_CONTROL_NAME" >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?
    echo "$(date): Remote control exited with code $EXIT_CODE. Restarting in 5s..." | tee -a "$LOG_FILE"
    sleep 5
done
