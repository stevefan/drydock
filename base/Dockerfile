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
  sudo \
  && apt-get clean && rm -rf /var/lib/apt/lists/* \
  && curl -fsSL https://tailscale.com/install.sh | sh

RUN npm install -g \
  @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
  @devcontainers/cli

COPY init-firewall.sh start-tailscale.sh start-remote-control.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/init-firewall.sh \
              /usr/local/bin/start-tailscale.sh \
              /usr/local/bin/start-remote-control.sh

RUN echo "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/tailscaled" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/bin/tailscale" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/iptables" >> /etc/sudoers.d/node-drydock && \
  echo "node ALL=(root) NOPASSWD: /usr/sbin/ipset" >> /etc/sudoers.d/node-drydock && \
  chmod 0440 /etc/sudoers.d/node-drydock

RUN mkdir -p /home/node/.claude && chown node:node /home/node/.claude
VOLUME /home/node/.claude

USER node
WORKDIR /workspaces

CMD ["/bin/bash"]
