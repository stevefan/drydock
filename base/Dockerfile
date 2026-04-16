# drydock-base: infrastructure layer for drydock workspace containers.
# Projects use: FROM ghcr.io/stevefan/drydock-base:v1
FROM node:20

ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update && apt-get install -y --no-install-recommends \
  bash \
  ca-certificates \
  curl \
  dnsutils \
  git \
  aggregate \
  iproute2 \
  iptables \
  ipset \
  jq \
  mosh \
  sudo \
  tmux \
  && apt-get clean && rm -rf /var/lib/apt/lists/* \
  && curl -fsSL https://tailscale.com/install.sh | sh

RUN npm install -g \
  @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
  @devcontainers/cli

COPY init-firewall.sh start-tailscale.sh start-remote-control.sh sync-claude-auth.sh refresh-firewall-allowlist.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/init-firewall.sh \
              /usr/local/bin/start-tailscale.sh \
              /usr/local/bin/start-remote-control.sh \
              /usr/local/bin/sync-claude-auth.sh \
              /usr/local/bin/refresh-firewall-allowlist.sh

RUN echo "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/tailscaled" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/bin/tailscale" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/iptables" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/ipset" >> /etc/sudoers.d/node-drydock && \
  chmod 0440 /etc/sudoers.d/node-drydock

# Pre-create target directories for named-volume mounts with node:node
# ownership so the volumes inherit correct perms on first mount.
# Without this, docker creates empty named volumes owned by root.
RUN mkdir -p /home/node/.claude \
             /home/node/.vscode-server \
             /home/node/.npm \
             /home/node/.cache/pip && \
    chown -R node:node /home/node/.claude \
                        /home/node/.vscode-server \
                        /home/node/.npm \
                        /home/node/.cache
VOLUME /home/node/.claude
VOLUME /home/node/.vscode-server
VOLUME /home/node/.npm
# Narrow shared cache: only /home/node/.cache/pip, not the umbrella.
# Umbrella would shadow project-baked assets like Playwright browsers at
# /home/node/.cache/ms-playwright.
VOLUME /home/node/.cache/pip

USER node
WORKDIR /workspaces

CMD ["/bin/bash"]
