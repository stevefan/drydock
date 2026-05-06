#!/bin/bash
# smoke: same-desk SECRET lease (V2 baseline).
# A drydock with request_secret_leases capability + the secret in its
# delegatable_secrets requests a lease on a secret. Same-desk file-backed:
# the secret should already be visible at /run/secrets/ via the bind mount.

set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"

# infra has request_secret_leases + anthropic_api_key in delegatable_secrets.
out=$($SSH "drydock exec infra -- drydock-rpc RequestCapability type=SECRET scope.secret_name=anthropic_api_key" 2>&1)
echo "$out" | grep -q '"type": "SECRET"' || {
    echo "FAIL: RequestCapability did not return SECRET lease"
    echo "$out"
    exit 1
}
lease_id=$(echo "$out" | python3 -c "import json,sys; print(json.load(sys.stdin)['lease_id'])")

# Release
rel=$($SSH "drydock exec infra -- drydock-rpc ReleaseCapability lease_id=$lease_id" 2>&1)
echo "$rel" | grep -q '"revoked": true' || {
    echo "FAIL: release didn't revoke"
    echo "$rel"
    exit 1
}

echo "secret lease issue + release: OK"
exit 0
