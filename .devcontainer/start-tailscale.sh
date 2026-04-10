#!/bin/bash
set -euo pipefail

LOG_FILE="/tmp/tailscale.log"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-claude-dev}"
TAILSCALE_SERVE_PORT="${TAILSCALE_SERVE_PORT:-3000}"

echo "Starting Tailscale daemon..." | tee "$LOG_FILE"

# Start tailscaled in userspace networking mode (no TUN device needed)
sudo tailscaled --state=/tmp/tailscale/tailscaled.state --tun=userspace-networking >> "$LOG_FILE" 2>&1 &

# Wait for daemon to be ready
sleep 2

# Bring Tailscale up — use auth key if available, otherwise fall back to interactive auth URL
echo "Bringing Tailscale up..." | tee -a "$LOG_FILE"
# Source auth key from .env.local if not already set
# Search for auth key in any project .env.local or .env.devcontainer
if [ -z "${TAILSCALE_AUTHKEY:-}" ]; then
    for envfile in /workspace/.env.local /workspace/*/.env.local /workspace/*/.env.devcontainer; do
        if [ -f "$envfile" ]; then
            found=$(grep -s '^TAILSCALE_AUTHKEY=' "$envfile" | cut -d'=' -f2- || true)
            if [ -n "$found" ]; then
                TAILSCALE_AUTHKEY="$found"
                break
            fi
        fi
    done
fi

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    sudo tailscale up --hostname="$TAILSCALE_HOSTNAME" --authkey="$TAILSCALE_AUTHKEY" 2>&1 | tee -a "$LOG_FILE"
else
    echo "WARNING: No TAILSCALE_AUTHKEY set. You'll need to authenticate manually via the URL below." | tee -a "$LOG_FILE"
    sudo tailscale up --hostname="$TAILSCALE_HOSTNAME" 2>&1 | tee -a "$LOG_FILE"
fi

# Serve the dev server on the tailnet
echo "Exposing port $TAILSCALE_SERVE_PORT via Tailscale serve..." | tee -a "$LOG_FILE"
sudo tailscale serve --bg "$TAILSCALE_SERVE_PORT" 2>&1 | tee -a "$LOG_FILE"

echo "Tailscale setup complete. Access at: https://$TAILSCALE_HOSTNAME.$(sudo tailscale status --json | jq -r '.MagicDNSSuffix')" | tee -a "$LOG_FILE"
