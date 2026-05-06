#!/bin/bash
# smoke: deskwatch event → drydock deskwatch end-to-end.
#
# Creates a throwaway desk, records a job_run event, asserts drydock deskwatch
# reports HEALTHY with exit 0. Then records a failure, asserts UNHEALTHY
# with exit 1. Verifies both the CLI plumbing and the registry round-trip.
#
# No container work here — the desk only exists in the registry for the
# duration of the test. Deliberately narrower than storage-mounts /
# secret-lease / infra-provision because deskwatch is a pure-data
# concern; container-level probes (outputs, probes) are exercised in
# their own smoke if/when one lands.

set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"
NAME="smoke-deskwatch-$$"

cleanup() {
    $SSH "drydock destroy $NAME --force" >/dev/null 2>&1 || true
    $SSH "rm -f /root/.drydock/projects/$NAME.yaml" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Minimal project YAML so registry can create the desk without a real build.
# We don't `drydock create` — we just want the registry row to record events
# against. deskwatch operates against the registry.
$SSH "cat > /root/.drydock/projects/$NAME.yaml" <<YAML
repo_path: /root/src/infra
deskwatch:
  jobs:
    - name: synthetic-job
      expect_success_within: 1h
YAML

# Insert a minimal registry row directly via the pipx-installed drydock
# python. Avoids the cost of `drydock create` (full container build) — we
# only need a registry row so deskwatch has something to target.
DRYDOCK_PY=$($SSH "head -1 \$(which ws) | tr -d '#!'")
$SSH "$DRYDOCK_PY -c '
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
r = Registry()
r.create_workspace(Workspace(
    id=\"ws_$NAME\", name=\"$NAME\", project=\"$NAME\",
    repo_path=\"/root/src/infra\", worktree_path=\"/tmp/smoke\",
    branch=\"ws/$NAME\", state=\"defined\", container_id=\"\",
    workspace_subdir=\"\",
))
r.close()
'" || { echo "FAIL: couldn't seed registry"; exit 1; }

# 1. No events yet → unhealthy (no run on record, exit 1).
got=$($SSH "ws --json deskwatch $NAME 2>&1; echo EXIT:\$?" | tail -n +1)
if ! echo "$got" | grep -q '"healthy": *false'; then
    echo "FAIL: expected unhealthy with no events; got:"; echo "$got"; exit 1
fi
echo "no-events → unhealthy: OK"

# 2. Record success → healthy, exit 0.
$SSH "drydock deskwatch-record $NAME job_run synthetic-job ok --detail 'smoke'" >/dev/null \
    || { echo "FAIL: couldn't record ok event"; exit 1; }
got=$($SSH "ws --json deskwatch $NAME 2>&1; echo EXIT:\$?")
if ! echo "$got" | grep -q '"healthy": *true'; then
    echo "FAIL: expected healthy after ok event; got:"; echo "$got"; exit 1
fi
echo "ok event → healthy: OK"

# 3. Record failure → unhealthy, exit 1.
$SSH "drydock deskwatch-record $NAME job_run synthetic-job failed --detail 'exit 2'" >/dev/null \
    || { echo "FAIL: couldn't record failed event"; exit 1; }
got=$($SSH "ws --json deskwatch $NAME 2>&1; echo EXIT:\$?")
if ! echo "$got" | grep -q '"healthy": *false'; then
    echo "FAIL: expected unhealthy after failed event; got:"; echo "$got"; exit 1
fi
if ! echo "$got" | grep -q 'EXIT:1'; then
    echo "FAIL: expected exit 1 after failed event; got:"; echo "$got"; exit 1
fi
echo "failed event → unhealthy + exit 1: OK"

exit 0
