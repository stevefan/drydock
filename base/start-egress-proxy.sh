#!/usr/bin/env bash
# start-egress-proxy.sh — launch smokescreen as the layer-7 egress
# enforcement layer. Phase 2a.1 of docs/design/make-the-harness-live.md.
#
# E0 (this script ships): no-op unless EGRESS_PROXY_ENABLED=1. Existing
# desks aren't affected.
#
# E1 (future activation): when enabled, smokescreen reads its allowlist
# from $EGRESS_PROXY_CONFIG (default /run/drydock/proxy/allowlist.yaml,
# bind-mounted from the Harbor) and listens on $EGRESS_PROXY_PORT
# (default 4750). The container's HTTP_PROXY / HTTPS_PROXY env should
# point at 127.0.0.1:$EGRESS_PROXY_PORT.
#
# E3 (future): the daemon writes the allowlist file and SIGHUPs us
# whenever NETWORK_REACH leases come and go. Sub-minute end-to-end.
set -euo pipefail

if [ "${EGRESS_PROXY_ENABLED:-0}" != "1" ]; then
    # Default off. Nothing to do; legacy iptables/ipset path handles egress.
    exit 0
fi

CONFIG="${EGRESS_PROXY_CONFIG:-/run/drydock/proxy/allowlist.yaml}"
PORT="${EGRESS_PROXY_PORT:-4750}"
LOG_DIR=/var/log/drydock
LOG="${LOG_DIR}/egress-proxy.log"
PID_FILE=/tmp/smokescreen.pid

mkdir -p "${LOG_DIR}" 2>/dev/null || sudo -n mkdir -p "${LOG_DIR}"

if [ ! -f "${CONFIG}" ]; then
    echo "$(date -u +%FT%TZ) start-egress-proxy: config not present at ${CONFIG}; refusing to start (would deny everything)" | tee -a "${LOG}"
    # The default-deny iptables rule is the security floor; without an
    # allowlist file, the worker would have no egress at all. Better to
    # surface this explicitly than to silently brick the container.
    exit 1
fi

echo "$(date -u +%FT%TZ) start-egress-proxy: launching smokescreen on :${PORT} with allowlist ${CONFIG}" | tee -a "${LOG}"

# smokescreen flags:
# --listen-ip 127.0.0.1: only accept connections from inside the container.
# --listen-port: HTTP CONNECT proxy port.
# --egress-acl-file: YAML allowlist driven by the daemon.
# --deny-address: explicit SSRF protections (RFC1918, link-local). Smokescreen
#   has built-in defaults; we keep them.
exec smokescreen \
    --listen-ip 127.0.0.1 \
    --listen-port "${PORT}" \
    --egress-acl-file "${CONFIG}" \
    >>"${LOG}" 2>&1 &
echo $! > "${PID_FILE}"

echo "$(date -u +%FT%TZ) start-egress-proxy: pid=$(cat ${PID_FILE})" | tee -a "${LOG}"
