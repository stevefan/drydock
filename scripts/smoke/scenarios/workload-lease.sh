#!/bin/bash
# smoke: WorkloadLease end-to-end (Phase 2a.3 WL1).
#
# Validates: cgroup live update, atomic apply, lease persistence,
# release path, and the WL1 RPC end-to-end through the daemon socket.
#
# 1. Pick a target desk + ensure it has a standing memory cap (V1
#    constraint: lifts require an original cap to revert to, since
#    docker's update API can't clear a once-set memory limit).
# 2. Read its cgroup memory limit BEFORE.
# 3. RegisterWorkload requesting memory_max=8g (above its standing cap).
# 4. Verify `docker inspect` shows the lifted memory limit live.
# 5. ReleaseWorkload via lease_id.
# 6. Verify cgroup memory limit reverted to original cap.

set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"
TARGET_DESK="${WORKLOAD_TARGET:-notebooks}"

# Resolve container id + original memory cap from registry.
inspect_json=$($SSH "drydock --json inspect $TARGET_DESK")
CID=$(echo "$inspect_json" | python3 -c "import sys, json; print(json.load(sys.stdin).get('container_id', ''))")
if [ -z "$CID" ]; then
    echo "FAIL: drydock '$TARGET_DESK' has no container_id (not running?)"
    exit 1
fi
ORIG_CAP=$(echo "$inspect_json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
caps = d.get('original_resources_hard') or {}
print(caps.get('memory_max') or '')
")

if [ -z "$ORIG_CAP" ]; then
    # No standing cap declared in project YAML. Lifts require one
    # (V1 constraint per docs/design/make-the-harness-live.md).
    # Apply a temporary 4g cap via docker update, treating it as
    # the "original" for this test. We restore "no cap" at exit
    # by leaving the lifted state in place — pragmatic for smoke.
    echo "no original cap; setting temporary 4g cap for the smoke window"
    $SSH "docker update --memory=4g --memory-swap=4g $CID" >/dev/null
    # Fake the registry's record of original_resources_hard so the
    # lease path picks up the cap we just installed.
    $SSH "python3 -c '
import json, sqlite3
c = sqlite3.connect(\"/root/.drydock/registry.db\")
c.execute(\"UPDATE drydocks SET original_resources_hard = ? WHERE name = ?\",
          (json.dumps({\"memory_max\": \"4g\"}), \"$TARGET_DESK\"))
c.commit()
'"
    ORIG_CAP="4g"
    cleanup_temp_cap=true
else
    cleanup_temp_cap=false
fi

cleanup() {
    # Always try to release any lease we might have left behind.
    if [ -n "${lease_id:-}" ]; then
        $SSH "drydock exec $TARGET_DESK -- drydock-rpc ReleaseWorkload lease_id=$lease_id" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

mem_before=$($SSH "docker inspect --format '{{.HostConfig.Memory}}' $CID")
echo "before: container=$CID memory_max=$mem_before (orig_cap=$ORIG_CAP)"

# Request a workload lease via drydock-rpc inside the container.
lease_resp=$($SSH "drydock exec $TARGET_DESK -- drydock-rpc RegisterWorkload \
    kind=experiment \
    duration_max_seconds=300 \
    expected.memory_max=8g")
lease_id=$(echo "$lease_resp" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('lease_id', ''))
except Exception:
    pass
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
    exit 1
fi
echo "cgroup lifted live: memory_max=$mem_lifted ($((mem_lifted/1024/1024/1024))g): OK"

# Release the lease.
release_resp=$($SSH "drydock exec $TARGET_DESK -- drydock-rpc ReleaseWorkload lease_id=$lease_id")
release_status=$(echo "$release_resp" | python3 -c "
import sys, json
print(json.load(sys.stdin).get('status', ''))
")
if [ "$release_status" != "released" ]; then
    echo "FAIL: ReleaseWorkload returned status=$release_status"
    echo "$release_resp"
    exit 1
fi
echo "ReleaseWorkload: status=$release_status"

# Verify cgroup reverted to original cap.
mem_after=$($SSH "docker inspect --format '{{.HostConfig.Memory}}' $CID")
if [ "$mem_after" != "$mem_before" ]; then
    echo "FAIL: expected memory_max reverted to $mem_before, got $mem_after"
    exit 1
fi
echo "cgroup reverted: memory_max=$mem_after: OK"

# If we installed a temporary cap, leave it (docker can't undo it).
# Next container recreate will reset to the project YAML's actual
# (unset) value, returning to unlimited.
if [ "$cleanup_temp_cap" = true ]; then
    echo "note: temporary 4g cap left in place (docker can't clear it); recreate to restore unlimited"
    # Reset registry's original_resources_hard back to {}
    $SSH "python3 -c '
import json, sqlite3
c = sqlite3.connect(\"/root/.drydock/registry.db\")
c.execute(\"UPDATE drydocks SET original_resources_hard = ? WHERE name = ?\",
          (json.dumps({}), \"$TARGET_DESK\"))
c.commit()
'"
fi

echo "workload-lease end-to-end: OK"
