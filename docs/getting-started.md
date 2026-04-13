# Getting Started with Drydock

Drydock is a host-side CLI (`ws`) that provisions sandboxed Claude Code workspaces as devcontainers. Each workspace gets its own firewall, Tailscale hostname, secrets mount, and git worktree.

A *workspace* is a durable addressable place where an agent works — not a throwaway container. It has a stable name, a scoped policy, a git worktree, and (once the container is up) a Claude Code session you can attach to from anywhere on your tailnet. The container can come and go; the workspace persists.

This guide walks you from zero to a running workspace. If you're trying to use Drydock for Microfoundry (or any other monorepo with heterogeneous sub-projects), the per-project YAML section is the part that matters most.

## Prerequisites

On the host (your laptop — not inside a devcontainer):

| Tool | How to install | Why |
|---|---|---|
| Python 3.11+ | `brew install python@3.12` | Runs the `ws` CLI |
| Docker Desktop (macOS) or Docker Engine (Linux) | https://www.docker.com/products/docker-desktop/ | Runs workspace containers |
| devcontainer CLI | `npm install -g @devcontainers/cli` | `ws` invokes this under the hood |
| Git | usually preinstalled | Worktrees for workspace branches |

You'll also want:

| Credential | Where to get it | Used for |
|---|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ | Claude Code inside the workspace |
| `TAILSCALE_AUTHKEY` | https://login.tailscale.com/admin/settings/keys | Auto-joining the tailnet |

**How secrets reach the workspace.** The workspace mounts `~/.drydock/secrets/<workspace_id>/` from your host at `/run/secrets/` (readonly). Before `ws create`, populate that directory with plain files named after each key:

```bash
mkdir -p ~/.drydock/secrets/ws_myproject
chmod 700 ~/.drydock/secrets/ws_myproject
echo -n "$ANTHROPIC_API_KEY" > ~/.drydock/secrets/ws_myproject/anthropic_api_key
echo -n "$TAILSCALE_AUTHKEY"  > ~/.drydock/secrets/ws_myproject/tailscale_authkey
chmod 400 ~/.drydock/secrets/ws_myproject/*
```

The workspace id is `ws_<name_slug>` — deterministic from the `ws create` args (dashes and spaces in the name become underscores). If the directory is missing, Docker auto-creates an empty one and the workspace starts with an empty `/run/secrets/`; scripts that need secrets will see empty strings. See [secrets-design.md](secrets-design.md) for the full convention and the v2 broker direction.

## Install `ws`

Clone Drydock and install in a venv (development-era recommendation; switch to `pipx` once the CLI stabilizes):

```bash
git clone https://github.com/<your-org>/drydock.git
cd drydock
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Confirm:

```bash
.venv/bin/ws --help
.venv/bin/pytest  # should pass cleanly
```

For convenience, add `.venv/bin` to your PATH, or create an alias:

```bash
alias ws="$HOME/path/to/drydock/.venv/bin/ws"
```

## Your first workspace

Drydock works on two things: a **project** (a source repo) and a **workspace** (a container running a branch of that project).

Minimal path — no per-project config:

```bash
ws create myproject --repo-path /path/to/myproject
```

This:

1. Creates a new branch `ws/myproject` off the project's HEAD
2. Materializes it as a git worktree at `~/.drydock/worktrees/ws_myproject/`
3. Writes a devcontainer override JSON at `~/.drydock/overlays/ws_myproject.devcontainer.override.json`
4. Runs `devcontainer up` with the override to launch the container
5. Records everything in `~/.drydock/registry.db`

Check it:

```bash
ws list
ws inspect myproject
```

Stop it (container down; registry remembers it):

```bash
ws stop myproject
```

Destroy it (container down, worktree removed, overlay removed, registry row deleted):

```bash
ws --dry-run destroy myproject   # preview first
ws destroy myproject --force
```

## Per-project configuration

For anything beyond the simplest case, create `drydock/projects/<project>.yaml` in the Drydock repo. `ws create <project>` reads this config and uses its values as defaults. CLI flags still win.

Example — `drydock/projects/myproject.yaml`:

```yaml
# Where the project's git repo lives on this host
repo_path: /Users/you/code/myproject

# Container image override (defaults to Drydock's template if absent)
image: ghcr.io/example/myproject-dev:latest

# Tailscale identity
tailscale_hostname: myproject-dev
tailscale_serve_port: 3000
tailscale_authkey_env_var: TAILSCALE_AUTHKEY  # which env var holds the key

# Claude Code remote control display name
remote_control_name: myproject

# Firewall — domains the workspace can reach beyond the default whitelist
firewall_extra_domains:
  - api.stripe.com
  - myproject.example.com

# Optional IPv6 hosts (host:port format)
firewall_ipv6_hosts: []
```

Unknown keys are rejected (so typos don't become silent no-ops). Missing file is fine — the CLI falls back to defaults.

## A real example: Microfoundry

Microfoundry is a monorepo with several sub-projects that have very different isolation needs. The current (v1) approach is to treat each sub-project as a separate Drydock workspace.

Step 1 — top-level microfoundry workspace:

```yaml
# drydock/projects/microfoundry.yaml
repo_path: /Users/you/Unified Workspaces/microfoundry
tailscale_hostname: microfoundry-dev
remote_control_name: microfoundry
firewall_extra_domains:
  - github.com
  - pypi.org
  - crates.io
```

```bash
ws create microfoundry
```

Step 2 — narrow-scope workspace for `auction-crawl`:

```yaml
# drydock/projects/auction-crawl.yaml
repo_path: /Users/you/Unified Workspaces/microfoundry/auction-crawl
tailscale_hostname: auction-crawl
remote_control_name: auction-crawl
firewall_extra_domains:
  - ebay.com
  - govdeals.com
  - publicsurplus.com
  # ...only the hosts auction-crawl legitimately needs
```

```bash
ws create auction-crawl
```

The two workspaces are fully independent: different firewalls, different tailnet hostnames, different worktrees, different containers. A bug in auction-crawl's Playwright scripts can't reach anything beyond the auction hosts listed above.

### What v1 does *not* do for microfoundry (yet)

- **Nested spawning.** A Claude inside the microfoundry workspace cannot currently call `ws create auction-crawl` to spawn a child workspace. V1's `ws` is a host-side CLI. This is a known gap, captured in [v2-scope.md](v2-scope.md).
- **Parent-child destroy cascade.** Each workspace is independent; destroying microfoundry does not destroy auction-crawl.
- **`ws attach`.** Attaching to a workspace from your phone or another machine is planned but not yet implemented; for now, use the workspace's Tailscale hostname and the Claude Code remote control or SSH.

For the nested-spawning design and the microfoundry requirement that drives it, see [v2-scope.md](v2-scope.md).

## Troubleshooting

**`ws create` fails with `devcontainer CLI not found`:**
Install it: `npm install -g @devcontainers/cli`. The `ws` CLI shells out to `devcontainer up`.

**`ws create` fails with `Workspace 'X' already exists (state: defined)`:**
You have a stale registry entry from a prior failed run. Destroy it: `ws destroy X --force`.

**`devcontainer up` succeeds but Tailscale never joins:**
Check that `TAILSCALE_AUTHKEY` is set in your host environment or in `<project>/.env.devcontainer`. The workspace sources these at start.

**The workspace's firewall is blocking something legitimate:**
Add the domain to `firewall_extra_domains` in the project YAML, destroy, and recreate. The firewall is rebuilt from scratch at container start.

**Python says `ws not found`:**
You're probably shelling from outside the venv. Use `.venv/bin/ws` or add the venv's `bin/` to your PATH.

**Tests aren't passing:**
Reinstall: `.venv/bin/pip install -e ".[dev]" --force-reinstall`. The editable install caches metadata occasionally.

## Where to go next

- [CLAUDE.md](../CLAUDE.md) — agent-facing conventions for Drydock development
- [vision.md](vision.md) — the fabric framing and long-form design rationale
- [v2-scope.md](v2-scope.md) — the `ws` daemon for nested orchestration (upcoming)
- [secrets-design.md](secrets-design.md) — secrets convention and the v2 broker direction
