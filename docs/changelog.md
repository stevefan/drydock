# Changelog

> **Vocabulary note (2026-04-17):** Entries below predate the Harbor / DryDock / Worker product-vocabulary shift and are preserved verbatim as history. In current vocabulary: "host" (the machine running `wsd`) is a **Harbor**, "agent-desk" / "workspace" (product concept) is a **DryDock**, and the agent running inside is a **Worker**. Code identifiers (`ws_<slug>`, `workspaces` table, `Workspace` class, CLI `ws` prefix) are unchanged. See [v2-design-vocabulary.md](v2-design-vocabulary.md).

## v1.0.0 — 2026-04-18

V2 architecturally complete + V4 Phase 1 (STORAGE_MOUNT) live end-to-end.

**What shipped since v1.0.0-rc1 (the "RPC surface feature-complete" marker):**

- **Persistence pivot.** V3 cross-host migration archived (`docs/_archive/migration-vision.md`); drydocks are durable on a chosen Harbor. Reboot-resume via systemd units (`drydock-wsd.service` + `drydock-desks.service` with ExecStop hook). Resume-on-CreateDesk for suspended drydocks + ungraceful-shutdown detection.
- **In-desk RPC.** `wsd` socket bind-mounted into every drydock at `/run/drydock/wsd.sock` (directory bind — durable across daemon restart). Stdlib-only client `drydock-rpc` bind-mounted at `/usr/local/bin/drydock-rpc`. Socket chmod 0o666 so non-root workers can connect; bearer-token auth remains the real security gate.
- **V4 Phase 1: STORAGE_MOUNT.** New `CapabilityKind.REQUEST_STORAGE_LEASES`, `[storage]` wsd.toml section, `StsAssumeRoleBackend` + `StubStorageBackend`. Workers get scoped AWS STS creds via `RequestCapability(type=STORAGE_MOUNT, scope={bucket, prefix, mode})`; daemon materializes `aws_*` files into the desk's secret dir with container-uid chown.
- **V4 Phase 1b narrowness.** `delegatable_storage_scopes` YAML field + registry column + validator. Scope format `"s3://bucket/prefix/*"` with optional `rw:` prefix. Default-permissive-when-empty for back-compat.
- **V2.1 cross-drydock secret delegation.** `source_desk_id` on `RequestCapability` lets a drydock receive a secret held by another; daemon-mediated file copy into caller's secret dir.
- **Resume regenerates overlay.** Overlay-code changes land on `ws create <suspended-name>` without `--force`-destroying the worktree.
- **Product vocabulary refactor.** Harbor / DryDock / Worker as the product three-layer model; code identifiers unchanged. See `docs/v2-design-vocabulary.md`.
- **project_config accepts V2 fields.** `capabilities`, `secret_entitlements`, `delegatable_secrets`, `delegatable_firewall_domains`, `delegatable_storage_scopes` all supported in YAML + forwarded via CLI→daemon.
- **ws daemon status under systemd.** Health-derives-from-socket, not pid-file.
- **Container-uid chown on materialized secrets.** Fixes the class of "daemon writes as root, node worker can't read" bugs for both cross-desk SECRET and STORAGE_MOUNT.
- **Validated end-to-end on `drydock-hillsboro`:** auction-crawl worker requests STORAGE_MOUNT from inside its container, writes to S3 via scoped creds, narrowness denies out-of-prefix writes. Infra bootstrapped as fleet-agent (Harbor `drydock-runner` → `drydock-agent` role assume-role).

**Known follow-ups (tracked, not blockers):**
- awscli not in `drydock-base` — fleet-agent drydocks reinstall on each container recreate
- S3 virtual-host per-bucket addressing vs firewall ipset — new buckets miss the allowlist until DNS refresh
- `firewall_extra_domains` YAML drift not reconciled on resume (needs `ws project reload <name>`)
- `ws overlay regenerate` as an explicit CLI command (today implicit via resume)
- Tighten `delegatable_storage_scopes` default-permissive-when-empty per-project when ready

**Commits since rc1:** 30+. Full list: `git log v1.0.0-rc1..v1.0.0`.

---

## v0.1.0 — 2026-04-13

First tagged release. V1 + V1.5 shipped. Covers the full "spawn sandboxed agent-desks and use them from anywhere" use case for a single user on a single host.

### What shipped

**Lifecycle**
- `ws create / list / inspect / stop / destroy / attach / exec / status`
- SQLite registry at `~/.drydock/registry.db`
- Git checkouts via standalone `git clone --reference --dissociate` (self-contained `.git` directory inside the desk; origin URL rewritten to the project's own origin so pushes go to GitHub)
- Composite devcontainer.json written at `~/.drydock/overlays/` by merging the project's own `.devcontainer/devcontainer.json` with drydock's per-workspace overlay
- Tailscale logout before container stop so the tailnet admin stays clean (no orphan nodes)
- Workspace id: `ws_<name_slug>`. Name is unique in the registry; project is metadata. Dashes in names become underscores.

**Per-workspace infrastructure**
- Default-deny egress firewall with per-project allowlist (`firewall_extra_domains`, `firewall_ipv6_hosts`)
- Tailscale with per-workspace hostname, auth via `/run/secrets/tailscale_authkey`, Tailscale SSH enabled at `up`
- Claude Code remote-control supervisor running at `claude remote-control --permission-mode bypassPermissions` (outer sandbox is the boundary; inside-desk prompts are friction)
- Per-workspace secrets at `~/.drydock/secrets/<ws_id>/` mounted readonly at `/run/secrets/` in the container
- `workspace_subdir` support for sub-project desks in monorepos
- `forward_ports`, `extra_mounts`, `claude_profile` YAML fields

**Shared infrastructure**
- `claude-code-config` volume carries auth, workspace-trust, session history, and `.claude.json` across all desks on a host. One `/login` and one workspace-trust acceptance work for every future desk.
- `drydock-vscode-server`, `drydock-npm-cache`, `drydock-pip-cache` volumes share editor extensions and package caches across desks
- Host `~/.gitconfig` bind-mounted readonly for consistent git identity

**Base image**
- `ghcr.io/stevefan/drydock-base:v1` published multi-arch (amd64 + arm64)
- Contains: claude, devcontainer CLI, tailscale, firewall scripts, sudoers for node user, base apt packages, mosh, Tailscale SSH
- Projects extend via `FROM ghcr.io/stevefan/drydock-base:v1` — infrastructure layers come free, projects add their own language toolchain and app deps
- Released as v1.0.x with semantic bumps per change

**Integration**
- `ws attach <name> [--editor <binary>]` opens Cursor / VS Code / code-insiders attached to the container at the right workspace folder via vscode-remote URI
- Claude mobile app and claude.ai/code reach desks over Tailscale via the remote-control supervisor
- Audit log at `~/.drydock/audit.log` (JSONL, append-only): workspace.created, workspace.running, workspace.error, workspace.stopped, workspace.destroyed

### What got designed but not built (roadmap)

- **V2 daemon** — `docs/v2-scope.md`. Nested orchestration (agent-in-desk spawns child-desk), policy graph with enforced narrowness, bearer-token auth over Tailscale, audit as first-class. Waits for real nested-spawn pressure.
- **V4+ cloud fabric** — remote filesystem mounts (S3/R2/GCS), capability broker generalizing the secrets broker, cloud-primary projects. Directional sketch in memory.

### Archived (2026-04-17)

- **V3 fleet-awareness and cross-host migration.** `ws migrate laptop→cloud`, fleet-aware daemon, identity continuity across hosts, suspend/resume as a first-class primitive. Pivoted to "always-on durability on a chosen host" — hardware refresh becomes a bounded rebuild-from-config runbook, not a daemon feature. V2's serializability-for-migration commitment dropped alongside. Full vision and reversal criteria preserved in `docs/_archive/migration-vision.md`.

### Tests

125 tests at tag. Audit pass reduced from 163 by removing vanity tests (default-value assertions, mock-verification theatrics, exhaustive-orthogonal combinatorics). Retained: regression tests (the bugs we hit: asymmetric slug, label-filter path for workspace_subdir, cache shadowing, overlay-replace-vs-merge, git worktree in container, Tailscale cascade failure, IFS strict-split bug), contract tests (error shapes with `fix:` fields), merge/dedup logic, state transitions, integration tests YAML → overlay → composite.

### Retrospective

**What the pre-fabric spec (`docs/_archive/workspace-orchestration-spec-v1.md`) anticipated:** a nine-state workspace lifecycle, fork and attach semantics, tmux and session management, mobile dashboard, permission profiles as a first-class concept, transcript and summary persistence. Most of that was aspirational — code never produced most of those states, fork was never built, session tracking is deferred, mobile is the Claude app (not a custom dashboard).

**What actually earned its keep in v1:** five states (`defined`, `provisioning`, `running`, `suspended`, `error`), the devcontainer overlay + standalone clone checkout model, per-workspace firewall + Tailscale + secrets, shared `claude-code-config` for auth propagation, `drydock-base` as the extension point for projects.

**Unanticipated wins:** the shared-volume pattern for claude auth + workspace-trust ended up being the single biggest UX lift. Publishing `drydock-base` as a GHCR image collapsed ~300 lines of per-project duplication to zero. Standalone clone (`--dissociate`) turned out to be architecturally cleaner than git worktrees once we hit the container-side `.git`-pointer problem.

**Mistakes caught in review:**
- Asymmetric name slugification (fixed)
- Umbrella `.cache` volume shadowing project-baked assets like Playwright browsers (narrowed to `.cache/pip` only)
- `devcontainer down` called in `ws destroy` (no such subcommand; swapped to `docker stop` + `docker rm`)
- Overlay treated as a fragment when `--override-config` actually replaces (now generates a composite devcontainer.json)
- State machine overbuilt (nine states declared, five used; unused ones pruned)

**Things that feel right:** desks as durable addressable places, the outer sandbox as the security boundary (permission prompts inside an already-sandboxed desk are noise), host/container split for the control plane, projects declare only what's project-specific (the overlay handles drydock-side).
