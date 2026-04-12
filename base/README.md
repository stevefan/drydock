# drydock-base

Lean base image containing drydock's infrastructure layer. Project devcontainers inherit from this image and add their own runtimes and tooling.

## Usage

```dockerfile
FROM ghcr.io/stevefan/drydock-base:v1

# Add project-specific runtimes, tools, etc.
RUN apt-get update && apt-get install -y python3 ...
```

## What's included

- **Claude Code CLI** and **devcontainers CLI** (via npm global)
- **Tailscale** binary + sudoers for node user
- **Firewall scripts**: `init-firewall.sh`, `start-tailscale.sh`, `start-remote-control.sh` in `/usr/local/bin/`
- **NOPASSWD sudoers** for node: tailscale, tailscaled, iptables, ipset, init-firewall.sh
- **OS packages**: bash, ca-certificates, curl, git, iptables, ipset, jq, dnsutils, iproute2, aggregate
- **VOLUME** at `/home/node/.claude` for shared claude-code config

## What's NOT included

No language runtimes (Python, Rust, Go, etc.), no editor tooling, no project-specific configuration. Projects add those in their own Dockerfile.

## Rebuild and publish

```bash
# Authenticate first:
docker login ghcr.io -u stevefan -p $GITHUB_PAT

# Build multi-arch and push:
./base/build-and-push.sh
```

The script tags `v1.0.0`, `v1`, and `latest`.
