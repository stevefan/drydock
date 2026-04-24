FROM ghcr.io/stevefan/drydock-base:v1

ARG TZ
ENV TZ="$TZ"

USER root

# Syncthing from the official apt repo — Debian's package lags.
# The syncthing repo's key is distributed via release.syncthing.net.
RUN curl -fsSL https://syncthing.net/release-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/syncthing-archive-keyring.gpg \
  && echo "deb [signed-by=/usr/share/keyrings/syncthing-archive-keyring.gpg] https://apt.syncthing.net/ syncthing stable" \
      > /etc/apt/sources.list.d/syncthing.list \
  && apt-get update && apt-get install -y --no-install-recommends syncthing \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Startup script lives at /usr/local/bin/start-syncthing.sh and is
# launched from postStartCommand (after init-firewall and tailscale).
# Keeping it outside the repo means project YAML only needs to exist;
# no extra source checkout required.
COPY start-syncthing.sh /usr/local/bin/start-syncthing.sh
RUN chmod +x /usr/local/bin/start-syncthing.sh

# Vault + Syncthing state dirs. Both are named-volume mount targets
# declared in the project YAML, so they survive container recreation.
RUN mkdir -p /vault /var/lib/syncthing \
  && chown -R node:node /vault /var/lib/syncthing

USER node
WORKDIR /workspace
