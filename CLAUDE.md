# Drydock — Personal Agent Fabric

Drydock provisions, connects, and governs bounded work environments (**drydocks**) where Claude **workers** do work. The Harbor-side CLI (`ws`) orchestrates devcontainer-based drydocks; the `wsd` daemon mediates lifecycle operations, policy enforcement, audit, and nested spawn. See [docs/vision.md](docs/vision.md) for the fabric framing and [docs/v2-scope.md](docs/v2-scope.md) for the daemon design.

**Vocabulary** (full definitions in [docs/v2-design-vocabulary.md](docs/v2-design-vocabulary.md)):

- **Harbor** — the host machine running `wsd`. Authority lives here.
- **DryDock** — a durable, bounded work environment (the runtime unit). Persistent across container rebuilds.
- **Worker** — the agent bound to a drydock; the thing that actually does work (Claude remote-control, scheduled operator, etc.).
- **Project** — YAML template declaring what a drydock should be.

Code identifiers (`ws`, `wsd`, `ws_<slug>`, `Workspace` class, `workspaces` SQLite table) are frozen from V1 — renaming would be cosmetic churn. Product-level prose uses the above vocabulary.

**New users: start with [docs/getting-started.md](docs/getting-started.md).** This file is agent-facing; the getting-started doc walks through install, project YAML config, and a concrete walkthrough.

## Repo structure

```
base/                   # drydock-base image (Dockerfile, firewall/tailscale/remote-control scripts) + systemd units
.devcontainer/          # DryDock template (devcontainer.json, Dockerfile, project configs)
src/drydock/
  cli/                  # Click commands (see CLI reference below)
  core/                 # Registry (SQLite), workspace model, overlay, checkout, policy, secrets, schedule, audit, tailnet, trust, compliance
  output/               # JSON/human output formatting
  wsd/                  # Daemon: server, handlers, auth, config, recovery, audit/capability handlers
  templates/            # Bundled devcontainer template for `ws new`
scripts/                # bootstrap-linux-host.sh, install-linux-services.sh, drydock-resume-desks, drydock-stop-desks
tests/                  # pytest tests
docs/                   # Specs and design docs
.claude/skills/         # Claude Code skill for /ws
```

## CLI reference

Install on the Harbor (not inside a container):
```bash
pip install -e .       # or: pipx install --editable .
```

### DryDock lifecycle

| Command | Description |
|---|---|
| `ws create <project> [name]` | Provision a drydock (worktree, overlay, container). Routes through daemon when available. Resumes if already suspended. |
| `ws stop <name>` | Stop and remove the container; volumes and worktree preserved (state → suspended). |
| `ws destroy <name> --force` | Remove drydock entirely (container, worktree, overlay, tailnet device record). |
| `ws upgrade <name> [--tag TAG]` | Bump drydock-base tag in the drydock's Dockerfile and recreate. |
| `ws new <project>` | Scaffold `.devcontainer/drydock/` in a project repo from the bundled template. |

### DryDock interaction

| Command | Description |
|---|---|
| `ws list [--project P] [--state S]` | List drydocks (filterable). |
| `ws inspect <name>` | Show full drydock details. |
| `ws status <name>` | Per-drydock health: container, tailscale, firewall, remote-control, compliance. |
| `ws attach <name> [--editor code]` | Open VS Code / Cursor attached to a running drydock. |
| `ws exec <name> [-- CMD...]` | Shell into (or run a command in) a running drydock container. |

### Secrets

| Command | Description |
|---|---|
| `ws secret set <drydock> <key>` | Store a secret (value from stdin). Files land in `~/.drydock/secrets/<ws_id>/`. |
| `ws secret list <drydock>` | List secret key names (never shows values). |
| `ws secret rm <drydock> <key>` | Remove a secret. |
| `ws secret push <drydock> --to <harbor>` | Rsync drydock secrets to a remote Harbor over SSH. |

Secrets are mounted into containers at `/run/secrets/` (readonly bind-mount of `~/.drydock/secrets/<ws_id>/`).

### Daemon

| Command | Description |
|---|---|
| `ws daemon start [--foreground]` | Start the `wsd` daemon (Unix socket at `~/.drydock/wsd.sock`). |
| `ws daemon stop` | Stop the daemon (SIGTERM, then SIGKILL after timeout). |
| `ws daemon status` | Show daemon health (pid, socket, RPC responsiveness). Exit 0 if healthy, 1 otherwise. |
| `ws daemon logs [-n N] [-f]` | Show (or follow) daemon log output. |

When the daemon is running, `ws create`, `ws destroy`, and `ws upgrade` route operations through it via JSON-RPC over the Unix socket. When the daemon is unavailable, the CLI falls back to direct execution.

### Administration

| Command | Description |
|---|---|
| `ws host init` | Idempotent post-install setup: state dirs, gitconfig stub. (CLI keeps `host` for lower-level plumbing; Harbor is the product-level name for the same thing.) |
| `ws host check` | Preflight: verify docker, devcontainer CLI, tailscale, gh auth, state dirs. Exits 1 on required-check failure. |
| `ws tailnet prune [--apply]` | List (or delete) orphan drydock-style tailnet device records. Dry-run by default. |
| `ws audit [--limit N] [--event E]` | Paginated query over the audit log (daemon RPC or direct file read). |
| `ws schedule sync <drydock>` | Sync `deploy/schedule.yaml` from a drydock to Harbor-native cron/launchd. |
| `ws schedule list <drydock>` | List installed schedule entries for a drydock. |
| `ws schedule remove <drydock>` | Remove all Harbor-native schedule entries for a drydock. |

Global flags: `--json` (force JSON output), `--dry-run` (preview without executing).
Output is JSON automatically when piped or called by an agent.

## drydock-base image

`base/` builds the `ghcr.io/stevefan/drydock-base` image. Project Dockerfiles use `FROM ghcr.io/stevefan/drydock-base:<tag>`. It provides:

- **Node 20** runtime (Claude Code, devcontainer CLI)
- **Default-deny firewall** via iptables/ipset (`init-firewall.sh`, `refresh-firewall-allowlist.sh`)
- **Tailscale** for private network access (`start-tailscale.sh`)
- **Claude Code remote control** for headless agent access (`start-remote-control.sh`)
- **Claude auth sync** (`sync-claude-auth.sh`) — transplants host Claude credentials into the container
- System tools: git, curl, jq, mosh, tmux, dnsutils, iproute2
- Pre-created volume mount targets (`~/.claude`, `~/.vscode-server`, `~/.npm`, `~/.cache/pip`) with correct ownership
- `git config --system safe.directory '*'` (container is the trust boundary)

## DryDock template

The `.devcontainer/` directory is the fallback template. Projects can have their own (scaffolded via `ws new`); if they don't, this one is used. `ws create` layers an override JSON on top for per-drydock identity, secrets mount, and networking config.

## The wsd daemon

`src/drydock/wsd/` implements the daemon process running on a Harbor. It listens on a Unix socket (`~/.drydock/wsd.sock`) and exposes JSON-RPC methods for drydock lifecycle, policy enforcement, capability grants, and audit. Bearer-token auth scopes each drydock's access. The CLI is one client; workers running inside a drydock are another.

Start with `ws daemon start`, or enable the systemd unit on Linux (`scripts/install-linux-services.sh`). Check health with `ws daemon status`. Logs at `~/.drydock/wsd.log`.

Design details live in [docs/v2-scope.md](docs/v2-scope.md) and the `docs/v2-design-*.md` files.

## Environment variables

Configuration (NOT secrets) set via project YAML, overlay, or `.env.devcontainer`:

| Variable | Default | Purpose |
|---|---|---|
| `TAILSCALE_HOSTNAME` | `claude-dev` | Machine name on tailnet |
| `TAILSCALE_SERVE_PORT` | `3000` | Port served via Tailscale HTTPS |
| `REMOTE_CONTROL_NAME` | `Claude Dev` | Remote control display name |
| `FIREWALL_EXTRA_DOMAINS` | *(empty)* | Additional domains to whitelist |
| `FIREWALL_IPV6_HOSTS` | *(empty)* | IPv6 hosts to allow (`host:port`) |
| `DRYDOCK_WSD_SOCKET` | `~/.drydock/wsd.sock` | Daemon socket path |
| `DRYDOCK_WSD_REGISTRY` | `~/.drydock/registry.db` | Daemon registry DB path |
| `DRYDOCK_WSD_LOG` | `~/.drydock/wsd.log` | Daemon log file path |

**Secrets are NOT env vars.** They go through `ws secret set` → `/run/secrets/` (see below).

## Secrets

`ws secret set` is the single entry point for all credentials. Secrets are stored at `~/.drydock/secrets/<ws_id>/` (mode 0400) on the Harbor and bind-mounted at `/run/secrets/` (read-only) inside the drydock's container. Per-drydock isolation: each drydock sees ONLY its own secrets. V2.1 cross-drydock delegation: a drydock can request a lease for a secret held by a different drydock (via `RequestCapability` with `source_desk_id`); the daemon validates the entitlement and materializes the bytes into the caller's secret dir.

Auto-materialization scripts in drydock-base read from `/run/secrets/` at container start:
- `sync-claude-auth.sh` → writes `~/.claude/.credentials.json` + `~/.claude.json`
- `sync-aws-auth.sh` → writes `~/.aws/credentials` + `~/.aws/config`
- `start-tailscale.sh` → reads `tailscale_authkey` directly

| Path | Mode | Purpose |
|---|---|---|
| `~/.drydock/secrets/<ws_id>/` | 0700 | Per-drydock secrets (bind-mounted to `/run/secrets/`) |
| `~/.drydock/daemon-secrets/` | 0700 | Harbor-level admin tokens (tailscale_admin_token, tailscale_tailnet) |

Use `ws secret set/list/rm` to manage per-drydock secrets. Use `ws secret push --to <harbor>` to replicate secrets to a remote Linux Harbor.

Common secret keys:

| Key | Source | Auto-materialized by |
|---|---|---|
| `tailscale_authkey` | Tailscale admin console | `start-tailscale.sh` |
| `anthropic_api_key` | Anthropic console | (read directly from `/run/secrets/`) |
| `claude_credentials` | Mac keychain: `security find-generic-password -s "Claude Code-credentials" -w` | `sync-claude-auth.sh` |
| `claude_account_state` | `~/.claude.json` on Mac | `sync-claude-auth.sh` |
| `aws_access_key_id` | AWS IAM console | `sync-aws-auth.sh` |
| `aws_secret_access_key` | AWS IAM console | `sync-aws-auth.sh` |

## Harbor bootstrap

Fresh Linux Harbor setup is scripted at `scripts/bootstrap-linux-host.sh` (idempotent). After running it, complete the interactive auth steps (Tailscale, GitHub, Claude) and install the systemd units with `scripts/install-linux-services.sh`. See [docs/host-bootstrap.md](docs/host-bootstrap.md) for the full walkthrough.

On any Harbor, `ws host init` + `ws host check` gets drydock state dirs right and verifies prerequisites.

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
