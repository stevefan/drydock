# Drydock — Capability Broker

**Purpose.** The `wsd` daemon is the sole issuer of time-bounded, scoped grants called **capability leases**. A worker inside a drydock asks the daemon for a lease; the daemon checks entitlements, mints backend state, materializes the result into the drydock, and records an audit event. Revocation is symmetric.

This is the trust boundary. Workers hold no long-lived credentials out-of-band. The daemon translates declared entitlements into backend-specific state (a file on disk, an STS session) and back.

See [vocabulary.md](vocabulary.md) for Harbor / DryDock / Worker.

---

## 1. What a capability lease is

```python
@dataclass(frozen=True)
class CapabilityLease:
    lease_id: str                      # ls_<uuid4>; new id each issuance
    desk_id: str                       # subject; derived from bearer token
    type: CapabilityType               # SECRET | STORAGE_MOUNT | INFRA_PROVISION | (reserved)
    scope: dict                        # type-specific JSON
    issued_at: datetime                # UTC
    expiry: datetime | None            # UTC; None = live until release/destroy
    issuer: str                        # always "wsd"
    revoked: bool = False
    revocation_reason: str | None = None
```

Persisted 1:1 in the `leases` SQLite table. Mutability after issuance is limited to revocation. Code: `src/drydock/core/capability.py`.

**`scope` is an unversioned dict by design.** Treat as append-only per type: never rename keys, never narrow value types. Adding a new key is safe; changing an existing key's meaning is not.

**`expiry` policy.** `SECRET` leases default to `None` — live until the caller calls `ReleaseCapability` or the drydock is destroyed. STS-backed leases (`STORAGE_MOUNT`, `INFRA_PROVISION`) always carry a concrete expiry (the STS session's own expiration, typically 4h). Finite-TTL + auto-renew machinery is reserved for backends that need it, not universal.

**No `parent_lease_id`.** Narrowness is enforced at spawn time (see [narrowness.md](narrowness.md)); children get independent leases against their own pinned entitlements. If sub-letting ever becomes required, add the field then.

## 2. RPC surface

Two methods on the JSON-RPC socket at `~/.drydock/run/wsd.sock`:

```
RequestCapability(type, scope)          → CapabilityLease
ReleaseCapability(lease_id)             → {lease_id, revoked}
```

The subject drydock is **always derived from the caller's bearer token** via the auth middleware — never taken as an argument. This avoids a confused-deputy class of bugs where one drydock could request a lease aimed at another. The daemon maps `token → desk_id` against the SHA-256 of the token in the registry.

Clients MUST send a `request_id` on `RequestCapability` — without it, retry would double-issue.

Handler entry points: `request_capability()` and `release_capability()` in `src/drydock/wsd/capability_handlers.py`.

### Scope shapes

`SECRET`:
```json
{"secret_name": "anthropic_api_key"}
{"secret_name": "shared_key", "source_desk_id": "ws_other"}   // cross-desk
```

`STORAGE_MOUNT`:
```json
{"bucket": "my-data", "prefix": "exports", "mode": "rw"}
```

`INFRA_PROVISION`:
```json
{"actions": ["s3:CreateBucket", "iam:PutRolePolicy"]}
```

## 3. Capability types

| Type | Status | Scope shape | Narrowness field |
|---|---|---|---|
| `SECRET` | Shipped | `{secret_name, source_desk_id?}` | `delegatable_secrets` |
| `STORAGE_MOUNT` | Shipped | `{bucket, prefix, mode}` | `delegatable_storage_scopes` |
| `INFRA_PROVISION` | Shipped | `{actions: [str, ...]}` | `delegatable_provision_scopes` |
| `COMPUTE_QUOTA` | Enum-reserved; `capability_unsupported` | — | — |
| `NETWORK_REACH` | Enum-reserved; `capability_unsupported` | — | — |

`STORAGE_MOUNT` and `INFRA_PROVISION` share the `aws_*` materialization slots in `/run/secrets/` — a drydock holds at most one active AWS lease at a time. Issuing either type supersedes the prior active lease of either type (see `registry.find_active_aws_lease`). Releasing the last active AWS lease removes the files.

Project YAML does not accept reserved type names. The reservation is an internal enum commitment (`CapabilityType` in `capability.py`) so the daemon can reject cleanly without per-type code.

## 4. Backend protocol

Two backends, one pattern: the daemon dispatches the request to a Protocol-typed backend that returns opaque bytes or a credential bundle. Materialization is daemon-owned, not backend-specific.

### `SecretsBackend` (`src/drydock/core/secrets.py`)

```python
class SecretsBackend(Protocol):
    name: str
    def fetch(self, secret_name: str, desk_id: str) -> bytes | None: ...
    def supports_rotation(self) -> bool: ...
    def rotate(self, secret_name: str) -> bytes | None: ...
```

Current implementation: **`FileBackend`**. Reads from `~/.drydock/secrets/<desk_id>/<name>` (mode 0400, owned by the Harbor user). The same file tree that `ws secret set` writes.

Raises `BackendPermissionDenied` / `BackendUnavailable` for the daemon to translate into RPC errors. Rotation not supported; future network backends (1Password via `op`, Vault, cloud SMs) plug in as additive classes plus a `[secrets] backend = ...` entry in `wsd.toml`. Sync-first: network backends should add `fetch_async` additively.

### `StorageBackend` (`src/drydock/core/storage.py`)

```python
class StorageBackend(Protocol):
    name: str
    def mint(self, *, desk_id, bucket, prefix, mode) -> StorageCredential: ...
    def mint_provision(self, *, desk_id, actions) -> StorageCredential: ...
```

Current implementations:

- **`StsAssumeRoleBackend`** — calls `aws sts assume-role` against the configured `drydock-agent` role with an inline session policy. `mint` uses `build_session_policy(bucket, prefix, mode)`; `mint_provision` uses `build_provision_session_policy(actions)` which grants the requested IAM action list on `Resource: *`. The long-lived `drydock-runner` IAM keys stay on the Harbor; the worker only ever sees the scoped session credential. Default session duration 4h.
- **`StubStorageBackend`** — deterministic fake credentials for tests and for Harbors without AWS wired up.

Both session-policy builders are pure functions. Storage-mount mode vocabulary: `ro` (`GetObject` + `ListBucket`), `rw` (additionally `PutObject` + `DeleteObject`). Provision action strings are bare IAM actions (`s3:CreateBucket`, `iam:*`, `*`) matched as fnmatch globs — no AWS-side ARN narrowing, only action-level, because provisioners create resources that don't exist yet. The `drydock-agent` permission boundary is the ceiling regardless.

## 5. Materialization

The daemon writes lease bytes into `~/.drydock/secrets/<desk_id>/` on the Harbor, which the overlay bind-mounts read-only at `/run/secrets/` inside the drydock container. Files are chowned to the container's node uid (1000) and `chmod 0400`; the daemon runs as root on Linux Harbors but the worker is uid 1000, so a root-owned 0400 file would be unreadable.

Per-type mapping:

| Type | Files written | Location |
|---|---|---|
| `SECRET` same-desk, file-backed | none (already visible via bind mount) | — |
| `SECRET` cross-desk, file-backed | `<secret_name>` | `~/.drydock/secrets/<caller>/` |
| `SECRET` non-file backend | `<secret_name>` via `docker exec` | `/run/secrets/<name>` (active) |
| `STORAGE_MOUNT` / `INFRA_PROVISION` | `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`, `aws_session_expiration` | `~/.drydock/secrets/<caller>/` |

The four `aws_*` filenames match the `drydock-base` `sync-aws-auth.sh` convention — a worker reads them directly or exports them as env vars. Writes are not atomic across the four files; workers poll `aws_session_expiration` for freshness.

On release the daemon deletes the files it materialized. For same-desk file-backed secrets (owned by `ws secret set`, not the daemon) nothing is removed. Cross-desk materializations are removed only after the last active lease for that `(desk_id, secret_name)` pair is revoked.

## 6. Cross-drydock delegation

A drydock may request a `SECRET` lease against a secret held by a different drydock by passing `source_desk_id` in the scope. The daemon:

1. Validates the caller's own `REQUEST_SECRET_LEASES` capability and that `secret_name` is in the caller's `delegatable_secrets` (the caller must have been granted this secret in its own project YAML).
2. Verifies `source_desk_id` exists.
3. Fetches bytes from the source drydock's secret dir.
4. Writes the bytes into the caller's secret dir (cross-desk file-backed) so the caller's bind mount picks them up.

The gate is on the **caller's** entitlements, not the source's. Project YAML authors pre-declare which secrets a drydock may pull from peers.

## 7. Single-active-lease semantics

- **`SECRET` same-desk**: reissuing is idempotent in effect — the file is already visible; the registry simply gets another row.
- **STS-backed types (`STORAGE_MOUNT`, `INFRA_PROVISION`)**: one active AWS lease per drydock, across both types combined. On new issue of either type, any prior active lease of either type for the caller is implicitly revoked with `revocation_reason = "superseded"` and a `lease.released` audit event. The four `aws_*` files are overwritten in place. This keeps release cleanup unambiguous — we always know which lease currently owns the files.

Revocation path: `registry.find_active_aws_lease()` inside the STS handlers in `capability_handlers.py`.

## 8. Audit

Every issue and release emits an event via `emit_audit()` in `src/drydock/core/audit.py`:

```json
{
  "event": "lease.issued",
  "principal": "ws_scraper",
  "method": "RequestCapability",
  "result": "ok",
  "details": {
    "lease_id": "ls_...",
    "desk_id": "ws_scraper",
    "type": "STORAGE_MOUNT",
    "scope": {"bucket": "my-data", "prefix": "exports", "mode": "rw"},
    "expiry": "2026-04-18T12:00:00+00:00"
  }
}
```

Event names: `lease.issued`, `lease.released`. Supersede-on-new-issue emits a `lease.released` with `reason: "superseded_by_new_storage_lease"` followed by a `lease.issued` for the new lease.

## 9. Error taxonomy

All errors return the standard `{error, message, fix?, data?}` JSON-RPC envelope. Stable codes:

| Code | `message` | When |
|---|---|---|
| -32001 | `desk_not_found` / `source_desk_not_found` | Caller or source drydock missing from registry |
| -32004 | `unauthenticated` | No caller desk derived from token |
| -32005 | `capability_not_granted` | Caller lacks `REQUEST_SECRET_LEASES` / `REQUEST_STORAGE_LEASES` |
| -32006 | `narrowness_violated` | Requested scope not in caller's delegatable set; `data.rule` identifies which |
| -32007 | `backend_permission_denied` | Backend rejected the fetch/mint |
| -32008 | `backend_unavailable` | Transient backend failure; `data.retry = true` |
| -32009 | `backend_missing_secret` | Backend resolved but no secret under that name; `fix` echoes the `ws secret set` command |
| -32010 | `desk_not_running` | Caller's container isn't up; can't materialize |
| -32011 | `materialization_failed` | File write / `docker exec` failed after mint |
| -32012 | `lease_not_found` | `ReleaseCapability` against unknown or foreign lease_id |
| -32013 | `capability_unsupported` | Reserved type (`COMPUTE_QUOTA`, `NETWORK_REACH`) |
| -32015 | `storage_backend_not_configured` | `STORAGE_MOUNT` or `INFRA_PROVISION` requested but no backend in `wsd.toml` |
| -32016 | `storage_backend_config_error` | Backend present but misconfigured (missing role ARN, etc.) |
| -32602 | `invalid_params` | Malformed scope; `data.reason` describes the defect |

`ReleaseCapability` returns `lease_not_found` even when the lease exists but belongs to another drydock — don't leak existence across the trust boundary.
