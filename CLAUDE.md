# Drydock — Agent Workspace Orchestration

Reusable devcontainer template for sandboxed Claude Code development with network firewall and Tailscale access. Designed to spawn and manage isolated agent workspaces across projects.

## Repo structure

```
.devcontainer/          # Container infra (Dockerfile, firewall, Tailscale, remote control)
docs/                   # Specs and design docs
<project>/              # Each project gets its own top-level folder
<project>/app/          # Project source code
<project>/.env.devcontainer  # Project-specific container env vars
```

## Container features

- **Default-deny firewall** via iptables/ipset — only whitelisted domains are reachable
- **Tailscale** for private network access to dev server
- **Claude Code remote control** for headless agent access
- Base whitelist: GitHub, npm, Anthropic API, VS Code marketplace, Tailscale infra

## Adding a new project

1. Create `<project>/` at repo root
2. Add `<project>/.env.devcontainer` with project-specific overrides:
   - `TAILSCALE_HOSTNAME` — Tailscale machine name (default: `claude-dev`)
   - `TAILSCALE_SERVE_PORT` — port to expose via Tailscale (default: `3000`)
   - `REMOTE_CONTROL_NAME` — name shown in Claude remote control
   - `FIREWALL_EXTRA_DOMAINS` — space-separated domains to whitelist
   - `FIREWALL_IPV6_HOSTS` — space-separated `host:port` pairs for IPv6 access
3. Rebuild the container

## Environment variables

Set on the host or in `<project>/.env.devcontainer`:

| Variable | Default | Purpose |
|---|---|---|
| `TAILSCALE_AUTHKEY` | *(empty)* | Tailscale auth key (falls back to interactive) |
| `TAILSCALE_HOSTNAME` | `claude-dev` | Machine name on tailnet |
| `TAILSCALE_SERVE_PORT` | `3000` | Port served via Tailscale HTTPS |
| `REMOTE_CONTROL_NAME` | `Claude Dev` | Remote control display name |
| `FIREWALL_EXTRA_DOMAINS` | *(empty)* | Additional domains to whitelist |
| `FIREWALL_IPV6_HOSTS` | *(empty)* | IPv6 hosts to allow (`host:port`) |

## Secrets

Secrets are loaded from `.env.local` files (gitignored). Required keys for full functionality:

| Key | Source | Purpose |
|---|---|---|
| `TAILSCALE_AUTHKEY` | Tailscale admin console | Container network access |
| `ANTHROPIC_API_KEY` | Anthropic console | Claude Code |

## Firewall

The `postStartCommand` sources all `*/.env.devcontainer` files, then runs:
1. `init-firewall.sh` — builds whitelist, sets DROP policy
2. `start-tailscale.sh` — connects to tailnet, serves dev port
3. `start-remote-control.sh` — starts Claude remote control (backgrounded)

Scripts are symlinked from `.devcontainer/` into `/usr/local/bin/` so edits take effect without rebuilding.
