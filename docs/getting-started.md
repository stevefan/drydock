# Getting Started with Drydock

Drydock is a host-side CLI (`ws`) that provisions sandboxed Claude Code drydocks as devcontainers. Each drydock gets its own firewall, Tailscale hostname, secrets mount, and git worktree.

A *drydock* is a durable addressable place where a Worker (an agent) does work — not a throwaway container. It has a stable name, a scoped policy, a git worktree, and (once the container is up) a Claude Code session you can attach to from anywhere on your tailnet. The container can come and go; the drydock persists.

Your laptop or server running `wsd` is the **Harbor**; each drydock you create lives on that Harbor. See [design/vocabulary.md](design/vocabulary.md) for the full vocabulary.

This guide walks you from zero to a running drydock. If you're using Drydock for a monorepo with heterogeneous sub-projects, the per-project YAML section is the part that matters most.

## Prerequisites

On the Harbor (your laptop — not inside a devcontainer):

| Tool | How to install | Why |
|---|---|---|
| Python 3.11+ | `brew install python@3.12` | Runs the `ws` CLI |
| Docker Desktop (macOS) or Docker Engine (Linux) | https://www.docker.com/products/docker-desktop/ | Runs drydock containers |
| devcontainer CLI | `npm install -g @devcontainers/cli` | `ws` invokes this under the hood |
| Git | usually preinstalled | Worktrees for drydock branches |

You'll also want:

| Credential | Where to get it | Used for |
|---|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ | Claude Code inside the drydock |
| `TAILSCALE_AUTHKEY` | https://login.tailscale.com/admin/settings/keys | Auto-joining the tailnet |

**How secrets reach the drydock.** Use `ws secret` to manage per-drydock secrets. They're stored at `~/.drydock/secrets/<workspace_id>/` on the Harbor and mounted at `/run/secrets/` (readonly) inside the container.

```bash
ws secret set myproject anthropic_api_key    # prompts for value (stdin)
ws secret set myproject tailscale_authkey
ws secret list myproject                     # show what's set
ws secret push myproject --to root@host      # sync to a remote Harbor
```

The drydock id is `ws_<name_slug>` — deterministic from the `ws create` args (dashes and spaces in the name become underscores). Secret names are validated (alphanumeric + underscores only). See [operations/secrets.md](operations/secrets.md) for the operator's guide and [design/capability-broker.md](design/capability-broker.md) for the lease model.

## Install `ws`

Install via `pipx` (recommended — gives you a global `ws` command without polluting your system Python):

```bash
pipx install --editable /path/to/drydock
```

Or for development (running tests, modifying the CLI):

```bash
git clone https://github.com/<your-org>/drydock.git
cd drydock
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Confirm:

```bash
ws --help          # or .venv/bin/ws --help if using venv
ws version         # should print the current version
```

## Your first drydock

Drydock works on two things: a **project** (a source repo + YAML description) and a **drydock** (a container running a branch of that project).

Minimal path — no per-project config:

```bash
ws create myproject --repo-path /path/to/myproject
```

This:

1. Creates a new branch `ws/myproject` off the project's HEAD
2. Clones the project into `~/.drydock/worktrees/ws_myproject/` as a standalone git repo with its own `.git` directory (uses `git clone --reference --dissociate` for disk efficiency; fully self-contained at runtime)
3. Writes a composite devcontainer.json at `~/.drydock/overlays/ws_myproject.devcontainer.json` by merging the project's own `.devcontainer/devcontainer.json` with drydock's per-drydock overlay (identity env vars, scoped secrets mount, firewall extras, Tailscale hostname, shared volumes)
4. Runs `devcontainer up` with the composite to launch the container
5. Records everything in `~/.drydock/registry.db` and logs the event to `~/.drydock/audit.log`

Interact with it:

```bash
ws list                   # all drydocks
ws inspect myproject      # full state for one
ws status                 # Harbor health: tailscale, supervisor, firewall, per-drydock
ws attach myproject --editor cursor   # open in editor
ws exec myproject         # shell inside the drydock
ws exec myproject pytest  # run a command inside
```

Stop it (container down; volume state preserved so next `ws create` reuses session history):

```bash
ws stop myproject
```

Destroy it (container removed, checkout `rm -rf`'d, overlay deleted, registry row gone; Tailscale node logged out before stop so your tailnet admin stays clean):

```bash
ws --dry-run destroy myproject   # preview first
ws destroy myproject --force
```

## Per-project configuration

For anything beyond the simplest case, create `~/.drydock/projects/<project>.yaml`. `ws create <project>` reads this config and uses its values as defaults. CLI flags still win.

Example — `~/.drydock/projects/myproject.yaml`:

```yaml
# Where the project's git repo lives on this Harbor
repo_path: /Users/you/code/myproject

# Container image override (defaults to Drydock's template if absent)
image: ghcr.io/example/myproject-dev:latest

# Tailscale identity
tailscale_hostname: myproject-dev
tailscale_serve_port: 3000
tailscale_authkey_env_var: TAILSCALE_AUTHKEY  # which env var holds the key

# Claude Code remote control display name
remote_control_name: myproject

# Firewall — domains the drydock can reach beyond the default whitelist
firewall_extra_domains:
  - api.stripe.com
  - myproject.example.com

# Optional IPv6 hosts (host:port format)
firewall_ipv6_hosts: []

# Ports to forward from the drydock to your Harbor (optional)
forward_ports: [3000, 8080]

# Sub-directory of the repo to treat as the drydock root (optional;
# useful for monorepos where individual sub-projects have their own
# .devcontainer/). The git checkout still includes the full repo.
workspace_subdir: services/api

# Per-project Claude config isolation (optional). Default unset = all
# drydocks share one claude-code-config volume (auth/history propagate).
# Set to isolate: each profile gets its own volume.
claude_profile: staging

# Harbor bind-mounts to inject into the drydock (optional). Useful for
# Notebook vaults, shared data dirs, host-side tooling.
extra_mounts:
  - "source=/Users/you/Notebooks/mylab,target=/workspace/vault,type=bind,readonly"
```

Unknown keys are rejected (so typos don't become silent no-ops). Missing file is fine — the CLI falls back to defaults.

## A worked example: heterogeneous monorepo

A common case is a monorepo whose sub-projects have different isolation needs — e.g. a core library with no network, a web app, and a scraper that pulls untrusted HTML from arbitrary hosts. In v1 each sub-project maps to a separate drydock.

Step 1 — top-level project drydock:

```yaml
# ~/.drydock/projects/myapp.yaml
repo_path: /path/to/myapp
tailscale_hostname: myapp-dev
remote_control_name: myapp
firewall_extra_domains:
  - github.com
  - pypi.org
  - crates.io
```

```bash
ws create myapp
```

Step 2 — narrow-scope drydock for the high-risk sub-project:

```yaml
# ~/.drydock/projects/myapp-scraper.yaml
repo_path: /path/to/myapp/scraper
tailscale_hostname: myapp-scraper
remote_control_name: myapp-scraper
firewall_extra_domains:
  - example.com
  - api.example.org
  # ...only the hosts this sub-project legitimately needs
```

```bash
ws create myapp-scraper
```

The two drydocks are fully independent: different firewalls, different tailnet hostnames, different worktrees, different containers. A bug in the scraper's automation can't reach anything beyond the hosts listed above.

## Schedules

Projects can declare scheduled jobs in `deploy/schedule.yaml`. The `ws schedule sync` command materializes these into the Harbor's native cron system:

```bash
ws schedule sync myproject        # install/update cron entries from schedule.yaml
ws schedule list myproject        # show current schedule state
```

See the auction-crawl project for a working example with three scheduled jobs.

## The v2 daemon

The `wsd` daemon mediates every lifecycle operation, enforces narrowness, brokers capability leases, and audits. Workers inside drydocks reach it via a bind-mounted socket + the embedded `drydock-rpc` client. See [design/in-desk-rpc.md](design/in-desk-rpc.md) and [design/capability-broker.md](design/capability-broker.md).

### What is *not* yet shipped

- **Parent-child destroy cascade.** Each drydock is independent; destroying a parent does not destroy its conceptual children.

### Not a goal

- **Cross-Harbor migration.** Drydocks are pinned to the Harbor that creates them; hardware refresh is a rebuild-from-config procedure (yaml + registry dump + worktree branches). See `_archive/migration-vision.md` for the archived exploration.

## Troubleshooting

**`ws create` fails with `devcontainer CLI not found`:**
Install it: `npm install -g @devcontainers/cli`. The `ws` CLI shells out to `devcontainer up`.

**`ws create` fails with `Workspace 'X' already exists (state: defined)`:**
You have a stale registry entry from a prior failed run. Destroy it: `ws destroy X --force`.

**`devcontainer up` succeeds but Tailscale never joins:**
Check that `TAILSCALE_AUTHKEY` is set in your Harbor environment or in `<project>/.env.devcontainer`. The drydock sources these at start.

**The drydock's firewall is blocking something legitimate:**
Add the domain to `firewall_extra_domains` in the project YAML, destroy, and recreate. The firewall is rebuilt from scratch at container start.

**Python says `ws not found`:**
If installed via `pipx`, check `pipx list`. If using a venv, ensure `.venv/bin/` is on your PATH.

**Tests aren't passing:**
Reinstall: `pip install -e ".[dev]" --force-reinstall`. The editable install caches metadata occasionally.

## Where to go next

- [CLAUDE.md](../CLAUDE.md) — agent-facing conventions for Drydock development
- [vision.md](vision.md) — the fabric framing and long-form design rationale
- [design/vocabulary.md](design/vocabulary.md) — Harbor / DryDock / Worker vocabulary (canonical reference)
- [design/capability-broker.md](design/capability-broker.md) — lease model, SECRET and STORAGE_MOUNT, cross-drydock delegation
- [design/in-desk-rpc.md](design/in-desk-rpc.md) — how workers reach the daemon
- [design/persistence.md](design/persistence.md) — state ownership, reboot recovery, audit event schema
- [operations/harbor-bootstrap.md](operations/harbor-bootstrap.md) — standing up a fresh Linux Harbor
- [operations/systemd-units.md](operations/systemd-units.md) — `drydock-wsd` + `drydock-desks` service lifecycle
- [operations/secrets.md](operations/secrets.md) — operator's guide to secret files and refresh flows
