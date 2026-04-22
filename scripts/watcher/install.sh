#!/bin/bash
# Install the deskwatch watcher as a Harbor-side cron job.
#
# Writes /etc/cron.d/drydock-watcher that runs watcher.py every 15
# minutes. Sink defaults to /var/log/drydock/deskwatch-alerts.md;
# override via env var before running this installer, e.g.:
#
#   WATCHER_SINK=/root/.drydock/alerts.md ./install.sh
#
# Idempotent — re-running overwrites the cron file.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "install-watcher: must run as root (writes /etc/cron.d/)" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/watcher/watcher.py"
SINK="${WATCHER_SINK:-/var/log/drydock/deskwatch-alerts.md}"
STATE="${WATCHER_STATE:-/root/.drydock/watcher-state.json}"

chmod +x "$SCRIPT"
mkdir -p "$(dirname "$SINK")" "$(dirname "$STATE")"

cat > /etc/cron.d/drydock-watcher <<EOF
# Managed by drydock — do not edit. Re-run install.sh to regenerate.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WATCHER_SINK=${SINK}
WATCHER_STATE=${STATE}
*/15 * * * * root ${SCRIPT} >> /var/log/drydock/watcher.log 2>&1
EOF

cat <<EOF
installed drydock-watcher
  script: $SCRIPT
  cron:   /etc/cron.d/drydock-watcher (every 15 min)
  sink:   $SINK
  state:  $STATE
  log:    /var/log/drydock/watcher.log

next:
  - tail -f /var/log/drydock/watcher.log
  - tail -f $SINK
  - fire a manual run: $SCRIPT
EOF
