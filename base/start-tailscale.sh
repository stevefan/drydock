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
# Auth key resolution — priority order:
#   1. /run/secrets/tailscale_authkey  (drydock-managed desks)
#   2. $TAILSCALE_AUTHKEY env var      (local dev, non-drydock devcontainers)
#   3. .env.local / .env.devcontainer  (legacy, will be removed)
#
# Defensive check: if the secret exists but isn't readable, surface the
# perm problem instead of silently falling to interactive auth (which
# hangs on headless hosts).
if [ -e /run/secrets/tailscale_authkey ] && [ ! -r /run/secrets/tailscale_authkey ]; then
    echo "ERROR: /run/secrets/tailscale_authkey exists but is unreadable by $(whoami) (uid $(id -u))." | tee -a "$LOG_FILE"
    echo "  Fix on host: chown 1000:1000 ~/.drydock/secrets/<ws_id>/*" | tee -a "$LOG_FILE"
fi
if [ -r /run/secrets/tailscale_authkey ]; then
    TAILSCALE_AUTHKEY=$(cat /run/secrets/tailscale_authkey | tr -d '\n')
    echo "Tailscale auth key loaded from /run/secrets/" | tee -a "$LOG_FILE"
elif [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    echo "Tailscale auth key from env var (local dev)" | tee -a "$LOG_FILE"
else
    for envfile in /workspace/.env.local /workspace/*/.env.local; do
        if [ -f "$envfile" ]; then
            found=$(grep -s '^TAILSCALE_AUTHKEY=' "$envfile" | cut -d'=' -f2- || true)
            if [ -n "$found" ]; then
                TAILSCALE_AUTHKEY="$found"
                echo "Tailscale auth key from $envfile (legacy)" | tee -a "$LOG_FILE"
                break
            fi
        fi
    done
fi

# Optional tag advertisement. Set TAILSCALE_ADVERTISE_TAGS=tag:server (or
# similar) in the project YAML or host env to target a specific ACL rule
# class. Example: a tailnet with a "ssh accept for tag:server" rule avoids
# the "check" web-approval prompt that `autogroup:self` rules usually trigger
# for personal tenants. Note: the tag only sticks if either the auth key
# was generated with that tag pre-assigned, or the tailnet's tagOwners ACL
# permits this device's identity to self-advertise it. See
# ops-personal/tech/Tenant Security Tradeoffs.md for the full story.
TS_ADVERTISE_TAGS_ARG=""
if [ -n "${TAILSCALE_ADVERTISE_TAGS:-}" ]; then
    TS_ADVERTISE_TAGS_ARG="--advertise-tags=${TAILSCALE_ADVERTISE_TAGS}"
    echo "Advertising tailnet tags: ${TAILSCALE_ADVERTISE_TAGS}" | tee -a "$LOG_FILE"
fi

# Tailscale failures (invalid key, network hiccup) must not cascade and kill
# the postStartCommand chain — downstream supervisors (remote-control) still
# need to launch. Log and continue.
if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    sudo tailscale up --ssh --hostname="$TAILSCALE_HOSTNAME" --authkey="$TAILSCALE_AUTHKEY" $TS_ADVERTISE_TAGS_ARG 2>&1 | tee -a "$LOG_FILE" || echo "WARNING: tailscale up failed; continuing without tailnet join" | tee -a "$LOG_FILE"
else
    echo "WARNING: No TAILSCALE_AUTHKEY set. You'll need to authenticate manually via the URL below." | tee -a "$LOG_FILE"
    sudo tailscale up --ssh --hostname="$TAILSCALE_HOSTNAME" $TS_ADVERTISE_TAGS_ARG 2>&1 | tee -a "$LOG_FILE" || echo "WARNING: tailscale up failed; continuing" | tee -a "$LOG_FILE"
fi

# Serve the dev server on the tailnet (only meaningful if tailscale up succeeded)
echo "Exposing port $TAILSCALE_SERVE_PORT via Tailscale serve..." | tee -a "$LOG_FILE"
sudo tailscale serve --bg "$TAILSCALE_SERVE_PORT" 2>&1 | tee -a "$LOG_FILE" || echo "WARNING: tailscale serve failed; continuing" | tee -a "$LOG_FILE"

echo "Tailscale setup complete. Access at: https://$TAILSCALE_HOSTNAME.$(sudo tailscale status --json | jq -r '.MagicDNSSuffix')" | tee -a "$LOG_FILE"
