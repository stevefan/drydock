#!/usr/bin/env bash
# Install drydock systemd units on a Linux host. Idempotent.
# Run as root from the drydock repo root (e.g. /root/drydock).
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "install-linux-services.sh must run as root" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -m 0644 "${REPO_ROOT}/base/drydock-wsd.service"   /etc/systemd/system/drydock-wsd.service
install -m 0644 "${REPO_ROOT}/base/drydock-desks.service" /etc/systemd/system/drydock-desks.service
install -m 0755 "${REPO_ROOT}/scripts/drydock-resume-desks" /usr/local/bin/drydock-resume-desks
install -m 0755 "${REPO_ROOT}/scripts/drydock-stop-desks"   /usr/local/bin/drydock-stop-desks
# drydock-rpc is bind-mounted into each drydock container (see overlay.py).
# Place it at ~/.drydock/bin/drydock-rpc — the overlay's source path — AND
# at /usr/local/bin for Harbor-side convenience.
DRYDOCK_BIN="/root/.drydock/bin"
mkdir -p "${DRYDOCK_BIN}"
install -m 0755 "${REPO_ROOT}/scripts/drydock-rpc"          "${DRYDOCK_BIN}/drydock-rpc"
install -m 0755 "${REPO_ROOT}/scripts/drydock-rpc"          /usr/local/bin/drydock-rpc

mkdir -p /root/.drydock/logs

systemctl daemon-reload
systemctl enable drydock-wsd.service drydock-desks.service

cat <<EOF
drydock systemd units installed.

  drydock-wsd.service    — long-running daemon
  drydock-desks.service  — one-shot resume-on-boot

Enabled for boot. To start now without a reboot:
  systemctl start drydock-wsd.service
  systemctl start drydock-desks.service

Status:
  systemctl status drydock-wsd.service
  systemctl status drydock-desks.service

Logs:
  journalctl -u drydock-wsd.service -n 50
  tail -f /root/.drydock/logs/wsd-systemd.log
  tail -f /root/.drydock/logs/desks-resume.log
EOF
