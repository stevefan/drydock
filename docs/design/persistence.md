# Persistence

Drydocks are durable on their chosen Harbor. Containers come and go; drydocks don't. This doc pins down who owns what state, how it survives daemon restart, container recreate, and Harbor reboot, and how a fresh Harbor can rebuild from config when the old one dies.

Vocabulary: [vocabulary.md](vocabulary.md). In short: **Harbor** = the host running `drydock daemon`; **DryDock** = the durable work environment; **Worker** = the agent inside.

## State ownership

Four surfaces, one primary owner each. Everything else is derived cache.

### SQLite registry (`~/.drydock/registry.db`) — source of truth

- **`workspaces` table** — per-drydock row. Columns: `id`, `name`, `project`, `state`, `container_id`, `worktree_path`, `config` (JSON), plus policy columns `parent_desk_id`, `delegatable_firewall_domains`, `delegatable_secrets`, `delegatable_storage_scopes`, `capabilities`. (`workspaces` is retained as the code-level table name; product vocabulary is "drydock.")
- **`leases` table** — one row per outstanding capability lease (`CapabilityLease` dataclass).
- **`tokens` table** — `drydock_id → token_sha256, issued_at, rotated_at`. Plaintext never stored.
- **`task_log` table** — `request_id, method, spec_json, status, outcome_json, created_at, completed_at`. The daemon's crash-recovery scratch.

### Daemon in-memory — derived cache

- Token → drydock_id map (rebuilt from `tokens` table at boot)
- Request-id LRU (rebuilt from `task_log`; bounded)
- Live lease bookkeeping (rebuilt from `leases`)

Reconstructible from SQLite on boot. Daemon crash loses cache, not state.

### Harbor filesystem — daemon-owned paths

- `~/.drydock/secrets/<dock_id>/` — per-drydock secret files; bind-mounted read-only at `/run/secrets/` inside the container
- `~/.drydock/overlays/<dock_id>.devcontainer.json` — composite devcontainer config
- `~/.drydock/worktrees/<dock_id>/` — git checkout
- `~/.drydock/run/daemon.sock` — daemon RPC socket (directory bind-mounted into drydocks; see [in-desk-rpc.md](in-desk-rpc.md))
- `~/.drydock/bin/drydock-rpc` — embedded in-desk JSON-RPC client
- `~/.drydock/daemon-secrets/` — Harbor-level admin tokens (Tailscale API token, etc.)
- `~/.drydock/audit.log` — append-only JSONL
- `~/.drydock/logs/daemon-systemd.log`, `desks-resume.log` — daemon + boot-sweep logs

All paths are addressable by `dock_id`. The rebuild-from-config runbook is: `tar ~/.drydock/{worktrees,overlays,secrets}/<dock_id>/`, copy the registry row, re-issue the token on the new Harbor.

### Container — ephemeral

Everything inside the drydock container is either rebuildable from the overlay + worktree (package installs, compile artifacts, `.venv`s, shell history) or volume-mounted to a Harbor-owned path if it must survive container recreate (`claude-code-config`, `drydock-vscode-server`, per-project named volumes declared via `extra_mounts`).

`drydock stop && drydock create` drops container-local state by design. Projects that need something to survive volume-mount it.

## Reboot resilience

### Systemd units (Linux Harbors)

Two units in `base/`, installed by `scripts/install-linux-services.sh`:

- **`drydock.service`** — long-running daemon. `Restart=on-failure`, `RestartSec=5`. Binds the socket, sets it to `0o666` so non-root workers can connect.
- **`drydock-desks.service`** — oneshot, `RemainAfterExit=yes`. `After=drydock.service`. On startup, runs `/usr/local/bin/drydock-resume-desks` which polls `drydock daemon status` and then resumes every drydock whose registry state is `suspended` OR whose registry state is `running` but whose container is absent in Docker (ungraceful-shutdown recovery).

systemd reverses ordering on shutdown. `drydock-desks.service`'s `ExecStop=/usr/local/bin/drydock-stop-desks` runs before `drydock.service` stops — so the stop script can call `drydock stop` over the socket while the daemon is still live, authoritatively transitioning running drydocks to `suspended` in the registry. Without this hook, a plain `reboot` would leave the registry stuck at `running` while containers were gone; the boot-sweep's `state=suspended` filter would skip them.

On macOS, the equivalent is a launchd user agent at `~/Library/LaunchAgents/com.drydock.plist`. No boot-sweep equivalent (Docker Desktop's VM covers laptop-close/open).

### Resume-on-CreateDesk

`drydock create <name>` on a drydock whose state is `suspended` or `defined` does not error with `workspace_already_running`. It regenerates the overlay from the current registry `config` + current overlay-code defaults, then `devcontainer up`s a fresh container reusing the existing worktree, named volumes, and bearer token.

The regenerate-on-resume behavior means overlay-code changes (new bind-mounts, new env vars, new default paths) land on existing drydocks without `--force` (which would destroy the worktree). It does NOT reconcile project-YAML drift — the YAML → registry step only happens at create-time. That's a known follow-up (`drydock project reload <name>`).

A new audit event `desk.resumed` fires.

## Crash recovery

### Daemon crashes mid-`CreateDesk` or mid-`SpawnChild`

`task_log` row at `status=in_progress`. On daemon restart, the recovery sweep:

1. Scans `task_log` for `in_progress` entries.
2. For each: inspects Docker for containers matching `devcontainer.local_folder=<overlay_path>`.
   - **Container running, matches spec:** mark `completed`, ensure `workspaces` row exists, emit audit.
   - **Container absent or partial:** roll back — remove overlay, delete `workspaces` row, remove worktree/secrets if present, also remove the `tokens` row (dangling token would authenticate against a nonexistent drydock). Mark `failed` with reason `crashed_during_create`.
3. In-desk clients retry the same `request_id` — daemon returns the reconciled outcome.

### Daemon crashes mid-`DestroyDesk`

Destroy is idempotent. Replay on restart: skip what's already gone, finish what's partial. Parent-child cascade resumes from whatever's left.

### Container dies while daemon up

Docker events stream notifies the daemon. Drydock transitions to `suspended`. Outstanding leases stay valid until expiry (drydock may resume). No auto-restart — user action (`drydock create`) brings it back.

### Devcontainer CLI errors

Propagate, don't retry. `error: devcontainer_failed`, `fix: Check Dockerfile and retry 'drydock create'`. Registry state → `error`. Overlay preserved for debugging. Partial container removed. The one exception: transient Docker socket unavailability — three retries with 200ms backoff before surfacing.

## Leases across daemon restart

Leases persist in SQLite. Re-loaded on startup with a revalidation pass: expired leases (`expiry < now()`) skipped; leases within the 10s safety margin also skipped (drydocks re-request on next use). Avoids a narrow race where the daemon was down long enough for leases to be "almost expired."

Materialized lease files in `~/.drydock/secrets/<caller>/` persist on disk — drydocks keep seeing them until release. If the daemon comes back, the in-memory lease bookkeeping rebuilds from the table.

## Rebuild on a fresh Harbor

Cross-Harbor migration is not a daemon primitive ([_archive/migration-vision.md](../_archive/migration-vision.md) preserves the archived vision). The rebuild runbook covers hardware refresh at a bounded manual cost:

1. `tar ~/.drydock/{worktrees,overlays,secrets,projects}/` on the old Harbor
2. `scp` to the new Harbor
3. Copy `registry.db` (SQLite file — portable)
4. Re-issue tokens (old tokens were Harbor-machine-specific; new ones minted on first `drydock create`)
5. Run `scripts/install-linux-services.sh` to set up systemd units
6. `systemctl start drydock-daemon drydock-desks`

Container-private state (SQLite WAL files, shell history, `.venv`s, tool caches) is fine where it naturally falls. Projects volume-mount what they want to survive.

## Audit event schema

The `~/.drydock/audit.log` JSONL stream is a consumer-facing contract. Event names and required `details` keys are stable — adding events is free, renaming is breaking.

```json
{
  "ts": "2026-04-18T13:30:00.000Z",
  "event": "desk.resumed",
  "principal": "dock_auction_crawl",
  "request_id": "018f...",
  "method": "CreateDesk",
  "result": "ok",
  "details": { "drydock_id": "dock_auction_crawl", "project": "auction-crawl" }
}
```

Current event vocabulary (see [capability-broker.md](capability-broker.md) for lease-side events):

| Event | Emitted on | Required `details` keys |
|---|---|---|
| `desk.created` | `CreateDesk` completes (new) | `drydock_id`, `project`, `parent_desk_id` |
| `desk.resumed` | `CreateDesk` on suspended/defined drydock | `drydock_id`, `project` |
| `desk.spawned` | `SpawnChild` completes | `drydock_id`, `parent_desk_id`, `narrowness_check: allow` |
| `desk.spawn_rejected` | `SpawnChild` validator rejects | `parent_desk_id`, `reject.rule`, `reject.offending_item` |
| `desk.stopped` | `StopDesk` | `drydock_id` |
| `desk.destroyed` | `DestroyDesk` | `drydock_id`, `cascaded_children: [ids]` |
| `desk.error` | devcontainer CLI error / unrecoverable task failure | `drydock_id`, `phase`, `stderr_excerpt` |
| `lease.issued` | `RequestCapability` succeeds | `lease_id`, `drydock_id`, `type`, `scope`, `expiry` |
| `lease.released` | `ReleaseCapability` or revocation | `lease_id`, `reason` |
| `token.issued` | token generated | `drydock_id`, `rotation_reason` (null on first issue) |
| `token.revoked` | drydock destroyed | `drydock_id` |
| `tailnet.device_deleted` | Tailscale API DELETE succeeds | `drydock_id`, `hostname`, `device_id` |
| `tailnet.device_delete_failed` | Tailscale API DELETE fails | `drydock_id`, `hostname`, `device_id`, `error` |

No secret values ever appear in `details` — only names (`secret_name`), hashes, or scope descriptors. `request_id` correlates daemon events to client-side logs.
