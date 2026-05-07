#!/bin/bash
# smoke: WorkloadLease end-to-end (Phase 2a.3 WL1).
#
# 1. Pick a running drydock (substrate — ambient, lightweight).
# 2. Inspect its cgroup memory limit before.
# 3. RegisterWorkload requesting memory_max=8g (above its standing cap).
# 4. Verify `docker inspect` shows the lifted memory limit live.
# 5. ReleaseWorkload via lease_id.
# 6. Verify cgroup memory limit reverted to original.
#
# Validates: cgroup live update, atomic apply, lease persistence,
# release path, and the WL1 RPC end-to-end through the daemon socket.
#
# Why substrate and not infra: substrate is read-mostly with no
# privileged credentials at risk if something goes wrong mid-test.

set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"
TARGET_DESK="${WORKLOAD_TARGET:-notebooks}"

# Find the container ID for the target desk.
CID=$($SSH "drydock --json inspect $TARGET_DESK" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('container_id', ''))
")
if [ -z "$CID" ]; then
    echo "FAIL: drydock '$TARGET_DESK' has no container_id (not running?)"
    exit 1
fi

# Capture the BEFORE memory limit (in bytes).
mem_before=$($SSH "docker inspect --format '{{.HostConfig.Memory}}' $CID")
echo "before: container=$CID memory_max=$mem_before"

# Request a workload lease via drydock-rpc inside the container.
# The bearer token at /run/secrets/drydock-token authenticates the call.
lease_resp=$($SSH "drydock exec $TARGET_DESK -- drydock-rpc RegisterWorkload \
    kind=experiment \
    duration_max_seconds=300 \
    expected.memory_max=8g")
lease_id=$(echo "$lease_resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('lease_id', ''))
")
if [ -z "$lease_id" ]; then
    echo "FAIL: RegisterWorkload returned no lease_id"
    echo "$lease_resp"
    exit 1
fi
echo "RegisterWorkload: $lease_id (kind=experiment, memory_max=8g)"

# Verify cgroup limit was lifted live.
mem_lifted=$($SSH "docker inspect --format '{{.HostConfig.Memory}}' $CID")
expected=$((8 * 1024 * 1024 * 1024))
if [ "$mem_lifted" != "$expected" ]; then
    echo "FAIL: expected memory_max=$expected (8g), got $mem_lifted"
    # Try to clean up
    $SSH "drydock exec $TARGET_DESK -- drydock-rpc ReleaseWorkload lease_id=$lease_id" >/dev/null
    exit 1
fi
echo "cgroup lifted live: memory_max=$mem_lifted ($((mem_lifted/1024/1024/1024))g): OK"

# Release the lease.
release_resp=$($SSH "drydock exec $TARGET_DESK -- drydock-rpc ReleaseWorkload lease_id=$lease_id")
release_status=$(echo "$release_resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('status', ''))
")
if [ "$release_status" != "released" ]; then
    echo "FAIL: ReleaseWorkload returned status=$release_status"
    echo "$release_resp"
    exit 1
fi
echo "ReleaseWorkload: status=$release_status"

# Verify cgroup reverted.
mem_after=$($SSH "docker inspect --format '{{.HostConfig.Memory}}' $CID")
if [ "$mem_after" != "$mem_before" ]; then
    echo "FAIL: expected memory_max reverted to $mem_before, got $mem_after"
    exit 1
fi
echo "cgroup reverted: memory_max=$mem_after: OK"

echo "workload-lease end-to-end: OK"
