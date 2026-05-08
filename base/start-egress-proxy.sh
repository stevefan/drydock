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

# Phase 2: per-desk file inside the bind-mounted dir. The directory is
# now the bind-mount target (so atomic renames in the daemon don't
# invalidate the file inode); the filename within is per-desk.
# Backwards-compat: if the legacy file-bind path exists, use it.
if [ -n "${DRYDOCK_WORKSPACE_ID:-}" ] && [ -f "/run/drydock/proxy/${DRYDOCK_WORKSPACE_ID}.yaml" ]; then
    DEFAULT_CONFIG="/run/drydock/proxy/${DRYDOCK_WORKSPACE_ID}.yaml"
else
    DEFAULT_CONFIG="/run/drydock/proxy/allowlist.yaml"
fi
CONFIG="${EGRESS_PROXY_CONFIG:-$DEFAULT_CONFIG}"
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

# Already running? Idempotent — postStartCommand may fire on container
# restart even when proxy already up; don't double-launch.
if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) start-egress-proxy: already running pid=$(cat ${PID_FILE})" | tee -a "${LOG}"
    exit 0
fi

# smokescreen flags:
# --listen-ip 127.0.0.1: only accept connections from inside the container.
# --listen-port: HTTP CONNECT proxy port.
# --egress-acl-file: YAML allowlist driven by the daemon.
# --allow-missing-role: use default rule for non-mTLS clients (which
#   is everyone — we don't issue per-process client certs).
#
# setsid + nohup detach from the postStartCommand's session so sudo's
# exit doesn't propagate SIGHUP to smokescreen. Without this, the
# background process is killed within milliseconds of start. </dev/null
# closes stdin so the daemon doesn't block on read.
# dockwarden — drydock's egress proxy (replaces smokescreen). Reads
# the same ACL file path; same listen port; SIGHUP reloads.
PROJECT_TAG="${DRYDOCK_WORKSPACE_ID:-drydock}"
setsid nohup dockwarden \
    -listen "127.0.0.1:${PORT}" \
    -acl "${CONFIG}" \
    -project "${PROJECT_TAG}" \
    </dev/null >>"${LOG}" 2>&1 &
PROXY_PID=$!
echo "${PROXY_PID}" > "${PID_FILE}"
disown -h "${PROXY_PID}" 2>/dev/null || true

echo "$(date -u +%FT%TZ) start-egress-proxy: pid=${PROXY_PID}" | tee -a "${LOG}"
