# Drydock — Personal Agent Fabric

Drydock provisions, connects, and governs bounded work environments (**drydocks**) where Claude **workers** do work. The Harbor-side CLI (`ws`) orchestrates devcontainer-based drydocks; the drydock daemon mediates lifecycle operations, policy enforcement, audit, and nested spawn. See [docs/vision.md](docs/vision.md) for the fabric framing and [docs/design/](docs/design/) for feature design docs.

**Vocabulary** (full definitions in [docs/design/vocabulary.md](docs/design/vocabulary.md)):

- **Harbor** — the host machine running `drydock daemon`. Authority lives here.
- **DryDock** — a durable, bounded work environment (the runtime unit). Persistent across container rebuilds.
- **Worker** — the agent bound to a drydock; the thing that actually does work (Claude remote-control, scheduled operator, etc.).
- **Project** — YAML template declaring what a drydock should be.

Vocabulary was consolidated to one word on 2026-05-06: CLI is `drydock`, daemon is `drydock daemon` subcommand, runtime entity is `Drydock` (Python class) / `drydocks` (SQLite table) / `dock_<slug>` (ID prefix). Audit events are `drydock.*`; daemon RPC methods are `daemon.*`. Live data is migrated automatically by the Registry on first init.

**New users: start with [docs/getting-started.md](docs/getting-started.md).** This file is agent-facing; the getting-started doc walks through install, project YAML config, and a concrete walkthrough.

## Repo structure

```
base/                   # drydock-base image (Dockerfile, firewall/tailscale/remote-control scripts) + systemd units
.devcontainer/          # DryDock template (devcontainer.json, Dockerfile, project configs)
src/drydock/
  cli/                  # Click commands (see CLI reference below)
  core/                 # Registry (SQLite), workspace model, overlay, checkout, policy, secrets, schedule, audit, tailnet, trust, compliance
  output/               # JSON/human output formatting
  daemon/                  # Daemon: server, handlers, auth, config, recovery, audit/capability handlers
  templates/            # Bundled devcontainer template for `drydock new`
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
| `drydock create <project> [name]` | Provision a drydock (worktree, overlay, container). Routes through daemon when available. Resumes if already suspended. |
| `drydock stop <name>` | Stop and remove the container; volumes and worktree preserved (state → suspended). |
| `drydock destroy <name> --force` | Remove drydock entirely (container, worktree, overlay, tailnet device record). |
| `drydock upgrade <name> [--tag TAG]` | Bump drydock-base tag in the drydock's Dockerfile and recreate. |
| `drydock new <project>` | Scaffold `.devcontainer/drydock/` in a project repo from the bundled template. |

### DryDock interaction

| Command | Description |
|---|---|
| `drydock list [--project P] [--state S]` | List drydocks (filterable). |
| `drydock inspect <name>` | Show full drydock details. |
| `drydock status <name>` | Per-drydock health: container, tailscale, firewall, remote-control, compliance. |
| `drydock attach <name> [--editor code]` | Open VS Code / Cursor attached to a running drydock. |
| `drydock exec <name> [-- CMD...]` | Shell into (or run a command in) a running drydock container. |

### Secrets

| Command | Description |
|---|---|
| `drydock secret set <drydock> <key>` | Store a secret (value from stdin). Files land in `~/.drydock/secrets/<dock_id>/`. |
| `drydock secret list <drydock>` | List secret key names (never shows values). |
| `drydock secret rm <drydock> <key>` | Remove a secret. |
| `drydock secret push <drydock> --to <harbor>` | Rsync drydock secrets to a remote Harbor over SSH. |

Secrets are mounted into containers at `/run/secrets/` (readonly bind-mount of `~/.drydock/secrets/<dock_id>/`).

### Daemon

| Command | Description |
|---|---|
| `drydock daemon start [--foreground]` | Start the drydock daemon (Unix socket at `~/.drydock/run/daemon.sock`). |
| `drydock daemon stop` | Stop the daemon (SIGTERM, then SIGKILL after timeout). |
| `drydock daemon status` | Show daemon health (pid, socket, RPC responsiveness). Exit 0 if healthy, 1 otherwise. |
| `drydock daemon logs [-n N] [-f]` | Show (or follow) daemon log output. |

When the daemon is running, `drydock create`, `drydock destroy`, and `drydock upgrade` route operations through it via JSON-RPC over the Unix socket. When the daemon is unavailable, the CLI falls back to direct execution.

### Administration

| Command | Description |
|---|---|
| `drydock host init` | Idempotent post-install setup: state dirs, gitconfig stub. (CLI keeps `host` for lower-level plumbing; Harbor is the product-level name for the same thing.) |
| `drydock host check` | Preflight: verify docker, devcontainer CLI, tailscale, gh auth, state dirs. Exits 1 on required-check failure. |
| `drydock tailnet prune [--apply]` | List (or delete) orphan drydock-style tailnet device records. Dry-run by default. |
| `drydock audit [--limit N] [--event E]` | Paginated query over the audit log (daemon RPC or direct file read). |
| `drydock schedule sync <drydock>` | Sync `deploy/schedule.yaml` from a drydock to Harbor-native cron/launchd. |
| `drydock schedule list <drydock>` | List installed schedule entries for a drydock. |
| `drydock schedule remove <drydock>` | Remove all Harbor-native schedule entries for a drydock. |
| `drydock project reload <drydock>` | Re-read project YAML, update registry config + policy columns, regenerate overlay. Apply to running container with `drydock stop && drydock create`. `--no-regenerate` skips overlay rewrite. |
| `drydock overlay regenerate <drydock>` | Rewrite overlay JSON from current registry config (no YAML re-read). Narrower than `project reload` — picks up overlay-code changes without touching YAML. |
| `drydock sync <drydock>` | Fast-forward the desk's worktree from its source repo (git fetch + ff-only merge). Refuses on dirty worktree or diverged branches. Kills the "edit in place on the Harbor" antipattern. |
| `drydock deskwatch [drydock]` | Workload health evaluation: scheduled-job outcomes, output freshness, probe results. Exits 1 if any desk has violations. Declare expectations in the project YAML's `deskwatch:` block. See [docs/design/deskwatch.md](docs/design/deskwatch.md). |
| `drydock deskwatch-record <drydock> <kind> <name> <status>` | Internal helper invoked by scheduler wrappers; records a single deskwatch event. |

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
- **Default tmux config** at `/etc/tmux.conf` — mouse on, OSC 52 system-clipboard sync, vi copy keys. Backup path for `claude /login` and other interactive paste flows where host-clipboard → container is otherwise lossy.
- Pre-created volume mount targets (`~/.claude`, `~/.vscode-server`, `~/.npm`, `~/.cache/pip`) with correct ownership
- `git config --system safe.directory '*'` (container is the trust boundary)

## DryDock template

The `.devcontainer/` directory is the fallback template. Projects can have their own (scaffolded via `drydock new`); if they don't, this one is used. `drydock create` layers an override JSON on top for per-drydock identity, secrets mount, and networking config.

## The drydock daemon

`src/drydock/daemon/` implements the daemon process running on a Harbor. It listens on a Unix socket (`~/.drydock/run/daemon.sock`) and exposes JSON-RPC methods for drydock lifecycle, policy enforcement, capability grants, and audit. Bearer-token auth scopes each drydock's access. The CLI is one client; workers running inside a drydock are another.

Start with `drydock daemon start`, or enable the systemd unit on Linux (`scripts/install-linux-services.sh`). Check health with `drydock daemon status`. Logs at `~/.drydock/daemon.log`.

Design details live in [docs/design/](docs/design/): `capability-broker.md`, `narrowness.md`, `in-desk-rpc.md`, `storage-mount.md`, `persistence.md`, `tailnet-identity.md`, `vocabulary.md`, `employee-worker.md`, `egress-enforcement.md`.

## Environment variables

Configuration (NOT secrets) set via project YAML, overlay, or `.env.devcontainer`:

| Variable | Default | Purpose |
|---|---|---|
| `TAILSCALE_HOSTNAME` | `claude-dev` | Machine name on tailnet |
| `TAILSCALE_SERVE_PORT` | `3000` | Port served via Tailscale HTTPS |
| `REMOTE_CONTROL_NAME` | `Claude Dev` | Remote control display name |
| `FIREWALL_EXTRA_DOMAINS` | *(empty)* | Additional domains to whitelist |
| `FIREWALL_IPV6_HOSTS` | *(empty)* | IPv6 hosts to allow (`host:port`) |
| `FIREWALL_AWS_IP_RANGES` | *(empty)* | AWS ip-ranges.json filters (`REGION:SERVICE` space-separated, e.g. `us-west-2:AMAZON`) |
| `DRYDOCK_DAEMON_SOCKET` | `~/.drydock/run/daemon.sock` | Daemon socket path |
| `DRYDOCK_DAEMON_REGISTRY` | `~/.drydock/registry.db` | Daemon registry DB path |
| `DRYDOCK_DAEMON_LOG` | `~/.drydock/daemon.log` | Daemon log file path |

**Secrets are NOT env vars.** They go through `drydock secret set` → `/run/secrets/` (see below).

## Secrets

`drydock secret set` is the single entry point for all credentials. Secrets are stored at `~/.drydock/secrets/<dock_id>/` (mode 0400) on the Harbor and bind-mounted at `/run/secrets/` (read-only) inside the drydock's container. Per-drydock isolation: each drydock sees ONLY its own secrets. V2.1 cross-drydock delegation: a drydock can request a lease for a secret held by a different drydock (via `RequestCapability` with `source_desk_id`); the daemon validates the entitlement and materializes the bytes into the caller's secret dir.

Auto-materialization scripts in drydock-base read from `/run/secrets/` at container start:
- `sync-claude-auth.sh` → writes `~/.claude/.credentials.json` + `~/.claude.json`
- `sync-aws-auth.sh` → writes `~/.aws/credentials` + `~/.aws/config`
- `start-tailscale.sh` → reads `tailscale_authkey` directly

| Path | Mode | Purpose |
|---|---|---|
| `~/.drydock/secrets/<dock_id>/` | 0700 | Per-drydock secrets (bind-mounted to `/run/secrets/`) |
| `~/.drydock/daemon-secrets/` | 0700 | Harbor-level admin tokens (tailscale_admin_token, tailscale_tailnet) |

Use `drydock secret set/list/rm` to manage per-drydock secrets. Use `drydock secret push --to <harbor>` to replicate secrets to a remote Linux Harbor.

Common secret keys:

| Key | Source | Auto-materialized by |
|---|---|---|
| `tailscale_authkey` | Tailscale admin console | `start-tailscale.sh` |
| `anthropic_api_key` | Anthropic console | (read directly from `/run/secrets/`) |
| `claude_credentials` | Mac keychain: `security find-generic-password -s "Claude Code-credentials" -w` | `sync-claude-auth.sh` |
| `claude_account_state` | `~/.claude.json` on Mac | `sync-claude-auth.sh` |
| `aws_access_key_id` | AWS IAM console | `sync-aws-auth.sh` |
| `aws_secret_access_key` | AWS IAM console | `sync-aws-auth.sh` |
| `drydock-token` | auto-issued by `drydock daemon` on drydock create | bearer auth for in-desk `drydock-rpc` (not a user-managed secret) |

## Harbor bootstrap

Fresh Linux Harbor setup is scripted at `scripts/bootstrap-linux-host.sh` (idempotent). After running it, complete the interactive auth steps (Tailscale, GitHub, Claude) and install the systemd units with `scripts/install-linux-services.sh`. See [docs/operations/harbor-bootstrap.md](docs/operations/harbor-bootstrap.md) + [docs/operations/systemd-units.md](docs/operations/systemd-units.md) for the full walkthrough.

On any Harbor, `drydock host init` + `drydock host check` gets drydock state dirs right and verifies prerequisites.

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
