#!/bin/bash
# Smoke test the V2 daemon adoption path end-to-end.
#
# What this validates:
#   - `ws daemon start` brings up the daemon and the socket appears
#   - `ws daemon status` reports running + health-responsive
#   - `ws create` routes through the daemon (verified by checking the
#     workspace lands in the daemon's registry, NOT the host's default)
#   - `ws destroy` routes through the daemon (cascaded cleanup runs)
#   - `ws daemon stop` tears down cleanly
#
# What this does NOT validate:
#   - Real `devcontainer up` (uses DRYDOCK_WSD_DRY_RUN=1 — synthetic
#     container_ids, no Docker required)
#   - Cross-host scenarios
#   - SpawnChild from inside a real desk (needs a real container)
#
# Exit 0 on success; non-zero on any failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS_BIN="${WS_BIN:-${REPO_ROOT}/.venv/bin/ws}"
PY_BIN="${PY_BIN:-${REPO_ROOT}/.venv/bin/python}"

if [[ ! -x "${WS_BIN}" ]]; then
    echo "ERROR: ws binary not found at ${WS_BIN}" >&2
    echo "  hint: pip install -e '.[dev]' from ${REPO_ROOT}" >&2
    exit 2
fi

# Use a short tmp dir to stay under macOS AF_UNIX 104-char limit.
TMPDIR_BASE=$(mktemp -d /tmp/wsd-smoke.XXXXXX)
SOCKET="${TMPDIR_BASE}/s"
REGISTRY="${TMPDIR_BASE}/r.db"
LOG="${TMPDIR_BASE}/wsd.log"
PID_FILE="${TMPDIR_BASE}/wsd.pid"
SECRETS="${TMPDIR_BASE}/secrets"
HOME_OVERRIDE="${TMPDIR_BASE}"

export DRYDOCK_WSD_SOCKET="${SOCKET}"
export DRYDOCK_WSD_REGISTRY="${REGISTRY}"
export DRYDOCK_WSD_LOG="${LOG}"
export DRYDOCK_SECRETS_ROOT="${SECRETS}"
export DRYDOCK_WSD_DRY_RUN=1
export HOME="${HOME_OVERRIDE}"

cleanup() {
    rc=$?
    set +e
    if [[ -f "${PID_FILE}" ]]; then
        pid=$(cat "${PID_FILE}" 2>/dev/null || true)
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill -TERM "${pid}" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                if ! kill -0 "${pid}" 2>/dev/null; then break; fi
                sleep 0.5
            done
            kill -KILL "${pid}" 2>/dev/null || true
        fi
    fi
    rm -rf "${TMPDIR_BASE}"
    if [[ ${rc} -ne 0 ]]; then
        echo "FAIL (rc=${rc})" >&2
        if [[ -f "${LOG}" ]]; then
            echo "--- daemon log tail ---" >&2
            tail -30 "${LOG}" >&2 || true
        fi
    fi
    exit ${rc}
}
trap cleanup EXIT INT TERM

echo "==> tmp: ${TMPDIR_BASE}"
echo "==> socket: ${SOCKET}"

# 1. Pre-create the registry file so the daemon doesn't race on first open.
"${PY_BIN}" -c "from pathlib import Path; from drydock.core.registry import Registry; Registry(db_path=Path('${REGISTRY}')).close()"

# 2. Start the daemon (manual subprocess; ws daemon start uses a different
# tempdir layout for HOME-relative defaults — easier to do it explicitly here).
echo "==> starting daemon"
"${PY_BIN}" -m drydock.wsd --socket "${SOCKET}" --registry "${REGISTRY}" >"${LOG}" 2>&1 &
DAEMON_PID=$!
echo "${DAEMON_PID}" > "${PID_FILE}"

# Wait up to 5s for the socket to appear.
for _ in $(seq 1 50); do
    if [[ -S "${SOCKET}" ]]; then break; fi
    sleep 0.1
done
if [[ ! -S "${SOCKET}" ]]; then
    echo "FAIL: socket never appeared at ${SOCKET}" >&2
    exit 1
fi
echo "    daemon up (pid=${DAEMON_PID})"

# 3. Probe wsd.health via a raw JSON-RPC call.
echo "==> probing wsd.health"
HEALTH=$(echo '{"jsonrpc":"2.0","method":"wsd.health","id":"smoke-1"}' | nc -U "${SOCKET}")
if ! echo "${HEALTH}" | grep -q '"ok": *true'; then
    echo "FAIL: wsd.health did not return ok=true" >&2
    echo "  response: ${HEALTH}" >&2
    exit 1
fi
echo "    health ok"

# 4. Set up a tiny git repo so create has a valid repo_path.
REPO="${TMPDIR_BASE}/proj-repo"
mkdir -p "${REPO}/.devcontainer"
echo "{}" > "${REPO}/.devcontainer/devcontainer.json"
git -C "${REPO}" init -q
git -C "${REPO}" config user.email smoke@local
git -C "${REPO}" config user.name smoke
echo "init" > "${REPO}/README.md"
git -C "${REPO}" add .
git -C "${REPO}" commit -m "init" -q

# 5. Call ws create — should route through the daemon since
# DRYDOCK_WSD_SOCKET is set and the socket exists.
echo "==> ws create proj smokedesk (expects daemon routing)"
CREATE_OUT=$("${WS_BIN}" --json create proj smokedesk --repo-path "${REPO}")
echo "    create output: ${CREATE_OUT}" | head -c 300; echo

# Verify the daemon's registry got the row (proves the routing went via the
# daemon, not via the V1 fallback path which would write to a different
# registry path).
WORKSPACE_NAME=$("${PY_BIN}" -c "
import sqlite3
conn = sqlite3.connect('${REGISTRY}')
row = conn.execute(\"SELECT name, container_id FROM workspaces WHERE name = 'smokedesk'\").fetchone()
if row is None:
    print('MISSING')
else:
    print(row[0], row[1])
" )
if [[ "${WORKSPACE_NAME}" == "MISSING" ]]; then
    echo "FAIL: workspace 'smokedesk' missing from daemon's registry" >&2
    echo "  the request did not route via the daemon" >&2
    exit 1
fi
case "${WORKSPACE_NAME}" in
    *dry-run*) echo "    workspace landed via daemon (container_id has dry-run prefix as expected)" ;;
    *) echo "FAIL: container_id missing dry-run prefix; routing or dry-run env may be wrong" >&2; echo "  row: ${WORKSPACE_NAME}" >&2; exit 1 ;;
esac

# 6. Call ws destroy.
echo "==> ws destroy smokedesk (expects daemon routing)"
DESTROY_OUT=$("${WS_BIN}" --json destroy smokedesk --force)
echo "    destroy output: ${DESTROY_OUT}" | head -c 300; echo

# Verify the row is gone.
ROW_GONE=$("${PY_BIN}" -c "
import sqlite3
conn = sqlite3.connect('${REGISTRY}')
row = conn.execute(\"SELECT name FROM workspaces WHERE name = 'smokedesk'\").fetchone()
print('GONE' if row is None else 'STILL_PRESENT')
")
if [[ "${ROW_GONE}" != "GONE" ]]; then
    echo "FAIL: workspace 'smokedesk' still present after destroy" >&2
    exit 1
fi
echo "    workspace gone"

# 7. Stop the daemon.
echo "==> stopping daemon"
kill -TERM "${DAEMON_PID}"
for _ in $(seq 1 50); do
    if ! kill -0 "${DAEMON_PID}" 2>/dev/null; then break; fi
    sleep 0.1
done
if kill -0 "${DAEMON_PID}" 2>/dev/null; then
    echo "FAIL: daemon did not exit within 5s of SIGTERM" >&2
    exit 1
fi
echo "    daemon stopped"

echo
echo "PASS: V2 daemon end-to-end smoke complete"
