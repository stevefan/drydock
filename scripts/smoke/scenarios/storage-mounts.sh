#!/bin/bash
# smoke: declarative storage_mounts end-to-end.
#
# 1. Uses the infra drydock to `aws s3 mb` a throwaway bucket + upload a marker.
# 2. Creates a smoke drydock with storage_mounts: declaring the bucket at /mnt/check.
# 3. Verifies the marker file is visible inside the smoke drydock via the FUSE mount.
# 4. Tears down smoke drydock + bucket.

set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"
NAME="smoke-storage-$$"
BUCKET="drydock-smoke-storage-$$"
MARKER="hello-from-smoke-$(date +%s)"

cleanup() {
    $SSH "ws destroy $NAME --force" >/dev/null 2>&1 || true
    $SSH "rm -f /root/.drydock/projects/$NAME.yaml" >/dev/null 2>&1 || true
    $SSH "ws exec infra -- bash -lc 'export AWS_ACCESS_KEY_ID=\$(cat /run/secrets/aws_access_key_id) AWS_SECRET_ACCESS_KEY=\$(cat /run/secrets/aws_secret_access_key) AWS_SESSION_TOKEN=\$(cat /run/secrets/aws_session_token) AWS_DEFAULT_REGION=us-west-2 && aws s3 rm s3://$BUCKET/greeting.txt >/dev/null 2>&1; aws s3 rb s3://$BUCKET >/dev/null 2>&1'" || true
}
trap cleanup EXIT

# Prereq: infra drydock running with INFRA_PROVISION lease.
$SSH "ws exec infra -- drydock-rpc RequestCapability type=INFRA_PROVISION 'scope.actions=[\"s3:*\"]'" >/dev/null || {
    echo "FAIL: couldn't get INFRA_PROVISION lease from infra drydock"; exit 1;
}

# Create bucket + marker
$SSH "ws exec infra -- bash -lc 'export AWS_ACCESS_KEY_ID=\$(cat /run/secrets/aws_access_key_id) AWS_SECRET_ACCESS_KEY=\$(cat /run/secrets/aws_secret_access_key) AWS_SESSION_TOKEN=\$(cat /run/secrets/aws_session_token) AWS_DEFAULT_REGION=us-west-2 && aws s3 mb s3://$BUCKET >/dev/null && echo -n $MARKER | aws s3 cp - s3://$BUCKET/greeting.txt >/dev/null'" || {
    echo "FAIL: bucket setup"; exit 1;
}

# Write throwaway project YAML
$SSH "cat > /root/.drydock/projects/$NAME.yaml" <<YAML
repo_path: /root/src/infra
tailscale_hostname: $NAME
remote_control_name: $NAME
firewall_extra_domains:
  - login.tailscale.com
  - controlplane.tailscale.com
  - deb.debian.org
secret_entitlements:
  - tailscale_authkey
storage_mounts:
  - source: s3://$BUCKET
    target: /mnt/check
    mode: ro
YAML

# Create drydock; ws create blocks until state=running.
$SSH "ws create $NAME" >/dev/null 2>&1 || { echo "FAIL: ws create"; exit 1; }

# Assert marker visible via mount
got=$($SSH "ws exec $NAME -- cat /mnt/check/greeting.txt 2>&1")
if [ "$got" != "$MARKER" ]; then
    echo "FAIL: expected '$MARKER', got '$got'"
    $SSH "ws exec $NAME -- cat /tmp/storage-mounts.log 2>&1" | sed 's/^/  log: /'
    exit 1
fi
echo "marker read through mount: OK ($got)"

# Assert refresh daemon is alive (Phase C.1)
refresh_status=$($SSH "ws exec $NAME -- bash -lc 'pid=\$(cat /tmp/storage-mounts-refresh.pid 2>/dev/null); if [ -n \"\$pid\" ] && kill -0 \$pid 2>/dev/null; then echo alive:\$pid; else echo dead; fi'")
if [[ "$refresh_status" == alive:* ]]; then
    echo "refresh daemon: OK ($refresh_status)"
    exit 0
else
    echo "FAIL: refresh daemon not running ($refresh_status)"
    $SSH "ws exec $NAME -- cat /tmp/storage-mounts.log 2>&1" | sed 's/^/  log: /'
    exit 1
fi
