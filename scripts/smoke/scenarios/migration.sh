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

# Project YAML + registry row. We don't need a real container — the
# executor's STOP stage handles a missing container gracefully.
$SSH "cat > /root/.drydock/projects/$NAME.yaml" <<YAML
repo_path: /tmp/$NAME-repo
YAML
$SSH "mkdir -p /tmp/$NAME-repo && cd /tmp/$NAME-repo && git init -q && git -c user.email=t@t.com -c user.name=t commit --allow-empty -m init -q"

# Insert a Drydock row directly via the pipx python — no container.
$SSH "/root/.local/share/pipx/venvs/drydock/bin/python -c '
from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
r = Registry()
r.create_drydock(Drydock(
    name=\"$NAME\", project=\"$NAME\",
    repo_path=\"/tmp/$NAME-repo\",
    worktree_path=\"/tmp/$NAME-repo\",
    branch=\"main\",
    image=\"img:v1\",
    state=\"defined\", container_id=\"\",
))
r.close()
'" || { echo "FAIL: couldn't seed registry"; exit 1; }

# 1. Dry-run.
dry_resp=$($SSH "drydock --json --dry-run migrate $NAME --target image=img:v2")
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
exec_resp=$($SSH "drydock --json migrate $NAME --target image=img:v3")
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
if [ "$new_image" != "img:v3" ]; then
    echo "FAIL: registry image not bumped (got '$new_image')"
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
