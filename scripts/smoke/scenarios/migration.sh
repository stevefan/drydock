#!/bin/bash
# smoke: migration primitive — image_bump dry-run + execute (Phase 2a.4 M1).
#
# Validates: planner produces a structured plan; precheck passes for
# clean state; executor walks PRECHECK→DRAIN→SNAPSHOT→STOP→MUTATE
# →START→VERIFY→CLEANUP and returns terminal=completed; snapshot tarball
# + manifest land on disk; registry's image field gets updated.
#
# Setup:
# - Create a throwaway desk with a synthetic image:v1 marker.
# - --dry-run first to confirm plan shape.
# - Execute against image:v2; verify the registry row mutated and
#   the snapshot tarball exists.
# - Tear down.
#
# We don't actually pull a new image — image_bump just changes the
# registry's image field; the new image is fetched on next `drydock
# create`. The smoke test stops at the executor's terminal state.

set -uo pipefail
HARBOR="${HARBOR_HOST:?HARBOR_HOST unset}"
SSH="ssh $HARBOR"
NAME="smoke-migration-$$"

cleanup() {
    # Force-cleanup the desk; remove any migration record + snapshot dir.
    $SSH "drydock destroy $NAME --force" >/dev/null 2>&1 || true
    $SSH "rm -f /root/.drydock/projects/$NAME.yaml" >/dev/null 2>&1 || true
    $SSH "python3 -c '
import sqlite3
c = sqlite3.connect(\"/root/.drydock/registry.db\")
c.execute(\"DELETE FROM migrations WHERE drydock_id = ?\", (\"dock_$NAME\",))
c.commit()
'" >/dev/null 2>&1 || true
    $SSH "rm -rf /root/.drydock/migrations/mig_*$NAME* 2>/dev/null" || true
}
trap cleanup EXIT

# Project YAML + registry row. We don't need a real container or
# worktree-on-disk: the executor's STOP stage handles missing
# container_id gracefully, and START skips when worktree_path is empty.
# The full devcontainer-up path is exercised by unit tests with mocked
# _resume_desk; this smoke validates the state-machine stage sequence
# against the real registry + filesystem.
$SSH "cat > /root/.drydock/projects/$NAME.yaml" <<YAML
repo_path: /tmp/$NAME-repo
YAML

# Insert a Drydock row directly via the pipx python — no container.
$SSH "/root/.local/share/pipx/venvs/drydock/bin/python -c '
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
r = Registry()
r.create_drydock(Drydock(
    name=\"$NAME\", project=\"$NAME\",
    repo_path=\"/tmp/$NAME-repo\",
    branch=\"main\",
    image=\"img:v1\",
    state=\"defined\", container_id=\"\",
))
r.close()
'" || { echo "FAIL: couldn't seed registry"; exit 1; }

# M4 pre-flight probes refuse if target image isn't pulled locally.
# Use real images that are guaranteed present on the smoke harbor:
# alpine:3 → alpine:3 is a no-op for the planner (same), so use two
# different real-images. drydock-base v1 and v1.0.9 are both pulled.
FROM_IMG="ghcr.io/stevefan/drydock-base:v1.0.9"
TO_IMG="ghcr.io/stevefan/drydock-base:v1"

# Reseed with the FROM_IMG so the bump is plausible.
$SSH "/root/.local/share/pipx/venvs/drydock/bin/python -c '
import sqlite3
c = sqlite3.connect(\"/root/.drydock/registry.db\")
c.execute(\"UPDATE drydocks SET image = ? WHERE name = ?\", (\"$FROM_IMG\", \"$NAME\"))
c.commit()
'"

# 1. Dry-run.
dry_resp=$($SSH "drydock --json --dry-run migrate $NAME --target image=$TO_IMG")
dry_kind=$(echo "$dry_resp" | python3 -c "
import sys, json
print(json.load(sys.stdin)['plan']['target_kind'])
")
if [ "$dry_kind" != "image_bump" ]; then
    echo "FAIL: dry-run plan target_kind != image_bump (got $dry_kind)"
    echo "$dry_resp"
    exit 1
fi
dry_executed=$(echo "$dry_resp" | python3 -c "
import sys, json
print(json.load(sys.stdin)['executed'])
")
if [ "$dry_executed" != "False" ]; then
    echo "FAIL: dry-run reported executed=$dry_executed"
    exit 1
fi
echo "dry-run plan: target_kind=image_bump, executed=False: OK"

# 2. Execute.
exec_resp=$($SSH "drydock --json migrate $NAME --target image=$TO_IMG")
terminal=$(echo "$exec_resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
out = d.get('outcome') or {}
print(out.get('terminal_status', 'NONE'))
")
if [ "$terminal" != "completed" ]; then
    echo "FAIL: execute terminal_status != completed (got $terminal)"
    echo "$exec_resp"
    exit 1
fi
echo "execute: terminal_status=completed: OK"

# 3. Registry's image field updated.
new_image=$($SSH "drydock --json inspect $NAME" | python3 -c "
import sys, json
print(json.load(sys.stdin).get('image', ''))
")
if [ "$new_image" != "$TO_IMG" ]; then
    echo "FAIL: registry image not bumped (got '$new_image', expected '$TO_IMG')"
    exit 1
fi
echo "registry image: $new_image: OK"

# 4. Snapshot tarball + manifest exist on disk.
snap_path=$(echo "$exec_resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
out = d.get('outcome') or {}
print(out.get('snapshot_path', ''))
")
if [ -z "$snap_path" ]; then
    echo "FAIL: no snapshot_path in outcome"
    exit 1
fi
$SSH "test -f $snap_path/snapshot.tgz && test -f $snap_path/manifest.json" || {
    echo "FAIL: snapshot files missing at $snap_path"
    exit 1
}
echo "snapshot files: $snap_path/{snapshot.tgz, manifest.json}: OK"

# 5. Stage list — all 8 forward stages, all ok.
stages=$(echo "$exec_resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
out = d.get('outcome') or {}
stages = out.get('stages') or []
print(' '.join(s['stage'] for s in stages))
")
expected="precheck drain snapshot stop mutate start verify cleanup"
if [ "$stages" != "$expected" ]; then
    echo "FAIL: unexpected stage sequence"
    echo "  expected: $expected"
    echo "  got:      $stages"
    exit 1
fi
echo "stages: $stages: OK"

echo "migration end-to-end (image_bump dry-run + execute): OK"
