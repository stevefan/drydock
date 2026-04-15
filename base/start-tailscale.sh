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
# Source auth key. Prefer the mounted secret; fall back to env files.
# Defensive check: if the secret exists but isn't readable, the silent
# fall-through below sends us to interactive auth, which on a headless
# host means the container appears to "hang" with no clear cause. Surface
# the perm problem before the fall-through swallows it.
if [ -e /run/secrets/tailscale_authkey ] && [ ! -r /run/secrets/tailscale_authkey ]; then
    echo "ERROR: /run/secrets/tailscale_authkey exists but is unreadable by $(whoami) (uid $(id -u))." | tee -a "$LOG_FILE"
    echo "  Likely cause: secret file owned by root with mode 0400 on a Linux host." | tee -a "$LOG_FILE"
    echo "  Fix on host: chown 1000:1000 ~/.drydock/secrets/<ws_id>/*" | tee -a "$LOG_FILE"
fi
if [ -z "${TAILSCALE_AUTHKEY:-}" ] && [ -r /run/secrets/tailscale_authkey ]; then
    TAILSCALE_AUTHKEY=$(cat /run/secrets/tailscale_authkey)
fi
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

# Tailscale failures (invalid key, network hiccup) must not cascade and kill
# the postStartCommand chain — downstream supervisors (remote-control) still
# need to launch. Log and continue.
if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    sudo tailscale up --ssh --hostname="$TAILSCALE_HOSTNAME" --authkey="$TAILSCALE_AUTHKEY" 2>&1 | tee -a "$LOG_FILE" || echo "WARNING: tailscale up failed; continuing without tailnet join" | tee -a "$LOG_FILE"
else
    echo "WARNING: No TAILSCALE_AUTHKEY set. You'll need to authenticate manually via the URL below." | tee -a "$LOG_FILE"
    sudo tailscale up --ssh --hostname="$TAILSCALE_HOSTNAME" 2>&1 | tee -a "$LOG_FILE" || echo "WARNING: tailscale up failed; continuing" | tee -a "$LOG_FILE"
fi

# Serve the dev server on the tailnet (only meaningful if tailscale up succeeded)
echo "Exposing port $TAILSCALE_SERVE_PORT via Tailscale serve..." | tee -a "$LOG_FILE"
sudo tailscale serve --bg "$TAILSCALE_SERVE_PORT" 2>&1 | tee -a "$LOG_FILE" || echo "WARNING: tailscale serve failed; continuing" | tee -a "$LOG_FILE"

echo "Tailscale setup complete. Access at: https://$TAILSCALE_HOSTNAME.$(sudo tailscale status --json | jq -r '.MagicDNSSuffix')" | tee -a "$LOG_FILE"
