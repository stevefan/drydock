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

## Tests must justify their existence

Every test you add or keep must answer yes to at least one:

1. Does it cover a real contract boundary (what callers depend on, what users see in output, what the CLI promises)?
2. Does it document a regression — a bug we hit and fixed (which we commit to not regressing)?
3. Does it verify a non-obvious invariant (merge semantics, state transitions, deterministic hashing, narrowness rules)?
4. Does it test an error path with a specific `fix:` message we've committed to as a stable contract with users?

If the answer to all four is no, the test is **vanity** and must not land.

Reject these patterns outright:

- Asserting that a default value equals the default value. Dataclasses, type annotations, and Python's assignment semantics guarantee this structurally. Writing a test for it verifies nothing except that the language works.
- Mock-verification theatrics: "I set up this mock, called my function, asserted the mock was called with exactly these args." The interesting work is in the mock setup; the assertion just checks that you typed the function call correctly. Type checkers cover this.
- Exhaustive combinatorics over orthogonal fields: if fields A, B, C don't interact, one test each plus one interaction test is enough. Not 2^3 permutations.
- Duplicates with trivial variations: three tests that all prove "the overlay contains the name field" against slightly different configs.

**Vanity tests are not free.** They impose maintenance cost on every refactor (touching an internal tends to rewrite the vanity tests that mock-check it), they create false confidence about actual coverage, and they drown out the tests that would catch real bugs if the suite grew further. The purpose of a test is to **tell us something if it breaks during refactoring or major extension** — nothing less.

Every bug we hit and fix earns a regression test. That's how we prevent re-regression. Every feature lands with the minimum tests that would break if the feature's behavior silently changed. No more.

When you're tempted to add a test to increase the green number, resist.
