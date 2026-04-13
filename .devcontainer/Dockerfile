FROM ghcr.io/stevefan/drydock-base:v1

ARG TZ
ENV TZ="$TZ"

# Drydock-dev additions (on top of the base image's infra layer).
# Root for package installs; switch back to node at the end.
USER root

# Dev-environment tooling not in the base image.
# Base already provides: bash, curl, git, sudo, iptables/ipset, dnsutils, iproute2, jq, ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    less \
    procps \
    fzf \
    zsh \
    man-db \
    unzip \
    gnupg2 \
    gh \
    nano \
    vim \
    python3 \
    python3-pip \
    python3-venv \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Docker CLI (talks to host Docker via socket mount — no daemon inside).
# Used during drydock dev for testing the devcontainer flow.
RUN install -m 0755 -d /etc/apt/keyrings && \
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
  chmod a+r /etc/apt/keyrings/docker.asc && \
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list && \
  apt-get update && \
  apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
  apt-get clean && rm -rf /var/lib/apt/lists/*

# Ensure default node user has access to /usr/local/share for npm globals
RUN mkdir -p /usr/local/share/npm-global && \
  chown -R node:node /usr/local/share

# Persist bash history
RUN SNIPPET="export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  && mkdir /commandhistory \
  && touch /commandhistory/.bash_history \
  && chown -R node /commandhistory

# Orientation env var (base doesn't set this since it's for any workspace type)
ENV DEVCONTAINER=true

# Drydock dev mounts the repo at /workspace, not the base's default /workspaces
RUN mkdir -p /workspace && chown node:node /workspace
WORKDIR /workspace

# git-delta for nicer diffs during drydock dev
ARG GIT_DELTA_VERSION=0.18.2
RUN ARCH=$(dpkg --print-architecture) && \
  wget "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  dpkg -i "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  rm "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb"

# Override the base image's script copies with symlinks to the workspace-bound base/ sources
# so drydock-dev edits to the scripts take effect live without rebuilding the image.
RUN ln -sf /workspace/base/init-firewall.sh /usr/local/bin/init-firewall.sh && \
  ln -sf /workspace/base/start-remote-control.sh /usr/local/bin/start-remote-control.sh && \
  ln -sf /workspace/base/start-tailscale.sh /usr/local/bin/start-tailscale.sh

# Docker socket access — node in docker group; GID fixed up at runtime via postCreateCommand
RUN groupadd -g 999 docker 2>/dev/null || true && \
  usermod -aG docker node

USER node

# Global npm prefix for any packages drydock dev wants beyond what base installs
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=$PATH:/usr/local/share/npm-global/bin

ENV SHELL=/bin/zsh
ENV EDITOR=nano
ENV VISUAL=nano

ARG ZSH_IN_DOCKER_VERSION=1.2.0
RUN sh -c "$(wget -O- https://github.com/deluan/zsh-in-docker/releases/download/v${ZSH_IN_DOCKER_VERSION}/zsh-in-docker.sh)" -- \
  -p git \
  -p fzf \
  -a "source /usr/share/doc/fzf/examples/key-bindings.zsh" \
  -a "source /usr/share/doc/fzf/examples/completion.zsh" \
  -a "export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  -x

# Python venv for drydock CLI dev
RUN python3 -m venv /home/node/.venv
ENV PATH="/home/node/.venv/bin:$PATH"
