#!/bin/bash
# Launch Syncthing inside the notebooks desk.
#
# - Config lives in /var/lib/syncthing (named volume, survives rebuild)
# - Vault data lives in /vault (named volume, exposed to sibling desks)
# - GUI bound to 0.0.0.0:8384 so the Tailscale-served port is reachable
#   from the harbor's tailnet (drydock attach / port-forward)
# - No restart loop — drydock's supervisor watches for process health
#
# First run writes a fresh config. Subsequent runs reuse it. To reset
# pairing, wipe /var/lib/syncthing/config.xml and restart the desk.

set -uo pipefail

LOG=/tmp/syncthing.log
HOME_DIR=/var/lib/syncthing
VAULT=/vault

mkdir -p "$HOME_DIR" "$VAULT"

if pgrep -x syncthing >/dev/null 2>&1; then
    echo "$(date +%H:%M:%S) syncthing: already running; skipping" >> "$LOG"
    exit 0
fi

# --home sets both config + database directory
# --no-browser + --no-restart: headless, drydock handles lifecycle
# --gui-address=0.0.0.0:8384 so we can configure it over the tailnet
# (Syncthing's default is 127.0.0.1:8384 — invisible from outside)
nohup syncthing \
    --home="$HOME_DIR" \
    --no-browser \
    --no-restart \
    --gui-address=0.0.0.0:8384 \
    >>"$LOG" 2>&1 &

# Brief wait so postStartCommand's log shows the device ID on first boot.
for _ in $(seq 1 20); do
    if [ -s "$HOME_DIR/cert.pem" ]; then
        device_id=$(syncthing --home="$HOME_DIR" --device-id 2>/dev/null || true)
        [ -n "$device_id" ] && echo "$(date +%H:%M:%S) syncthing started, device-id=$device_id" >> "$LOG"
        break
    fi
    sleep 0.5
done

echo "$(date +%H:%M:%S) start-syncthing.sh: done (pid=$!)" >> "$LOG"
