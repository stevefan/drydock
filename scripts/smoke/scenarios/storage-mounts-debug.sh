#!/bin/bash
# Debug variant of storage-mounts: leaves the drydock alive on failure so
# we can inspect state.
set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"
NAME="smoke-dbg-$$"
BUCKET="drydock-smoke-dbg-$$"
MARKER="hello-dbg-$(date +%s)"

$SSH "ws exec infra -- drydock-rpc RequestCapability type=INFRA_PROVISION 'scope.actions=[\"s3:*\"]'" >/dev/null
$SSH "ws exec infra -- bash -lc 'export AWS_ACCESS_KEY_ID=\$(cat /run/secrets/aws_access_key_id) AWS_SECRET_ACCESS_KEY=\$(cat /run/secrets/aws_secret_access_key) AWS_SESSION_TOKEN=\$(cat /run/secrets/aws_session_token) AWS_DEFAULT_REGION=us-west-2 && aws s3 mb s3://$BUCKET >/dev/null && echo -n $MARKER | aws s3 cp - s3://$BUCKET/greeting.txt >/dev/null'"

$SSH "cat > /root/.drydock/projects/$NAME.yaml" <<YAML
repo_path: /root/src/infra
tailscale_hostname: $NAME
remote_control_name: $NAME
firewall_extra_domains:
  - deb.debian.org
storage_mounts:
  - source: s3://$BUCKET
    target: /mnt/check
    mode: ro
YAML

echo "project YAML:"
$SSH "cat /root/.drydock/projects/$NAME.yaml"

echo "--- creating drydock..."
$SSH "ws create $NAME" 2>&1 | tail -5

echo "--- overlay contents:"
$SSH "cat /root/.drydock/overlays/ws_${NAME//-/_}.devcontainer.json | jq '.containerEnv.STORAGE_MOUNTS_JSON, .runArgs'"

echo "--- container env STORAGE_MOUNTS_JSON:"
$SSH "docker exec \$(docker ps --filter name=ws_${NAME//-/_} -q) env | grep STORAGE_MOUNTS_JSON"

echo "--- storage-mounts.log inside:"
$SSH "docker exec \$(docker ps --filter name=ws_${NAME//-/_} -q) cat /tmp/storage-mounts.log"

echo "--- /mnt contents:"
$SSH "docker exec \$(docker ps --filter name=ws_${NAME//-/_} -q) ls -la /mnt/"

echo "(not cleaning up — do 'ws destroy $NAME --force; aws s3 rb s3://$BUCKET' manually)"
