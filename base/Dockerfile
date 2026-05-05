# drydock-base: infrastructure layer for drydock workspace containers.
# Projects use: FROM ghcr.io/stevefan/drydock-base:v1
FROM node:20

ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update && apt-get install -y --no-install-recommends \
  bash \
  ca-certificates \
  curl \
  dnsutils \
  fuse \
  git \
  aggregate \
  iproute2 \
  iptables \
  ipset \
  jq \
  mosh \
  s3fs \
  sudo \
  tmux \
  unzip \
  && apt-get clean && rm -rf /var/lib/apt/lists/* \
  && curl -fsSL https://tailscale.com/install.sh | sh

RUN npm install -g \
  @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
  @devcontainers/cli

# awscli v2 (~90 MB). Bundled in drydock-base so fleet-agent / provisioner
# drydocks (infra, future per-service provisioners) can `aws sts assume-role`
# from inside their container without a runtime install that vanishes on
# every recreate. Workers that don't need it pay the pull cost once per
# Harbor; that's acceptable overhead for the simpler "click-and-go" story.
# TARGETARCH is set automatically by buildx + docker build; defaults to the
# build host's arch for single-arch builds.
ARG TARGETARCH
RUN case "${TARGETARCH:-amd64}" in \
      amd64) AWS_ARCH=x86_64 ;; \
      arm64) AWS_ARCH=aarch64 ;; \
      *) echo "unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws && \
    aws --version

COPY init-firewall.sh start-tailscale.sh start-remote-control.sh sync-claude-auth.sh refresh-firewall-allowlist.sh add-allowed-domain.sh setup-storage-mounts.sh refresh-storage-mounts.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/init-firewall.sh \
              /usr/local/bin/start-tailscale.sh \
              /usr/local/bin/start-remote-control.sh \
              /usr/local/bin/sync-claude-auth.sh \
              /usr/local/bin/refresh-firewall-allowlist.sh \
              /usr/local/bin/add-allowed-domain.sh \
              /usr/local/bin/setup-storage-mounts.sh \
              /usr/local/bin/refresh-storage-mounts.sh

# System-wide tmux config so SSH'd-in interactive sessions get
# host-clipboard-friendly copy/paste out of the box. Used as the
# backup path for `claude /login` and other interactive paste flows
# that the in-memory OAuth refresh can't replace. Users can extend
# via ~/.tmux.conf (sourced after this).
COPY tmux.conf /etc/tmux.conf

RUN echo "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/tailscaled" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/bin/tailscale" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/iptables" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/ipset" >> /etc/sudoers.d/node-drydock && \
  echo 'Defaults env_keep += "FIREWALL_EXTRA_DOMAINS FIREWALL_IPV6_HOSTS FIREWALL_AWS_IP_RANGES FIREWALL_REFRESH_INTERVAL"' >> /etc/sudoers.d/node-drydock && \
  chmod 0440 /etc/sudoers.d/node-drydock

# git safe.directory = * system-wide. Inside a drydock desk, the container
# itself is the trust boundary — individual paths within /workspace or
# /root/.drydock don't need per-path ownership validation. Host-bind-mount
# uid mismatches (worktree owned by root-on-host with container running as
# node:1000) would otherwise trip git's "dubious ownership" guard, which
# blocked the auction-crawl smart operator from committing to ws/operator-*
# branches on 2026-04-15 (had to extract the patch via host stash instead).
RUN git config --system safe.directory '*'

# Pre-create target directories for named-volume mounts with node:node
# ownership so the volumes inherit correct perms on first mount.
# Without this, docker creates empty named volumes owned by root.
RUN mkdir -p /home/node/.claude \
             /home/node/.vscode-server/extensions \
             /home/node/.npm \
             /home/node/.cache/pip && \
    chown -R node:node /home/node/.claude \
                        /home/node/.vscode-server \
                        /home/node/.npm \
                        /home/node/.cache

# Pre-install Anthropic Claude Code VS Code extension into the seed
# directory for the drydock-vscode-server named volume. Docker copies
# image contents into a freshly created volume on first mount, so
# new desks have the extension immediately on `ws attach` — no
# install-on-first-attach delay. Existing desks with already-populated
# volumes are unaffected; recreate the volume to pick up.
ARG CLAUDE_VSCODE_PUBLISHER=anthropic
ARG CLAUDE_VSCODE_NAME=claude-code
RUN set -e; \
    EXT_DIR=/home/node/.vscode-server/extensions; \
    TMP=$(mktemp -d); cd "$TMP"; \
    curl -fsSL -H 'Accept: application/octet-stream' \
      "https://${CLAUDE_VSCODE_PUBLISHER}.gallery.vsassets.io/_apis/public/gallery/publisher/${CLAUDE_VSCODE_PUBLISHER}/extension/${CLAUDE_VSCODE_NAME}/latest/assetbyname/Microsoft.VisualStudio.Services.VSIXPackage" \
      -o ext.vsix; \
    unzip -q ext.vsix -d unpacked; \
    VERSION=$(jq -r '.version' unpacked/extension/package.json); \
    DEST="$EXT_DIR/${CLAUDE_VSCODE_PUBLISHER}.${CLAUDE_VSCODE_NAME}-${VERSION}"; \
    mkdir -p "$DEST"; \
    cp -r unpacked/extension/. "$DEST/"; \
    cd /; rm -rf "$TMP"; \
    chown -R node:node /home/node/.vscode-server; \
    echo "pre-installed ${CLAUDE_VSCODE_PUBLISHER}.${CLAUDE_VSCODE_NAME} v${VERSION}"
VOLUME /home/node/.claude
VOLUME /home/node/.vscode-server
VOLUME /home/node/.npm
# Narrow shared cache: only /home/node/.cache/pip, not the umbrella.
# Umbrella would shadow project-baked assets like Playwright browsers at
# /home/node/.cache/ms-playwright.
VOLUME /home/node/.cache/pip

# /mnt is node-writable so storage_mounts (s3fs FUSE mounts) can create
# target subdirs at container start without root help.
RUN chown node:node /mnt && \
    sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf

USER node
WORKDIR /workspaces

CMD ["/bin/bash"]
