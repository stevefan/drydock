# V2 Design — State Ownership, Crash Recovery, V1 Coexistence

**Purpose.** Pin down what the daemon owns, what SQLite owns, what Docker owns, what the container owns — and exactly what happens on daemon crash, container crash, and when a v1 desk runs against a daemon-aware world.

Extends `v2-scope.md` §Migration from V1 and addresses OQ#2 (devcontainer CLI errors) and Topic 8 (V1 coexistence).

---

## 1. State ownership — who holds what

Four storage surfaces. Rule: each piece of state has exactly one primary owner; the rest are derived caches that can be rebuilt.

### SQLite registry (`~/.drydock/registry.db`) — primary owner

Extends the v1 schema with V2 tables. SQLite is source of truth for:

- **`desks` table** (v1's `workspaces` table, renamed via migration or kept as-is per vocabulary doc):
  - v1 columns: `id`, `name`, `project`, `state`, `created_at`, `container_id`, `worktree_path`, …
  - v2 additions (per `v2-scope.md`): `parent_desk_id`, `delegatable_firewall_domains` (JSON), `delegatable_secrets` (JSON), `capabilities` (JSON). Resource budgets deferred to V3 — see capability-broker §4.
- **`leases` table** (new): columns per `CapabilityLease` dataclass. Every outstanding lease persisted so restart doesn't drop them.
- **`tokens` table** (new): `desk_id → token_sha256, issued_at, rotated_at`. Plaintext never stored.
- **`task_log` table** (new): `request_id, method, spec_json, status (in_progress|completed|failed), outcome_json, created_at, completed_at`. See §3.
- **`audit` events**: provisionally append-only JSONL at `~/.drydock/audit.log` (v1 convention); optionally migrate to SQLite table in V2 if query shape demands it. Default: keep JSONL, add daemon-written entries alongside v1 CLI entries. Schema in §1a.

### 1a. Audit event schema

Every daemon action emits one JSONL line. Fields:

```json
{
  "ts": "2026-04-14T14:23:00.000Z",
  "event": "desk.created",
  "principal": "ws_microfoundry",
  "request_id": "018f...",
  "method": "SpawnChild",
  "result": "ok",
  "details": { ... }
}
```

**Event types** (V2 set; extended additively in V3/V4):

| Event | Emitted on | Required `details` keys |
|---|---|---|
| `desk.created` | `CreateDesk` completes | `desk_id`, `project`, `parent_desk_id` (null for host-created) |
| `desk.spawned` | `SpawnChild` completes | `desk_id`, `parent_desk_id`, `narrowness_check: allow` |
| `desk.spawn_rejected` | `SpawnChild` validator rejects | `parent_desk_id`, `reject.rule`, `reject.offending_item` |
| `desk.stopped` | `StopDesk` | `desk_id` |
| `desk.destroyed` | `DestroyDesk` | `desk_id`, `cascaded_children: [ids]` |
| `desk.error` | devcontainer CLI error / unrecoverable task failure | `desk_id`, `phase`, `stderr_excerpt` |
| `lease.issued` | `RequestCapability` succeeds | `lease_id`, `desk_id`, `type`, `scope`, `expiry` |
| `lease.renewed` | `RenewCapability` | `lease_id`, `old_expiry`, `new_expiry` |
| `lease.released` | `ReleaseCapability` or revocation | `lease_id`, `reason` (`client_release`, `desk_destroyed`, `expired`, `policy_violation`) |
| `token.issued` | token generated (create or rotate) | `desk_id`, `rotation_reason` (null for first issue) |
| `token.revoked` | on desk destroy | `desk_id` |
| `tailnet.device_deleted` | successful Tailscale API DELETE during `DestroyDesk` cleanup or `PruneStaleTailnetDevices` | `desk_id` (nullable for orphan prune), `hostname`, `device_id` |
| `tailnet.device_delete_failed` | Tailscale API DELETE returns non-2xx or connection fails | `desk_id` (nullable), `hostname`, `device_id` (nullable if resolve failed), `error` |

**Contract commitments.**
- `event` and `result` string values are stable — tests exist to prevent silent rename.
- `details` may grow additively per event. Consumers tolerate unknown keys.
- No secret values ever appear in `details`. Only names (`secret_name`), hashes, or scope descriptors.
- `request_id` correlates daemon events to client-side logs.

**Why spec it now.** Audit consumers (the future ops / dashboards / weekly "what did agents do" reviews) depend on stable event names. Changing event names or required-details fields later is a breaking change for every consumer. Committing the shape in V2 means V3/V4 add events without reshaping existing ones.

**Reversibility: MEDIUM.** The event-name vocabulary is a consumer-facing contract. Adding events is free; renaming or restructuring is breaking.

### Daemon in-memory — derived cache

- **Token → desk_id map** (rebuilt from `tokens` table at boot).
- **Active Docker container IDs per desk** (rebuilt via `docker ps` filtered by overlay labels at boot).
- **Request-id LRU cache** (rebuilt from `task_log`; bounded).
- **Live lease bookkeeping** (rebuilt from `leases` table).
- **Docker event stream subscription state** (transient).

All in-memory state is reconstructible from SQLite and Docker. Daemon crash does not lose state — it loses cache.

### Host filesystem — daemon-owned paths

- `~/.drydock/secrets/<ws_id>/` — Phase-2 secrets backend store (encrypted at rest via host keychain-derived key once implemented).
- `~/.drydock/overlays/<ws_id>.devcontainer.json` — composite devcontainer.json per desk.
- `~/.drydock/worktrees/<ws_id>/` — git checkout per desk.
- `~/.drydock/leases/<lease_id>` — tmpfs-mounted lease materialization points (if we use a per-lease file-mount approach; alternative is a single `/run/secrets/` dir with all active leases materialized as files).
- `~/.drydock/wsd.sock` — daemon RPC socket.
- `~/.drydock/logs/wsd.log` — daemon log.
- `~/.drydock/audit.log` — audit stream.

**All paths are addressable by `ws_id`** so V3 migration is a tar-and-ship of owned paths + DB export.

### Container — ephemeral

Everything inside the desk container is either:
- **Rebuildable** from the overlay + worktree (package installs, compile artifacts).
- **Host-volume-mounted** if it should persist across container recreate (shared v1 pattern: `claude-code-config`, `drydock-vscode-server`, per-project named volumes).

Container-local state (shell history in `/root`, untracked files in `/tmp`) is lost on `ws stop` + `ws create` cycle. This is by design (per v1 ephemeral-container-lifecycle branch just merged).

## 2. Serializability for V3 (forward-compat)

V2 doesn't migrate, but every piece of state must be serializable so V3 can:

| Property | How V2 enforces |
|---|---|
| No host-specific absolute paths in registry | All paths constructed from `ws_id` + host prefix at runtime; registry stores relative paths where possible |
| No host-clock-relative timestamps | All timestamps UTC-absolute; lease `expiry` is absolute instant, never "5 minutes from now" at persistence |
| No host-specific tokens in container | Token is opaque; re-issue on migration (source host's tokens invalidated on migrate-out) |
| Leases portable | `issuer` field lets V3 track which host issued; on migration, issuer rewrites to destination host |
| Container state recoverable | Anything in container-local filesystem that mustn't be lost is volume-mounted to `~/.drydock/...` (host-owned, serializable) |

Practical V3 migration is `tar ~/.drydock/{worktrees,overlays,secrets,leases}/<ws_id>/` + registry row export + tokens re-issued on destination. V2 doesn't implement this; V2 just doesn't preclude it.

**Reversibility: HIGH** on "no host-specific state in container." Breaking this invariant by V2 means V3 is either a rewrite or a buggy migrator. Guarded by CI lint: registry-write helpers reject absolute paths that aren't under `~/.drydock/`.

## 3. Crash recovery

### Daemon crashes mid-`CreateDesk` (or `SpawnChild`)

Task log entry exists with `status=in_progress`. On daemon restart:

1. Scan `task_log` for `in_progress` entries.
2. For each entry, reconcile:
   - Inspect Docker for containers matching the spec's overlay label (`devcontainer.local_folder=<overlay_path>`).
   - **Container running + matches spec:** mark task `completed`, ensure `desks` row exists (insert if `CreateDesk` didn't commit before crash), emit audit event.
   - **Container absent OR container exists but partial** (e.g., no network, init script failed): roll back.
     - Remove overlay file if present.
     - Delete `desks` row if present.
     - Remove worktree if partial.
     - Remove any secrets mount dir.
     - Mark task `failed` with reason `crashed_during_create`.
3. Desk-mode client retries the same `request_id`; daemon returns the reconciled outcome.

**Subtle case: token issued but desk row rolled back.** On rollback, also remove the `tokens` row. Otherwise a dangling token (desk-side file exists) could authenticate against a nonexistent desk.

### Daemon crashes mid-`DestroyDesk`

- Destroy is idempotent. On restart, replay the destroy: if container gone, proceed; if registry row gone, treat as complete.
- Children destroyed before parents (cascade); on restart with partial cascade, resume with whatever's left.

### Container dies while daemon up

- Docker event stream notifies daemon (subscribe via Docker events API).
- Daemon marks desk `suspended` in registry.
- Outstanding leases remain valid (desk may resume).
- `ws status` reflects suspended state.
- **No auto-restart** (v1 convention; containers restart via user action).

### Daemon up, devcontainer CLI errors (addresses OQ#2)

Philosophy: **propagate, don't retry.**

- Devcontainer failures are almost always policy (missing secret, bad Dockerfile, firewall-blocked registry pull) or environment (Docker not running).
- Silent retry creates thrash, masks real errors, and burns time on transient-looking-but-actually-persistent failures.
- Daemon's response: propagate the error structured — `error: "devcontainer_failed", message: <stderr snippet>, fix: "Check Dockerfile and retry 'ws create'"`.
- Registry row moves to `error` state. Overlay file preserved for debugging. Partial container (if any) removed.
- Audit event emitted.
- Client decides whether to retry.

**One exception:** transient Docker socket unavailability (daemon restarted, connection blip). The daemon retries docker-API calls up to 3 times with 200ms backoff before surfacing as a devcontainer failure. This is Docker-API resilience, not devcontainer-error retry.

## 4. Active leases on daemon restart

- Leases persisted in SQLite, re-loaded into memory on startup.
- Re-validate expiry at load: `expiry < now() → mark revoked, skip loading`.
- **Safety margin:** leases with `expiry < now() + 10s` are also skipped. Desks re-request on next use. Prevents a narrow race where the daemon was down long enough for leases to be "almost expired."
- Tmpfs lease files inside running desks are not re-materialized at daemon startup — the daemon has no way to push into a running container reliably. Desks re-request capabilities (clients reissue `RequestCapability` when they get `lease_not_found` or when the file they expected is absent). Brief re-request storm acceptable; daemon restart is rare.

## 5. V1 coexistence contract

**Baseline assumption.** v1 desks exist on Steven's host today. V2 ships; daemon runs; existing desks must keep working. Daemon is opt-in: `wsd.toml` optional, `wsd` service optional, old behavior preserved if daemon absent.

### 5a. Operation-level routing

| Operation | V1 (no daemon) | V2 (daemon present), host CLI | V2 (daemon present), desk-mode CLI |
|---|---|---|---|
| `ws create <project>` | direct (writes registry, calls devcontainer) | **direct**; daemon observes via audit | RPC (`CreateDesk`) — only if called from a desk |
| `ws create --parent X <child>` | n/a (no nesting in v1) | RPC (`CreateDesk` with parent) | RPC (`SpawnChild`) — policy validated |
| `ws stop <name>` | direct | RPC (so lease revocation cascades) | RPC |
| `ws destroy <name>` | direct | RPC (cascade, lease revocation, token revocation) | RPC |
| `ws list`, `ws inspect` | direct (SQLite reads) | **direct** (SQLite reads — same result) | RPC (so desk sees only its own or its children) |
| `ws attach` | direct (editor launch) | **direct** | **direct** (no desk→desk attach in V2) |
| `ws exec` | direct (`docker exec`) | direct | RPC routing for desk-side future work; v2 stays direct |
| `ws secret set/list/rm` | direct (filesystem writes) | **direct** in Phase 2 transition (writes to daemon store via daemon API if present, falls back to file-backed if not); RPC in Phase 3 | RPC |

**Bolded "direct"** = bypasses daemon on purpose (read-only introspection, UI helper ops). These are the operations the user can always run even if daemon is down.

### 5b. Registry schema evolution

New columns default to `NULL` / empty JSON on existing rows. No data migration needed at upgrade:

```sql
ALTER TABLE workspaces ADD COLUMN parent_desk_id TEXT DEFAULT NULL;
ALTER TABLE workspaces ADD COLUMN delegatable_firewall_domains TEXT DEFAULT '[]';
ALTER TABLE workspaces ADD COLUMN delegatable_secrets TEXT DEFAULT '[]';
ALTER TABLE workspaces ADD COLUMN capabilities TEXT DEFAULT '[]';
```

New tables (`leases`, `tokens`, `task_log`) created on daemon first-start if absent.

Rows with `parent_desk_id IS NULL` are treated as host-created v1 desks; narrowness invariant trivially holds (no parent to be narrower than); daemon enforces no policy on them.

### 5c. Bringing v1 desks under daemon management

V2 does **not** ship `ws adopt`. The design considered live-adoption (inject token into running container, mark as daemon-managed) but it needs a container restart to mount the new secret anyway. In practice: `ws destroy <name> && ws create <name>` is the V1 → V2 on-ramp. Simpler, fewer edge cases, same outcome.

Existing v1 desks keep running unchanged (`parent_desk_id IS NULL` → daemon treats as host-created, no policy). Users opt in to V2 features by recreating desks; nothing forces migration.

### 5d. Failure modes when daemon dies mid-session

| Situation | Behavior |
|---|---|
| Host CLI invocation, daemon socket absent | Fall back to v1 direct path; log `warning: daemon unavailable, direct mode` |
| Host CLI, daemon socket present but unresponsive | 2-second timeout, then fall back to direct, log warning |
| Desk-mode CLI | Return `daemon_unavailable` to caller. Caller retries (launchd/systemd restarts daemon in seconds). No fallback — desk-mode ops require policy validation, which direct mode can't provide |
| In-flight leases in running desks | Continue to work until `expiry` (since the daemon isn't in the data path for using a lease — it's only in the path for issuing/renewing). Renewal requests fail until daemon recovers |
| In-flight `CreateDesk` from desk-mode | Client sees `daemon_unavailable` after timeout. On daemon restart, replay same `request_id`; daemon reconciles. See §3 |

## 6. Pre-V2 dependency: close the v1 volume-preservation test gap

Not blocking design, but blocking implementation. The `ephemeral-container-lifecycle` branch merged to main has `test_force_rebuild_preserves_checkout` covering **checkout preservation**, but the original-intent test (seed file in a named volume via `extra_mounts`, verify survival after `ws create --force`) was scope-reduced. No regression test covers named-volume survival.

V2's daemon will inherit the v1 "thin-runtime / thick-volumes" contract. If v1 has no test for named-volume survival and V2 introduces a regression in container teardown semantics, we won't catch it. Close the v1 gap first:

- [ ] Add `test_force_rebuild_preserves_named_volume` that seeds a file via `extra_mounts` → `ws create --force` → assert file present.
- [ ] Tag `v0.1.1` once the test lands.

Called out in `v2-design-overview.md` verification checklist.

## 7. Daemon test strategy

The daemon is the first long-running process in Drydock. Tests cover four layers:

1. **In-process unit tests for pure components.** `validate_spawn`, canonicalization, lease math, audit-event construction. No subprocess needed. Fastest feedback, largest surface. The capability-broker §5 fuzz discipline applies here.

2. **Subprocess integration tests.** Spawn `wsd` in a subprocess with a temp socket (`tmp_path / "wsd.sock"`), point a test client at it, exercise a method, tear down. Use a pytest fixture handling lifecycle + cleanup. One test per method for happy path + one per error-surface contract (`fix:` field stability). Validator edge cases stay in unit tests.

3. **Crash-recovery tests.** SIGKILL the subprocess mid-`CreateDesk` (between "task_log entry written" and "response sent"), restart, verify reconciliation produces the expected state. Use fault injection via env var the daemon reads at specific hook points (e.g., `DRYDOCK_CRASH_AT=post_task_log_write`).

4. **V1 coexistence smoke tests.** Registry upgraded from v1 schema in-place; daemon starts against it; existing v1 desks remain usable; host CLI falls back to direct mode when daemon absent.

**Fixture sketch:**

```python
@pytest.fixture
def wsd(tmp_path):
    sock = tmp_path / "wsd.sock"
    proc = subprocess.Popen([
        "python", "-m", "drydock.wsd",
        "--socket", str(sock),
        "--registry", str(tmp_path / "reg.db"),
    ])
    wait_for_socket(sock, timeout=5)
    yield Client(sock)
    proc.terminate()
    proc.wait(timeout=5)
```

**Explicitly not in V2 test discipline:**
- Load / scale tests (single user, ~10 desks; scale concerns are V3).
- Long-running soak tests (restart cadence is human-driven).
- Fuzzing beyond the canonicalization fuzz specced in capability-broker §5.

Per `CLAUDE.md §Tests must justify their existence`: every daemon test must answer the justification questions. "The daemon starts" is vanity; "the daemon starts, accepts a bind, and returns a structured error when the socket is already in use" is a contract.

## 8. Reversibility audit

| Decision | Cost | Notes |
|---|---|---|
| SQLite as primary state store | Low | Migration to Postgres / other is one-time ETL |
| Task log persistence | Low | Internal, append-only, prunable |
| `parent_desk_id` nullable column | Low | Additive |
| V1 coexistence = daemon opt-in | Low | Can tighten later |
| File-backed leases under `~/.drydock/secrets/` | Medium | Shared disk layout; in-memory-tmpfs-only mode would need container rebuild |
| "Propagate, don't retry" devcontainer errors | Low | Can layer retry later without API change |
| No auto-restart on container death | Low | Matches v1; can add flag later |
| Destroy+create as the v1→v2 on-ramp (no `ws adopt`) | Low | Can add live-adoption later if pressure surfaces |
| "No host-specific state in containers" invariant | **HIGH** | If broken, V3 migration either is a rewrite or a buggy migrator. CI lint + reviewer discipline required |
| Audit event schema (names + required `details` keys — §1a) | Medium | Consumer-facing contract; adding events is free, renaming is breaking. Commit the shape in V2 so V3/V4 extend additively |
