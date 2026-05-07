# drydock-port-auditor: role-locked image for the Port Auditor's drydock.
#
# Distinct from drydock-base. The Auditor watches drydocks; it does
# not run the workloads drydocks run. So this image is smaller and
# narrower:
#
#   - No Node, no Claude Code CLI, no devcontainer CLI (Auditor calls
#     the Anthropic API directly via the Python SDK; no IDE attach)
#   - No Tailscale (Auditor reaches OUT to API endpoints; doesn't
#     listen on the tailnet)
#   - No AWS CLI (Auditor has no AWS reach by validator constraint)
#   - No mosh/tmux (no interactive sessions; observation-only)
#   - No s3fs / FUSE (no storage_mounts allowed)
#   - No VS Code server (can't `drydock attach` an Auditor)
#
# What's in:
#
#   - Python 3.12 + drydock package (so `drydock auditor watch-loop`
#     is the entrypoint)
#   - iptables / ipset / aggregate + the shared init-firewall.sh +
#     refresh-firewall-allowlist.sh — narrow egress is structural,
#     not advisory; the validator requires the YAML declare it, this
#     image enforces it inside the container
#   - jq, ripgrep, curl — curated tooling for the Auditor's own
#     ad-hoc analysis (audit-log scans, JSON queries). Tools are
#     read-only by nature; no shell utilities for state mutation
#     beyond the standard /bin/sh
#
# Image is approved by core/auditor/role_validator.py
# (_APPROVED_IMAGE_PREFIXES). Loosening that list requires editing
# the validator — a deliberate principal-side action.
#
# Build with: ./base/port-auditor/build-and-push.sh <version>
# (build context is repo root; Dockerfile path is explicit)

FROM python:3.12-slim

ARG TARGETARCH

# Minimal system layer — narrow firewall stack + curated read-only
# analysis tools. apt-clean afterwards keeps the image tight.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      dnsutils \
      iproute2 \
      iptables \
      ipset \
      aggregate \
      jq \
      ripgrep \
      sqlite3 \
      tini \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Shared firewall scripts (same source-of-truth as drydock-base, so
# enforcement semantics don't drift between worker and auditor desks).
# Build context is repo root; paths are relative to that.
COPY base/init-firewall.sh \
     base/refresh-firewall-allowlist.sh \
     base/add-allowed-domain.sh \
     /usr/local/bin/
RUN chmod +x /usr/local/bin/init-firewall.sh \
              /usr/local/bin/refresh-firewall-allowlist.sh \
              /usr/local/bin/add-allowed-domain.sh

# drydock package itself — pinned to the build moment. The auditor
# CLI + core/auditor/* live inside this install, so `drydock auditor
# watch-loop` resolves to a versioned snapshot, not whatever the
# Harbor happens to have.
COPY pyproject.toml README.md /opt/drydock-src/
COPY src/ /opt/drydock-src/src/
RUN pip install --no-cache-dir /opt/drydock-src

# Auditor-specific entrypoint — runs init-firewall.sh, then exec's
# the watch loop.
COPY base/port-auditor/start-auditor.sh /usr/local/bin/start-auditor.sh
RUN chmod +x /usr/local/bin/start-auditor.sh

# Pre-create the directories the watch loop writes to so the named
# volumes inherit correct ownership when first mounted. The Auditor
# runs as root inside its container (the kernel container boundary
# is the trust line; user-inside-container doesn't add isolation).
RUN mkdir -p /root/.drydock/auditor /root/.drydock/run

# tini as PID 1 so SIGTERM from `drydock stop` propagates to the
# watch loop, the loop unwinds cleanly (heartbeat write,
# verdict-buffer flush), and we don't leak zombie children if the
# loop spawns subprocesses for ad-hoc analysis.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-auditor.sh"]
