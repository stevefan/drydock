FROM ghcr.io/stevefan/drydock-base:v1

ARG TZ
ENV TZ="$TZ"

USER root

# Python + minimal CLI tooling beyond what drydock-base provides.
# python3 is already in the base; we add pip + venv so the bot's
# deps land in a project-owned venv without touching system python.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3-pip python3-venv \
      less procps fzf zsh nano vim \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Bot + helper CLIs go to /usr/local/bin so they're on PATH for both
# the postStartCommand-launched daemon and any in-desk Claude session.
COPY start-telegram-bot.sh bot.py tg-recent tg-send tg-mode /usr/local/bin/
RUN chmod +x /usr/local/bin/start-telegram-bot.sh \
             /usr/local/bin/bot.py \
             /usr/local/bin/tg-recent \
             /usr/local/bin/tg-send \
             /usr/local/bin/tg-mode

# Bot venv with python-telegram-bot. Building it at image time keeps
# postCreate fast and ensures the dependency is reproducible.
RUN python3 -m venv /opt/telegram-bot-venv && \
    /opt/telegram-bot-venv/bin/pip install --no-cache-dir \
      "python-telegram-bot[ext]>=21" "httpx>=0.27"

# Bot state lives at /workspace/telegram (named-volume-backed via
# project YAML so messages survive `ws stop && ws create`).
RUN mkdir -p /workspace/telegram && chown -R node:node /workspace/telegram

USER node
WORKDIR /workspace
