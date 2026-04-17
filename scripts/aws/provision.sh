#!/usr/bin/env bash
# Provision the drydock AWS identity stack:
#   - permission boundary policy `drydock-boundary`
#   - IAM role `drydock-agent` (AdminAccess + boundary, 4h max session)
#   - IAM user `drydock-runner` (sts:AssumeRole on drydock-agent only)
#   - access key for drydock-runner
#   - `[drydock-runner]` in ~/.aws/credentials and `[profile drydock]` in ~/.aws/config
#
# Idempotent: safe to re-run. Uses the `personal` profile to bootstrap.
# Access key is only created if drydock-runner has <2 keys.

set -euo pipefail

ACCOUNT_ID=047535447308
REGION=us-west-2
BOOTSTRAP_PROFILE=personal
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

aws_p() { aws --profile "$BOOTSTRAP_PROFILE" "$@"; }

echo "==> Creating permission boundary policy drydock-boundary..."
if aws_p iam get-policy --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/drydock-boundary" >/dev/null 2>&1; then
  echo "    already exists; updating as new default version"
  VERSIONS=$(aws_p iam list-policy-versions --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/drydock-boundary" --query 'Versions[?!IsDefaultVersion].VersionId' --output text)
  for v in $VERSIONS; do
    aws_p iam delete-policy-version --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/drydock-boundary" --version-id "$v" || true
  done
  aws_p iam create-policy-version \
    --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/drydock-boundary" \
    --policy-document "file://${HERE}/boundary-policy.json" \
    --set-as-default >/dev/null
else
  aws_p iam create-policy \
    --policy-name drydock-boundary \
    --policy-document "file://${HERE}/boundary-policy.json" >/dev/null
fi

echo "==> Creating IAM user drydock-runner..."
if ! aws_p iam get-user --user-name drydock-runner >/dev/null 2>&1; then
  aws_p iam create-user --user-name drydock-runner >/dev/null
fi

echo "==> Attaching inline policy assume-drydock-agent to drydock-runner..."
aws_p iam put-user-policy \
  --user-name drydock-runner \
  --policy-name assume-drydock-agent \
  --policy-document "file://${HERE}/runner-policy.json"

echo "==> Creating IAM role drydock-agent..."
# Retry handles IAM propagation delay after user create.
create_role_with_retry() {
  local attempts=0
  until aws_p iam create-role \
    --role-name drydock-agent \
    --assume-role-policy-document "file://${HERE}/trust-policy.json" \
    --permissions-boundary "arn:aws:iam::${ACCOUNT_ID}:policy/drydock-boundary" \
    --max-session-duration 14400 >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 10 ]; then
      echo "    create-role failed after $attempts attempts" >&2
      aws_p iam create-role \
        --role-name drydock-agent \
        --assume-role-policy-document "file://${HERE}/trust-policy.json" \
        --permissions-boundary "arn:aws:iam::${ACCOUNT_ID}:policy/drydock-boundary" \
        --max-session-duration 14400
      return 1
    fi
    echo "    waiting for IAM propagation (attempt $attempts)..."
    sleep 3
  done
}
if aws_p iam get-role --role-name drydock-agent >/dev/null 2>&1; then
  echo "    already exists; updating trust + boundary + session duration"
  aws_p iam update-assume-role-policy \
    --role-name drydock-agent \
    --policy-document "file://${HERE}/trust-policy.json"
  aws_p iam put-role-permissions-boundary \
    --role-name drydock-agent \
    --permissions-boundary "arn:aws:iam::${ACCOUNT_ID}:policy/drydock-boundary"
  aws_p iam update-role --role-name drydock-agent --max-session-duration 14400
else
  create_role_with_retry
fi

echo "==> Attaching AdministratorAccess to drydock-agent..."
aws_p iam attach-role-policy \
  --role-name drydock-agent \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess || true

echo "==> Ensuring drydock-runner has an active access key..."
KEY_COUNT=$(aws_p iam list-access-keys --user-name drydock-runner --query 'length(AccessKeyMetadata)' --output text)
if [ "$KEY_COUNT" = "0" ]; then
  KEY_JSON=$(aws_p iam create-access-key --user-name drydock-runner)
  AK=$(echo "$KEY_JSON" | jq -r .AccessKey.AccessKeyId)
  SK=$(echo "$KEY_JSON" | jq -r .AccessKey.SecretAccessKey)

  CRED_FILE="$HOME/.aws/credentials"
  if grep -q '^\[drydock-runner\]' "$CRED_FILE" 2>/dev/null; then
    echo "    ~/.aws/credentials already has [drydock-runner]; skipping write"
  else
    {
      echo ""
      echo "[drydock-runner]"
      echo "aws_access_key_id = $AK"
      echo "aws_secret_access_key = $SK"
    } >> "$CRED_FILE"
    echo "    wrote [drydock-runner] to $CRED_FILE"
  fi
else
  echo "    drydock-runner already has $KEY_COUNT access key(s); not rotating"
fi

echo "==> Ensuring [profile drydock] in ~/.aws/config..."
CFG_FILE="$HOME/.aws/config"
if grep -q '^\[profile drydock\]' "$CFG_FILE"; then
  echo "    [profile drydock] already present"
else
  {
    echo ""
    echo "[profile drydock]"
    echo "role_arn = arn:aws:iam::${ACCOUNT_ID}:role/drydock-agent"
    echo "source_profile = drydock-runner"
    echo "role_session_name = drydock-cli"
    echo "duration_seconds = 14400"
    echo "region = ${REGION}"
    echo "output = json"
  } >> "$CFG_FILE"
  echo "    wrote [profile drydock] to $CFG_FILE"
fi

echo "==> Verifying assume-role chain (may retry on key propagation)..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if aws --profile drydock sts get-caller-identity 2>/dev/null; then
    break
  fi
  if [ "$i" = "10" ]; then
    echo "    still failing after 10 attempts" >&2
    aws --profile drydock sts get-caller-identity
    exit 1
  fi
  echo "    attempt $i: still propagating..."
  sleep 4
done

echo
echo "Done. Role: arn:aws:iam::${ACCOUNT_ID}:role/drydock-agent"
echo "Use: aws --profile drydock <command>"
echo "Kill: $HERE/kill.sh"
