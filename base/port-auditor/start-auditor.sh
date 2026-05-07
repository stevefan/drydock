#!/bin/bash
# Port Auditor container entrypoint.
#
# Order of operations:
#   1. Run init-firewall.sh — narrow egress is structural, must be
#      up before the watch loop opens any HTTP connection
#   2. Verify daemon socket bind-mount is present (without it, the
#      auditor can't observe; fail loud rather than degrade silently)
#   3. exec drydock auditor watch-loop — replaces shell with the
#      watch loop so tini's SIGTERM hits the right process
#
# Environment expected (set by overlay generator when role=auditor):
#   FIREWALL_EXTRA_DOMAINS   — must be subset of validator's allowlist
#   DRYDOCK_DAEMON_SOCKET    — daemon socket path (default
#                               /run/drydock/daemon.sock)
#   DRYDOCK_DAEMON_REGISTRY  — bind-mounted registry DB path RO
#                               (/var/lib/drydock/registry.db)
#   DRYDOCK_AUDIT_LOG        — bind-mounted audit log path RO
#                               (/var/log/drydock/audit.log)
#
# Bearer token expected at /run/secrets/auditor-token (preferred) or
# /run/secrets/drydock-token (fallback during the structural-rollout
# phase). The auditor module reads it directly.

set -euo pipefail

LOG_PREFIX="[start-auditor]"

echo "${LOG_PREFIX} startup at $(date -Iseconds)"

# 1. Firewall up before any egress
if [ -x /usr/local/bin/init-firewall.sh ]; then
    echo "${LOG_PREFIX} initializing narrow egress firewall..."
    /usr/local/bin/init-firewall.sh
    echo "${LOG_PREFIX} firewall up"
else
    echo "${LOG_PREFIX} ERROR: init-firewall.sh not found — refusing to" \
         "start. Narrow egress is structural for the Auditor." >&2
    exit 1
fi

# 2. Daemon socket present
DAEMON_SOCKET="${DRYDOCK_DAEMON_SOCKET:-/run/drydock/daemon.sock}"
if [ ! -S "${DAEMON_SOCKET}" ]; then
    echo "${LOG_PREFIX} ERROR: daemon socket not found at ${DAEMON_SOCKET}." \
         "Ensure the daemon is running on the Harbor and the socket" \
         "is bind-mounted into this container at create-time." >&2
    exit 2
fi
echo "${LOG_PREFIX} daemon socket present at ${DAEMON_SOCKET}"

# 2b. Registry DB + audit log bind-mounts (set by overlay when
# role=auditor). Warn rather than fail if missing — the daemon RPC
# surface should still let the watch loop limp along; missing files
# point to an overlay regression worth surfacing.
REGISTRY_DB="${DRYDOCK_DAEMON_REGISTRY:-/var/lib/drydock/registry.db}"
if [ ! -f "${REGISTRY_DB}" ]; then
    echo "${LOG_PREFIX} WARNING: registry DB not bind-mounted at" \
         "${REGISTRY_DB} — snapshot_harbor() will fail until the" \
         "overlay is regenerated with role=auditor." >&2
else
    echo "${LOG_PREFIX} registry DB present at ${REGISTRY_DB}"
fi
AUDIT_LOG="${DRYDOCK_AUDIT_LOG:-/var/log/drydock/audit.log}"
if [ -f "${AUDIT_LOG}" ]; then
    echo "${LOG_PREFIX} audit log present at ${AUDIT_LOG}"
fi

# 3. Bearer token sanity (don't fail; the watch loop will surface a
# clearer error if it's missing — but log so operators know)
if [ -f /run/secrets/auditor-token ]; then
    echo "${LOG_PREFIX} auditor-scoped bearer token present"
elif [ -f /run/secrets/drydock-token ]; then
    echo "${LOG_PREFIX} dock-scoped token present (fallback during" \
         "structural rollout); auditor-scope check happens at the" \
         "daemon, not here"
else
    echo "${LOG_PREFIX} WARNING: no bearer token at /run/secrets/" \
         "{auditor-token,drydock-token} — RPC calls will fail" >&2
fi

# 4. Hand off to the watch loop
echo "${LOG_PREFIX} exec drydock auditor watch-loop"
exec drydock auditor watch-loop "$@"
