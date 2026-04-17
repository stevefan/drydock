#!/bin/bash
# Lightweight daemon health check. Run on a schedule (launchd/cron).
# Checks wsd.health via the socket; if unresponsive, logs a warning.
# The launchd KeepAlive on the daemon plist handles restarts — this
# script just surfaces the problem to the log for operators.
#
# Usage: drydock-health-check.sh [socket_path]

set -euo pipefail

SOCKET="${1:-$HOME/.drydock/wsd.sock}"
LOG="$HOME/.drydock/logs/health-check.log"
WS="${WS_BIN:-$HOME/.local/bin/ws}"

mkdir -p "$(dirname "$LOG")"

if [ ! -S "$SOCKET" ]; then
    echo "$(date): WARN: daemon socket missing at $SOCKET" >> "$LOG"
    exit 1
fi

# Quick health RPC via the ws CLI
if "$WS" daemon status >/dev/null 2>&1; then
    # Only log if the previous check was unhealthy (reduce noise)
    if [ -f "$LOG" ] && tail -1 "$LOG" | grep -q "WARN"; then
        echo "$(date): OK: daemon recovered" >> "$LOG"
    fi
    exit 0
else
    echo "$(date): WARN: daemon health check failed (socket present but unresponsive)" >> "$LOG"
    exit 1
fi
