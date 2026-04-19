#!/bin/bash
# Keep s3fs mounts alive across STS expiry.
#
# STS credentials issued for STORAGE_MOUNT leases expire (typically 1h,
# capped at the backend session duration). s3fs caches creds from env
# vars at mount time and does not refresh them — once the session token
# expires, every s3fs operation returns ExpiredToken and the mount goes
# silently dead.
#
# This daemon runs in the background after setup-storage-mounts.sh
# finishes. It reads the state file written by setup, sleeps until
# LEAD_SECS before the earliest known expiry, then for each mount:
# re-issues the lease (RequestCapability), unmounts, and remounts with
# the fresh creds. Brief disruption (~1s) per mount per refresh cycle.
#
# Idempotent via pidfile at /tmp/storage-mounts-refresh.pid.

set -uo pipefail

LOG=/tmp/storage-mounts.log
PIDFILE=/tmp/storage-mounts-refresh.pid
STATE=/tmp/storage-mounts-state.json
LEAD_SECS=${STORAGE_REFRESH_LEAD_SECS:-600}
MIN_SLEEP=${STORAGE_REFRESH_MIN_SLEEP:-30}

log() { echo "$(date +%H:%M:%S) refresh: $*" >> "$LOG"; }

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    log "already running pid=$(cat "$PIDFILE"); exiting"
    exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

if ! [ -s "$STATE" ]; then
    log "no state file at $STATE; nothing to refresh"
    exit 0
fi

unmount_target() {
    local t="$1"
    fusermount -u "$t" 2>>"$LOG" \
        || fusermount3 -u "$t" 2>>"$LOG" \
        || umount "$t" 2>>"$LOG"
}

remount_entry() {
    local bucket="$1" prefix="$2" mode="$3" region="$4" target="$5"
    local s3fs_src="$bucket"
    [ -n "$prefix" ] && s3fs_src="$bucket:/$prefix"

    local opts=(
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
        s3fs "$s3fs_src" "$target" "${opts[@]}" 2>>"$LOG"
}

refresh_cycle() {
    # Each iteration in the state file re-requests a lease (overwriting
    # /run/secrets/aws_*), unmounts, remounts. The final lease's expiry
    # becomes the governing deadline for the next wake-up.
    while IFS= read -r entry; do
        local source target mode region bucket prefix
        source=$(echo "$entry" | jq -r '.source')
        target=$(echo "$entry" | jq -r '.target')
        mode=$(echo "$entry"   | jq -r '.mode')
        region=$(echo "$entry" | jq -r '.region')
        bucket=$(echo "$entry" | jq -r '.bucket')
        prefix=$(echo "$entry" | jq -r '.prefix')

        log "refreshing $source -> $target"

        if ! drydock-rpc RequestCapability \
                type=STORAGE_MOUNT \
                "scope.bucket=$bucket" \
                "scope.prefix=$prefix" \
                "scope.mode=$mode" >/dev/null 2>>"$LOG"; then
            log "  ERROR: RequestCapability failed for $source"
            continue
        fi

        unmount_target "$target"

        if ! remount_entry "$bucket" "$prefix" "$mode" "$region" "$target"; then
            log "  ERROR: s3fs remount failed for $target"
            continue
        fi
        log "  remounted $target"
    done < <(jq -c '.[]' "$STATE")
}

while true; do
    exp_iso=$(cat /run/secrets/aws_session_expiration 2>/dev/null || true)
    if [ -z "$exp_iso" ]; then
        log "no aws_session_expiration file; retrying in 60s"
        sleep 60
        continue
    fi

    exp_epoch=$(date -u -d "$exp_iso" +%s 2>/dev/null || true)
    if [ -z "$exp_epoch" ]; then
        log "unparseable expiration: $exp_iso; retrying in 60s"
        sleep 60
        continue
    fi

    now=$(date -u +%s)
    sleep_for=$(( exp_epoch - now - LEAD_SECS ))
    if [ "$sleep_for" -lt "$MIN_SLEEP" ]; then
        sleep_for=$MIN_SLEEP
    fi
    log "next refresh in ${sleep_for}s (exp=$exp_iso)"
    sleep "$sleep_for"

    refresh_cycle
done
