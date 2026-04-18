#!/bin/bash
# Setup S3-backed FUSE mounts declared via STORAGE_MOUNTS_JSON.
#
# Runs at postStartCommand after init-firewall + start-tailscale. For each
# entry in STORAGE_MOUNTS_JSON, requests a STORAGE_MOUNT lease via
# drydock-rpc (the daemon mints scoped AWS STS creds, writing aws_* files
# into /run/secrets/), then mounts the bucket+prefix at the declared
# target with s3fs.
#
# Errors are logged but don't fail the postStartCommand chain — tailscale
# and remote-control should still come up even if a storage mount is
# mis-declared. Check /tmp/storage-mounts.log on the drydock to diagnose.

set -uo pipefail

LOG=/tmp/storage-mounts.log
: > "$LOG"
log() { echo "$(date +%H:%M:%S) $*" | tee -a "$LOG"; }

if [ -z "${STORAGE_MOUNTS_JSON:-}" ] || [ "${STORAGE_MOUNTS_JSON}" = "[]" ]; then
    log "storage-mounts: nothing declared; skipping"
    exit 0
fi

count=$(echo "$STORAGE_MOUNTS_JSON" | jq '. | length')
log "storage-mounts: setting up $count mount(s)"

# Single lease covers all entries — each RequestCapability overwrites the
# aws_* files (see capability-broker.md §7). To support N mounts with one
# shared credential set, request a single lease whose scope is the UNION
# of requested buckets/prefixes. For now we accept one-at-a-time: each
# mount gets its own lease request, and each subsequent request
# supersedes the prior. Last lease wins for the files; earlier mounts
# keep their cached creds via s3fs's in-memory state (s3fs re-reads on
# expiry, which requires its own refresh mechanism — TODO for Phase C.1).
i=0
while IFS= read -r entry; do
    i=$((i + 1))
    source=$(echo "$entry" | jq -r '.source')
    target=$(echo "$entry" | jq -r '.target')
    mode=$(echo "$entry" | jq -r '.mode // "ro"')
    region=$(echo "$entry" | jq -r '.region // "us-west-2"')

    # Parse s3://bucket/prefix
    body=${source#s3://}
    bucket=${body%%/*}
    if [ "$body" = "$bucket" ]; then
        prefix=""
    else
        prefix=${body#*/}
        prefix=${prefix%/}
    fi

    log "  [$i/$count] $source -> $target ($mode, $region)"

    # Request lease; scope matches what the daemon expects (see
    # capability_handlers._validate_storage_scope). drydock-rpc builds
    # nested dicts from dotted keys.
    rpc_out=$(drydock-rpc RequestCapability \
        type=STORAGE_MOUNT \
        "scope.bucket=$bucket" \
        "scope.prefix=$prefix" \
        "scope.mode=$mode" 2>&1) || {
        log "    ERROR: RequestCapability failed: $rpc_out"
        continue
    }

    if ! [ -r /run/secrets/aws_access_key_id ]; then
        log "    ERROR: lease issued but aws_access_key_id missing in /run/secrets/"
        continue
    fi

    mkdir -p "$target" 2>>"$LOG" || {
        log "    ERROR: mkdir $target failed"
        continue
    }

    s3fs_src="$bucket"
    [ -n "$prefix" ] && s3fs_src="$bucket:/$prefix"

    opts=(
        -o "use_path_request_style"
        -o "url=https://s3.$region.amazonaws.com"
        -o "endpoint=$region"
        -o "allow_other"
        -o "umask=0022"
    )
    [ "$mode" = "ro" ] && opts+=(-o "ro")

    AWS_ACCESS_KEY_ID=$(cat /run/secrets/aws_access_key_id) \
    AWS_SECRET_ACCESS_KEY=$(cat /run/secrets/aws_secret_access_key) \
    AWS_SESSION_TOKEN=$(cat /run/secrets/aws_session_token) \
        s3fs "$s3fs_src" "$target" "${opts[@]}" 2>>"$LOG" || {
        log "    ERROR: s3fs mount failed (see tail above)"
        continue
    }

    log "    mounted"
done < <(echo "$STORAGE_MOUNTS_JSON" | jq -c '.[]')

log "storage-mounts: done"
exit 0
