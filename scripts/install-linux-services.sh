#!/usr/bin/env bash
# Install drydock systemd units on a Linux host. Idempotent.
# Run as root from the drydock repo root (e.g. /root/drydock).
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "install-linux-services.sh must run as root" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Old-name unit cleanup (from the V1 → drydock vocab rename).
# The legacy unit was named with the historical daemon-binary suffix.
# DO NOT run this block through any vocab-sweep tooling — the literal
# 'drydock-' + suffix pair is the on-disk filename we are cleaning up.
LEGACY_SUFFIX="wsd"
LEGACY_UNIT="drydock-${LEGACY_SUFFIX}.service"
if [ -f "/etc/systemd/system/${LEGACY_UNIT}" ]; then
    systemctl stop "${LEGACY_UNIT}" 2>/dev/null || true
    systemctl disable "${LEGACY_UNIT}" 2>/dev/null || true
    rm -f "/etc/systemd/system/${LEGACY_UNIT}"
fi

install -m 0644 "${REPO_ROOT}/base/drydock.service"       /etc/systemd/system/drydock.service
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
systemctl enable drydock.service drydock-desks.service

cat <<EOF
drydock systemd units installed.

  drydock.service        — long-running daemon
  drydock-desks.service  — one-shot resume-on-boot

Enabled for boot. To start now without a reboot:
  systemctl start drydock.service
  systemctl start drydock-desks.service

Status:
  systemctl status drydock.service
  systemctl status drydock-desks.service

Logs:
  journalctl -u drydock.service -n 50
  tail -f /root/.drydock/logs/daemon-systemd.log
  tail -f /root/.drydock/logs/desks-resume.log
EOF
