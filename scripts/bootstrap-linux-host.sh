#!/usr/bin/env bash
# Drydock host bootstrap — fresh Linux box → working Harbor.
#
# Usage (interactive):
#   curl -fsSL https://raw.githubusercontent.com/stevefan/drydock/main/scripts/bootstrap-linux-host.sh | bash
#
# Usage (unattended, e.g. EC2 user-data):
#   TAILSCALE_AUTHKEY=tskey-xxx HARBOR_HOSTNAME=my-harbor \
#       bash <(curl -fsSL https://raw.githubusercontent.com/stevefan/drydock/main/scripts/bootstrap-linux-host.sh)
#
# Env:
#   TAILSCALE_AUTHKEY  — if set, runs `tailscale up` non-interactively
#   HARBOR_HOSTNAME    — tailnet hostname for this Harbor (defaults to `hostname -s`)
#   GH_TOKEN           — gh respects natively; skip `gh auth login` if set
#   DRYDOCK_REPO/DIR   — override for fork/local clone
#
# Idempotent — safe to re-run. Tested on Ubuntu 24.04 LTS.
#
# Handles: apt deps (docker, tailscale, gh, node/npm, pipx), @devcontainers/cli,
# drydock clone + editable install, `drydock host init`, systemd units (daemon + resume),
# optional unattended tailnet join.
#
# Still interactive after bootstrap (if env vars not set):
#   - tailscale up --hostname=<box>               (device flow)
#   - gh auth login                                (device flow, unless GH_TOKEN set)
#   - (after first drydock create) docker exec … claude /login
#   - (optional) tailscale admin API token → /root/.drydock/daemon-secrets/
#
# See docs/operations/harbor-bootstrap.md for the full walkthrough.

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
    python3 python3-pip pipx

# nodejs/npm: only install via apt if neither is present. Boxes that
# already have Node from NodeSource/nvm/Volta ship their own npm, and
# Debian's npm package pulls in a swarm of node-* deps that conflict
# with non-Debian nodejs installs ("held broken packages").
if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
    log "node/npm: installing via apt (no prior install detected)"
    apt-get install -y -qq nodejs npm
else
    log "node/npm: already present ($(node --version), npm $(npm --version)); skipping apt install"
fi

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

log "drydock CLI (pipx editable)"
if [ ! -d "$DRYDOCK_DIR/.git" ]; then
    git clone "$DRYDOCK_REPO" "$DRYDOCK_DIR"
fi
pipx install --force --editable "$DRYDOCK_DIR" >/dev/null

log "drydock host init (state dirs + gitconfig stub)"
drydock host init

log "systemd units (daemon + resume-on-boot)"
bash "$DRYDOCK_DIR/scripts/install-linux-services.sh"

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    ts_hostname="${HARBOR_HOSTNAME:-$(hostname -s)}"
    log "tailscale up --authkey=*** --hostname=$ts_hostname (unattended)"
    tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname="$ts_hostname" --ssh
else
    log "tailscale: no TAILSCALE_AUTHKEY; run interactively below"
fi

systemctl start drydock.service || true

echo
log "versions"
docker --version
tailscale --version | head -1
devcontainer --version
gh --version | head -1
drydock --version 2>&1 | head -1 || drydock --help 2>&1 | head -1

echo
log "bootstrap done. Remaining interactive steps (skip any already satisfied):"
cat <<EOF

  # 1. Tailnet (skip if TAILSCALE_AUTHKEY was set):
  tailscale up --hostname=<this-box-name> --ssh

  # 2. GitHub (skip if GH_TOKEN env is set):
  gh auth login --hostname github.com --git-protocol https --web
  gh auth setup-git

  # 3. Verify:
  drydock host check
  drydock daemon status

  # Per-project, when you're ready:
  git clone https://github.com/<you>/<project>.git /root/src/<project>
  cat > /root/.drydock/projects/<project>.yaml <<YAML
  repo_path: /root/src/<project>
  tailscale_hostname: <project>
  firewall_extra_domains:
    - <hosts the drydock legitimately needs>
  YAML
  echo -n "<value>" | drydock secret set <project> <key>
  drydock create <project>

See docs/operations/harbor-bootstrap.md for the full walkthrough.
EOF
