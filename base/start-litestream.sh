#!/bin/bash
# LiteStream SQLite replication manager for drydock desks.
#
# Restores from S3 on startup (if a replica exists), then starts
# continuous replication in the background. Designed to run in the
# postStartCommand chain after init-firewall.sh (needs network access
# to S3).
#
# Configuration via environment variables:
#   LITESTREAM_DB_PATH     — local SQLite database path (required)
#   LITESTREAM_S3_BUCKET   — S3 bucket name (required)
#   LITESTREAM_S3_PATH     — path within the bucket (default: derived from desk name)
#   LITESTREAM_S3_REGION   — AWS region (default: us-west-2)
#   LITESTREAM_S3_ENDPOINT — custom S3 endpoint (optional, for MinIO etc.)
#   AWS_ACCESS_KEY_ID      — AWS credentials (required)
#   AWS_SECRET_ACCESS_KEY  — AWS credentials (required)
#
# Skip entirely if LITESTREAM_DB_PATH is not set (desk doesn't use replication).

set -euo pipefail

LOG="/tmp/litestream.log"

if [ -z "${LITESTREAM_DB_PATH:-}" ]; then
    echo "LITESTREAM_DB_PATH not set; skipping LiteStream replication" | tee -a "$LOG"
    exit 0
fi

if [ -z "${LITESTREAM_S3_BUCKET:-}" ]; then
    echo "ERROR: LITESTREAM_S3_BUCKET required when LITESTREAM_DB_PATH is set" | tee -a "$LOG"
    exit 1
fi

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    echo "ERROR: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY required for LiteStream" | tee -a "$LOG"
    exit 1
fi

REGION="${LITESTREAM_S3_REGION:-us-west-2}"
S3_PATH="${LITESTREAM_S3_PATH:-${DRYDOCK_WORKSPACE_NAME:-desk}/$(basename "$LITESTREAM_DB_PATH")}"

# Generate litestream.yml from environment
LITESTREAM_CONFIG="/tmp/litestream.yml"
cat > "$LITESTREAM_CONFIG" <<YAML
access-key-id: ${AWS_ACCESS_KEY_ID}
secret-access-key: ${AWS_SECRET_ACCESS_KEY}

dbs:
  - path: ${LITESTREAM_DB_PATH}
    replicas:
      - type: s3
        bucket: ${LITESTREAM_S3_BUCKET}
        path: ${S3_PATH}
        region: ${REGION}
        sync-interval: 10s
        snapshot-interval: 1h
YAML

echo "$(date): LiteStream config generated" | tee -a "$LOG"
echo "  db:     $LITESTREAM_DB_PATH" | tee -a "$LOG"
echo "  bucket: $LITESTREAM_S3_BUCKET" | tee -a "$LOG"
echo "  path:   $S3_PATH" | tee -a "$LOG"
echo "  region: $REGION" | tee -a "$LOG"

# Restore from S3 if a replica exists (graceful for new databases)
echo "$(date): Restoring from S3 (if replica exists)..." | tee -a "$LOG"
if litestream restore -config "$LITESTREAM_CONFIG" -if-replica-exists "$LITESTREAM_DB_PATH" >> "$LOG" 2>&1; then
    if [ -f "$LITESTREAM_DB_PATH" ]; then
        SIZE=$(stat -c%s "$LITESTREAM_DB_PATH" 2>/dev/null || stat -f%z "$LITESTREAM_DB_PATH" 2>/dev/null || echo "?")
        echo "$(date): Restored $LITESTREAM_DB_PATH ($SIZE bytes)" | tee -a "$LOG"
    else
        echo "$(date): No replica found on S3; starting fresh" | tee -a "$LOG"
    fi
else
    echo "$(date): WARNING: Restore failed (continuing with local state)" | tee -a "$LOG"
fi

# Start continuous replication in background
echo "$(date): Starting continuous replication..." | tee -a "$LOG"
nohup litestream replicate -config "$LITESTREAM_CONFIG" >> "$LOG" 2>&1 &
disown 2>/dev/null || true
echo "$(date): LiteStream replicating (PID $!)" | tee -a "$LOG"
