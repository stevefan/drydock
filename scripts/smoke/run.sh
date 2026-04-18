#!/bin/bash
# Smoke-harness runner. Executes one or all scenarios against a live Harbor.
#
# Usage:  run.sh [scenario-name]
# Env:    HARBOR_HOST (ssh target, default: root@drydock-hillsboro)

set -uo pipefail

HARBOR_HOST="${HARBOR_HOST:-root@5.78.146.141}"
HERE=$(cd "$(dirname "$0")" && pwd)
SCENARIOS_DIR="$HERE/scenarios"

export HARBOR_HOST

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

run_one() {
    local name=$1
    local script="$SCENARIOS_DIR/$name.sh"
    if [ ! -x "$script" ]; then
        red "scenario not found: $name ($script)"
        return 1
    fi
    bold ""
    bold "=== $name ==="
    if "$script"; then
        green "  PASS: $name"
        return 0
    else
        red "  FAIL: $name"
        return 1
    fi
}

if [ $# -ge 1 ]; then
    run_one "$1"
    exit $?
fi

# No arg -> all scenarios alphabetically.
fails=0
for script in "$SCENARIOS_DIR"/*.sh; do
    name=$(basename "$script" .sh)
    run_one "$name" || fails=$((fails + 1))
done

bold ""
if [ "$fails" -eq 0 ]; then
    green "all scenarios passed"
    exit 0
else
    red "$fails scenario(s) failed"
    exit 1
fi
