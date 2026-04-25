#!/bin/bash
# Launch the collab Telegram bot as a backgrounded daemon.
# Pidfile-guarded so postStartCommand re-runs are no-ops.

set -uo pipefail

LOG=/tmp/telegram-bot.log
PIDFILE=/tmp/telegram-bot.pid

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "$(date +%H:%M:%S) bot: already running pid=$(cat "$PIDFILE")" >> "$LOG"
    exit 0
fi

if ! [ -r /run/secrets/telegram_bot_token ]; then
    echo "$(date +%H:%M:%S) bot: /run/secrets/telegram_bot_token missing — skipping start" >> "$LOG"
    exit 0
fi

nohup /opt/telegram-bot-venv/bin/python3 /usr/local/bin/bot.py >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "$(date +%H:%M:%S) bot: started pid=$!" >> "$LOG"
