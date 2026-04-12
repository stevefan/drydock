# Drydock — Personal Agent Fabric

Drydock provisions, connects, and governs the sandboxed workspaces where Claude agents live and do work. V1 ships as a host-side CLI (`ws`) over devcontainer primitives; the long-term shape is a daemon-mediated control plane with policy graph, audit, secrets brokering, and cross-host placement. See [docs/vision.md](docs/vision.md) for the fabric framing and [docs/v2-scope.md](docs/v2-scope.md) for the daemon design.

The v1 CLI runs on the host; containers are workspaces, not orchestrators. Nested spawning (a workspace calling `ws create`) is a v2 feature.

**New users: start with [docs/getting-started.md](docs/getting-started.md).** This file is agent-facing; the getting-started doc walks through install, project YAML config, and a concrete microfoundry example.

## Repo structure

```
.devcontainer/          # Workspace template (Dockerfile, firewall, Tailscale, remote control)
src/drydock/            # ws CLI source (Python)
  cli/                  # Click commands (create, list, inspect, stop, destroy)
  core/                 # Registry (SQLite), workspace model, devcontainer wrapper, errors
  output/               # JSON/human output formatting
tests/                  # pytest tests
docs/                   # Specs and design docs
.claude/skills/ws.md    # Claude Code skill for /ws
```

## Container features

- **Default-deny firewall** via iptables/ipset — only whitelisted domains are reachable
- **Tailscale** for private network access to dev server
- **Claude Code remote control** for headless agent access
- Base whitelist: GitHub, npm, Anthropic API, VS Code marketplace, Tailscale infra

## Using the ws CLI

Install on the host (not inside a container):
```bash
pip install -e .
```

Commands:
```
ws create <project> [name]    Provision a workspace container
ws list                       List workspaces
ws inspect <name>             Show workspace details
ws stop <name>                Stop a workspace
ws destroy <name> --force     Remove a workspace
```

Global flags: `--json` (force JSON output), `--dry-run` (preview without executing).
Output is JSON automatically when piped or called by an agent.

## Workspace template

The `.devcontainer/` directory is the base workspace template. Projects can have their own devcontainer; if they don't, this one is used. `ws create` layers an override JSON on top for per-workspace identity, secrets, and networking.

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
