#!/bin/bash
# Materialize AWS credentials from drydock secrets into ~/.aws/ inside
# the container. Mirrors the sync-claude-auth.sh pattern.
#
# Reads from /run/secrets/ (bind-mounted from ~/.drydock/secrets/<desk_id>/):
#   aws_access_key_id      — required for AWS access
#   aws_secret_access_key  — required for AWS access
#   aws_region             — optional (default: us-west-2)
#   aws_profile_name       — optional (default: "default")
#
# Writes:
#   ~/.aws/credentials     — [profile] with access key + secret
#   ~/.aws/config          — [profile] with region + output format
#
# Skip silently if no AWS secrets are present (desk doesn't need AWS).
# Run in the postStartCommand chain or from start-remote-control.sh.

set -euo pipefail

LOG="/tmp/aws-auth-sync.log"
SECRETS_DIR="/run/secrets"
AWS_DIR="${HOME}/.aws"

# Check if AWS secrets exist
if [ ! -r "${SECRETS_DIR}/aws_access_key_id" ] || [ ! -r "${SECRETS_DIR}/aws_secret_access_key" ]; then
    echo "$(date): No AWS secrets at ${SECRETS_DIR}; skipping AWS auth sync" >> "$LOG"
    exit 0
fi

ACCESS_KEY=$(cat "${SECRETS_DIR}/aws_access_key_id" | tr -d '\n')
SECRET_KEY=$(cat "${SECRETS_DIR}/aws_secret_access_key" | tr -d '\n')
REGION="${AWS_DEFAULT_REGION:-us-west-2}"
PROFILE="${AWS_PROFILE_NAME:-default}"

# Read region override from secrets if present
if [ -r "${SECRETS_DIR}/aws_region" ]; then
    REGION=$(cat "${SECRETS_DIR}/aws_region" | tr -d '\n')
fi

# Read profile name override from secrets if present
if [ -r "${SECRETS_DIR}/aws_profile_name" ]; then
    PROFILE=$(cat "${SECRETS_DIR}/aws_profile_name" | tr -d '\n')
fi

mkdir -p "$AWS_DIR"
chmod 700 "$AWS_DIR"

# Write credentials file
if [ "$PROFILE" = "default" ]; then
    CRED_HEADER="[default]"
    CONF_HEADER="[default]"
else
    CRED_HEADER="[${PROFILE}]"
    CONF_HEADER="[profile ${PROFILE}]"
fi

cat > "${AWS_DIR}/credentials" <<EOF
${CRED_HEADER}
aws_access_key_id = ${ACCESS_KEY}
aws_secret_access_key = ${SECRET_KEY}
EOF
chmod 600 "${AWS_DIR}/credentials"

cat > "${AWS_DIR}/config" <<EOF
${CONF_HEADER}
region = ${REGION}
output = json
EOF
chmod 600 "${AWS_DIR}/config"

echo "$(date): AWS credentials synced (profile=${PROFILE}, region=${REGION})" | tee -a "$LOG"
