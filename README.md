# Drydock

A personal agent fabric. Each workspace is a durable, addressable place where an agent does real work — not a throwaway container.

**Status:** V1 + V1.5 shipped. Tagged `v0.1.0`.

## What it does today

- **One-command desks.** `ws create myapp` spins up a sandboxed devcontainer with its own Tailscale hostname, default-deny firewall, scoped secrets, Claude Code remote-control, and a git checkout of your project on a fresh branch.
- **Reachable from anywhere.** Desks are on your tailnet. Attach via `ws attach <name>` (opens VS Code / Cursor), SSH via `mosh node@<desk>`, or jump in from the Claude mobile app at `claude.ai/code`.
- **Fleet aware.** `ws status` shows every desk's health (tailscale joined? supervisor alive? firewall active?). Audit log at `~/.drydock/audit.log` tracks lifecycle events.
- **Isolated by default.** Each desk gets its own `/run/secrets/` directory, its own narrow firewall allowlist, its own git branch. Compromise of one desk doesn't reach the others.
- **Shared where it matters.** Claude config (auth, trust, sessions) carries across all your desks automatically. VS Code extensions, npm cache, pip cache all shared. Per-desk isolation of the rest.

## Quick start

See [**docs/getting-started.md**](docs/getting-started.md) for the full walkthrough.

Short version:

```bash
# install
pipx install --editable /path/to/drydock

# describe a project (per-host)
mkdir -p ~/.drydock/projects
cat > ~/.drydock/projects/myproject.yaml <<EOF
repo_path: /path/to/myproject
tailscale_hostname: myproject
firewall_extra_domains:
  - pypi.org
EOF

# populate secrets
mkdir -p ~/.drydock/secrets/ws_myproject
echo -n "$ANTHROPIC_API_KEY" > ~/.drydock/secrets/ws_myproject/anthropic_api_key
echo -n "$TAILSCALE_AUTHKEY"  > ~/.drydock/secrets/ws_myproject/tailscale_authkey

# spawn a desk
ws create myproject
ws attach myproject --editor cursor
```

## Commands

```
ws create <name>          Spawn a new desk
ws list                   All desks, compact
ws inspect <name>         Full state for one desk
ws status                 Fleet health (probes tailscale/supervisor/firewall)
ws attach <name>          Open editor attached to the desk
ws exec <name> [cmd...]   Shell or command inside the desk
ws stop <name>            Stop the container, preserve state
ws destroy <name> --force Remove the desk entirely (worktree, overlay, container, registry)
```

## Repo layout

```
.devcontainer/     Drydock's own dev environment
base/              Published base image (drydock-base) — Dockerfile + firewall/Tailscale/remote-control scripts
src/drydock/
  cli/             Commands: create, list, inspect, stop, destroy, attach, exec, status
  core/            Registry (SQLite), workspace model, checkout (git clone), overlay, devcontainer wrapper, audit, project config
  output/          JSON and human-readable formatting
docs/              Canonical design docs (vision, getting-started, v2-scope, secrets-design)
  _archive/        Pre-fabric-framing specs (historical, superseded)
tests/             pytest suite (125 tests)
Makefile           test / lint / install / base-publish / rebuild / clean-registry
```

## Where it's going

- **V2 (designed in [docs/v2-scope.md](docs/v2-scope.md)):** daemon-mediated control plane. Enables agent-spawns-agent with enforced narrowness (a child desk's policy cannot exceed its parent's). Opens nested orchestration; audit becomes first-class.
- **V3:** fleet-awareness. Desks live on any host in your tailnet (laptop, home server, cloud VM); `ws` from laptop orchestrates them across hosts. Laptop becomes viewport; always-on host runs desks continuously.
- **V4+:** cloud fabric. Remote filesystem mounts, capability broker (secrets broker generalized), projects that primarily live in cloud because that's where the data lives.

See [docs/vision.md](docs/vision.md) for the longer arc.

## Further reading

- [docs/vision.md](docs/vision.md) — what Drydock is becoming (personal agent fabric framing)
- [docs/getting-started.md](docs/getting-started.md) — user-facing walkthrough
- [docs/v2-scope.md](docs/v2-scope.md) — daemon design and V2/V3 roadmap
- [docs/secrets-design.md](docs/secrets-design.md) — secrets convention and v2 broker direction
