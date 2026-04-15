#!/usr/bin/env bash
# Drydock host bootstrap — gets a fresh Linux box ready for `ws create`.
#
# Usage on the target box (as root):
#   curl -fsSL https://raw.githubusercontent.com/stevefan/drydock/main/scripts/bootstrap-linux-host.sh | bash
# OR after cloning:
#   bash drydock/scripts/bootstrap-linux-host.sh
#
# Idempotent — safe to re-run.
# Tested on Ubuntu 24.04 LTS. Debian-family should work; other distros need
# adapter steps for the package install commands.
#
# Deterministic steps this script handles:
#   - apt deps: docker (+buildx), tailscale, python3+pipx, git, gh, node/npm
#   - @devcontainers/cli (npm global)
#   - /root/.drydock/{projects,secrets,worktrees,overlays,daemon-secrets,logs}
#   - /var/log/drydock/
#   - /root/.gitconfig stub (devcontainer bind-mount needs it to exist)
#   - drydock clone + pipx editable install
#
# Interactive steps you do AFTER (one-time per host):
#   - tailscale up --hostname=<box-name>            (device flow)
#   - gh auth login --hostname github.com --git-protocol https --web  (device flow)
#   - gh auth setup-git
#   - (after first ws create) docker exec -u node <ctr> claude /login (device flow)
#   - (optional) generate Tailscale API token, write to
#     /root/.drydock/daemon-secrets/{tailscale_admin_token,tailscale_tailnet}
#
# See docs/host-bootstrap.md for full context.

set -euo pipefail

DRYDOCK_REPO="${DRYDOCK_REPO:-https://github.com/stevefan/drydock.git}"
DRYDOCK_DIR="${DRYDOCK_DIR:-/root/drydock}"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (sudo)." >&2
    exit 1
fi

if ! command -v apt-get >/dev/null; then
    echo "ERROR: this script targets Debian-family (apt). Adapt for your distro." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
export PATH="/root/.local/bin:$PATH"

log() { echo ">>> $*"; }

log "apt update + base deps"
apt-get update -qq
apt-get install -y -qq \
    apt-transport-https ca-certificates curl gnupg lsb-release \
    git \
    python3 python3-pip pipx \
    nodejs npm

log "docker"
if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin
fi

log "tailscale"
if ! command -v tailscale >/dev/null; then
    curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).noarmor.gpg" \
        | tee /usr/share/keyrings/tailscale-archive-keyring.gpg > /dev/null
    curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).tailscale-keyring.list" \
        | tee /etc/apt/sources.list.d/tailscale.list > /dev/null
    apt-get update -qq
    apt-get install -y -qq tailscale
fi

log "gh CLI"
if ! command -v gh >/dev/null; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list
    apt-get update -qq
    apt-get install -y -qq gh
fi

log "@devcontainers/cli (npm global)"
if ! command -v devcontainer >/dev/null; then
    npm install -g @devcontainers/cli >/dev/null
fi

log "drydock state directories"
mkdir -p /root/.drydock/{projects,secrets,worktrees,overlays,daemon-secrets,logs}
chmod 700 /root/.drydock/secrets /root/.drydock/daemon-secrets
mkdir -p /var/log/drydock

log "/root/.gitconfig stub (devcontainer bind-mount needs it)"
[ -f /root/.gitconfig ] || touch /root/.gitconfig

log "drydock CLI (pipx editable)"
if [ ! -d "$DRYDOCK_DIR/.git" ]; then
    git clone "$DRYDOCK_REPO" "$DRYDOCK_DIR"
fi
pipx install --force --editable "$DRYDOCK_DIR" >/dev/null

echo
log "versions"
docker --version
tailscale --version | head -1
devcontainer --version
gh --version | head -1
ws --version 2>&1 | head -1 || ws --help 2>&1 | head -1

echo
log "done. Interactive next steps:"
cat <<EOF

  tailscale up --hostname=<this-box-name>
  gh auth login --hostname github.com --git-protocol https --web
  gh auth setup-git

  # Per-project (after the auth steps):
  git clone https://github.com/<you>/<project>.git /root/src/<project>
  cat > /root/.drydock/projects/<project>.yaml <<YAML
  repo_path: /root/src/<project>
  workspace_subdir: <subdir-if-monorepo>
  tailscale_hostname: <project>
  firewall_extra_domains:
    - <hosts the desk legitimately needs>
  YAML
  echo -n "<value>" | ws secret set <project> <key>
  ws create <project>

  # After first ws create, log Claude in (one-time, persists to volume):
  docker exec -u node \$(docker ps -q --filter label=devcontainer.local_folder=/root/.drydock/worktrees/ws_<project>/<subdir>) claude /login

See docs/host-bootstrap.md for full context.
EOF
