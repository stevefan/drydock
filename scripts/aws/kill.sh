#!/usr/bin/env bash
# Emergency stop for the drydock AWS role.
#
# Deactivates all access keys on `drydock-runner`, severing the agent's ability
# to assume drydock-agent. Any already-active session creds continue working
# until they expire (max 4h). For faster revocation, also revoke sessions
# issued before now via `aws iam put-role-policy` with an AWSRevokeOlderSessions
# deny — this script does that too.
#
# Does NOT delete resources. After using this, re-enable with `provision.sh` or
# `aws iam update-access-key --status Active`.

set -euo pipefail

ACCOUNT_ID=047535447308
BOOTSTRAP_PROFILE=personal

aws_p() { aws --profile "$BOOTSTRAP_PROFILE" "$@"; }

echo "This will DEACTIVATE all access keys on drydock-runner and revoke active"
echo "drydock-agent sessions. Resources are preserved; creds can be reactivated."
echo
read -r -p "Confirm (type 'kill'): " CONFIRM
if [ "$CONFIRM" != "kill" ]; then
  echo "aborted"
  exit 1
fi

echo "==> Deactivating drydock-runner access keys..."
KEYS=$(aws_p iam list-access-keys --user-name drydock-runner --query 'AccessKeyMetadata[].AccessKeyId' --output text)
for k in $KEYS; do
  aws_p iam update-access-key --user-name drydock-runner --access-key-id "$k" --status Inactive
  echo "    deactivated $k"
done

echo "==> Revoking active drydock-agent sessions (AWSRevokeOlderSessions)..."
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
aws_p iam put-role-policy \
  --role-name drydock-agent \
  --policy-name AWSRevokeOlderSessions \
  --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Deny",
      "Action": ["*"],
      "Resource": ["*"],
      "Condition": {"DateLessThan": {"aws:TokenIssueTime": "${NOW}"}}
    }
  ]
}
EOF
)"
echo "    revoked all sessions issued before ${NOW}"

echo
echo "Drydock is killed. To restore:"
echo "  aws iam update-access-key --profile ${BOOTSTRAP_PROFILE} --user-name drydock-runner --access-key-id <AKID> --status Active"
echo "  aws iam delete-role-policy --profile ${BOOTSTRAP_PROFILE} --role-name drydock-agent --policy-name AWSRevokeOlderSessions"
