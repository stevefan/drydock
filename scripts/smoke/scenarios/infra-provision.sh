#!/bin/bash
# smoke: INFRA_PROVISION lease → STS session → bucket round-trip.
#
# Validates that a drydock with request_provision_leases + a matching
# delegatable_provision_scopes entry can mint STS creds and use them.
# Uses the infra drydock (which has these wired already).

set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"
BUCKET="drydock-smoke-ip-$$"

cleanup() {
    $SSH "ws exec infra -- bash -lc 'export AWS_ACCESS_KEY_ID=\$(cat /run/secrets/aws_access_key_id) AWS_SECRET_ACCESS_KEY=\$(cat /run/secrets/aws_secret_access_key) AWS_SESSION_TOKEN=\$(cat /run/secrets/aws_session_token) AWS_DEFAULT_REGION=us-west-2 && aws s3 rb s3://$BUCKET --force >/dev/null 2>&1'" || true
}
trap cleanup EXIT

# Request a lease scoped to s3:*
lease=$($SSH "ws exec infra -- drydock-rpc RequestCapability type=INFRA_PROVISION 'scope.actions=[\"s3:*\",\"sts:GetCallerIdentity\"]'" 2>&1)
echo "$lease" | grep -q '"type": "INFRA_PROVISION"' || {
    echo "FAIL: RequestCapability didn't return INFRA_PROVISION lease"
    echo "$lease"
    exit 1
}

# Validate STS identity
identity=$($SSH "ws exec infra -- bash -lc 'export AWS_ACCESS_KEY_ID=\$(cat /run/secrets/aws_access_key_id) AWS_SECRET_ACCESS_KEY=\$(cat /run/secrets/aws_secret_access_key) AWS_SESSION_TOKEN=\$(cat /run/secrets/aws_session_token) AWS_DEFAULT_REGION=us-west-2 && aws sts get-caller-identity'")
echo "$identity" | grep -q 'assumed-role/drydock-agent/drydock-ws_infra' || {
    echo "FAIL: STS identity wasn't drydock-agent"
    echo "$identity"
    exit 1
}

# Create + delete a bucket (proves s3:* actions actually granted)
$SSH "ws exec infra -- bash -lc 'export AWS_ACCESS_KEY_ID=\$(cat /run/secrets/aws_access_key_id) AWS_SECRET_ACCESS_KEY=\$(cat /run/secrets/aws_secret_access_key) AWS_SESSION_TOKEN=\$(cat /run/secrets/aws_session_token) AWS_DEFAULT_REGION=us-west-2 && aws s3 mb s3://$BUCKET && aws s3 rb s3://$BUCKET'" >/dev/null || {
    echo "FAIL: bucket round-trip"
    exit 1
}

echo "provision lease -> STS -> bucket round-trip: OK"
exit 0
