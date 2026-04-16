FROM ghcr.io/stevefan/drydock-base:{{ base_tag }}

# drydock-base provides: node:20, tailscale, claude code CLI, iptables/ipset,
# init-firewall.sh, start-tailscale.sh, start-remote-control.sh, refresh-firewall-allowlist.sh,
# sudoers entries for tailscaled/tailscale/iptables/ipset/init-firewall, NOPASSWD.
# Layer project-specific tooling on top below.

ARG TZ
ENV TZ="$TZ"

USER root

# Project-specific apt installs go here. Examples:
#   apt-get install -y --no-install-recommends python3 python3-pip
RUN apt-get update && apt-get install -y --no-install-recommends \
    less procps fzf zsh nano vim \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Preserve FIREWALL_EXTRA_DOMAINS / FIREWALL_IPV6_HOSTS across sudo init-firewall.sh.
# Without this the env vars get stripped and the allowlist skips your additions.
RUN echo 'Defaults env_keep += "FIREWALL_EXTRA_DOMAINS FIREWALL_IPV6_HOSTS"' \
    > /etc/sudoers.d/firewall-env \
  && chmod 0440 /etc/sudoers.d/firewall-env

# bash history volume target
RUN mkdir -p /commandhistory \
  && touch /commandhistory/.bash_history \
  && chown -R node /commandhistory

# workspaceMount target
RUN mkdir -p /workspace && chown node:node /workspace
WORKDIR /workspace

USER node

ENV SHELL=/bin/zsh
ENV EDITOR=nano
ENV VISUAL=nano
