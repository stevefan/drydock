# In-desk RPC

A worker inside a drydock reaches the Harbor-side daemon over a Unix socket that's bind-mounted into the container. JSON-RPC 2.0 over the wire. Identity is a bearer token materialized into `/run/secrets/drydock-token` at drydock create-time.

Vocabulary: [vocabulary.md](vocabulary.md).

## Transport

JSON-RPC 2.0 over a Unix domain socket at `~/.drydock/run/wsd.sock` on the Harbor. The overlay bind-mounts the `~/.drydock/run/` **directory** (not the socket file) into the container at `/run/drydock/`. Directory-bind means a daemon restart that unlinks and recreates the socket inode is transparent to running drydocks — the container resolves `/run/drydock/wsd.sock` through the bind every time it connects.

Socket mode is `0o666`. The bearer token is the security boundary; socket permission only gates transport reachability from uid 1000 (the container's `node` user) against uid 0 (daemon).

HTTP transport is reserved. The wire protocol stays JSON-RPC either way; adding HTTP over Tailscale is additive if a thin multi-Harbor case surfaces.

## Method surface

```
# Lifecycle
CreateDesk(spec)                      → DeskRef
SpawnChild(parent_id, child_spec)     → DeskRef     (nested; narrowness-validated)
StopDesk(desk_id)
DestroyDesk(desk_id, force?)                        (cascades to children)

# Introspection
ListDesks(filter?)                    → [DeskSummary]
ListChildren(parent_id)               → [DeskSummary]
InspectDesk(desk_id)                  → DeskDetail

# Capabilities — see capability-broker.md
RequestCapability(type, scope)        → Lease
ReleaseCapability(lease_id)

# Audit / ops
GetAudit(filter?)                     → [AuditEvent]
wsd.health                            → { ok, pid, version }
wsd.whoami                            → { desk_id }
```

### Three shapes worth naming

1. **`CreateDesk` vs `SpawnChild` are distinct methods.** `CreateDesk` is Harbor-authorised (no parent; caller is the Harbor user). `SpawnChild` is drydock-authorised — the caller is an authenticated drydock holding `spawn_children`, and the narrowness validator runs. Splitting them keeps the trust boundary visible in the RPC surface.

2. **`RequestCapability` is the single entry point for capability leases.** One RPC, typed by `type` + `scope`. New capability types (COMPUTE_QUOTA, NETWORK_REACH) plug in without RPC churn. See [capability-broker.md](capability-broker.md).

3. **`RequestCapability` does NOT take a `desk_id` argument.** The subject drydock is derived from the caller's bearer token via auth middleware. This avoids a confused-deputy class of bug where a compromised or buggy client could request leases targeted at another drydock. `RequestCapability` *does* accept an optional `scope.source_desk_id` for cross-drydock secret delegation — that's entitlement-gated against the **caller's** policy, not the source's.

Read-only introspection (`ListDesks`, `InspectDesk`, `GetAudit`) optionally bypasses the daemon when called by the Harbor CLI — direct SQLite reads produce the same result. Drydock-mode clients always go through the daemon so narrowness applies uniformly.

## Client

### Embedded: `drydock-rpc`

`scripts/drydock-rpc` is a single-file stdlib-only Python JSON-RPC client. `ws host init` deploys it to `~/.drydock/bin/drydock-rpc`; the overlay bind-mounts that path read-only at `/usr/local/bin/drydock-rpc` inside every container.

```
$ drydock-rpc wsd.whoami
{ "desk_id": "ws_auction_crawl" }

$ drydock-rpc RequestCapability \
    type=SECRET \
    scope.secret_name=anthropic_api_key
{ "lease_id": "ls_...", "desk_id": "ws_auction_crawl", ... }

$ drydock-rpc RequestCapability \
    type=STORAGE_MOUNT \
    scope.bucket=drydock-auction-crawl-data \
    scope.prefix=alerts \
    scope.mode=rw
{ "lease_id": "ls_...", "scope": {"bucket":"...","prefix":"alerts","mode":"rw",...} }
```

Dotted keys build nested dicts (`scope.secret_name=X` → `{"scope":{"secret_name":"X"}}`). Values are JSON-parsed when they look like JSON (`true`, `42`, `null`), else raw strings.

Env: `DRYDOCK_WSD_SOCKET` (default `/run/drydock/wsd.sock` inside containers; `~/.drydock/run/wsd.sock` on the Harbor). Token: read from `/run/secrets/drydock-token` and sent as the `auth` field of the JSON-RPC request.

### Harbor CLI

`ws create`, `ws stop`, `ws destroy`, etc. detect `$DRYDOCK_WORKSPACE_ID` env. If set, they route to the daemon (drydock mode). If not, they write SQLite + call devcontainer CLI directly (Harbor mode). `_daemon_overlay_params()` in `src/drydock/cli/create.py` builds the RPC params dict from the project YAML.

## Auth

Opaque base64 token, 32 bytes of entropy, server-generated. No JWT — no claims needed, and rejecting structure shrinks the parsing surface.

Lifecycle:

- Issued at `CreateDesk` or `SpawnChild` (see `src/drydock/wsd/auth.py :: issue_token_for_desk`)
- Materialized at `~/.drydock/secrets/<ws_id>/drydock-token` on the Harbor, mode 0400, owner container-uid (1000). Bind-mount makes it visible at `/run/secrets/drydock-token` inside the container.
- Daemon persists a SHA-256 of the token in SQLite (`tokens` table). The mounted file is the only plaintext copy.
- Revoked automatically on `DestroyDesk` — hash removed from memory + SQLite before teardown completes.
- Rotation reserved (`RotateDeskToken` admin method). For now: re-issue by `ws destroy && ws create`.

Token → `desk_id`. Token does NOT carry capability claims; the daemon looks up the drydock's current capabilities on every request. That means:

- Capability grants/revocations take effect immediately. No token reissue needed after editing project YAML + reloading.
- A leaked token grants only whatever the drydock currently has — nothing more.

Multi-user generalization is reserved: the map becomes `token → (principal_id, desk_id)` when principals become explicit. Adding a principal later is additive; every audit consumer and policy-keyed store participates.

## Idempotency + request IDs

Every RPC request carries a client-generated `request_id` (UUIDv7). The daemon persists `request_id → {method, status, outcome}` in the `task_log` table.

| Method | Idempotency |
|---|---|
| `CreateDesk`, `SpawnChild` | By `request_id`. Retry same id → cached outcome. Retry new id + same `(name, parent_id)` → `desk_exists`. |
| `StopDesk`, `DestroyDesk` | Naturally idempotent. |
| `RequestCapability` | By `request_id`. Without it, retry is unsafe — two leases. Clients MUST send `request_id`. |
| `ReleaseCapability` | By `lease_id`. Replay-safe. |
| `ListDesks`, `InspectDesk`, `GetAudit` | Read-only; always safe. |

The task log is bounded: entries older than 24h **and** in a terminal state are LRU-evicted on daemon boot. In-flight entries never evict — they're the crash-recovery pivot.

See [persistence.md](persistence.md) for the crash-mid-request recovery sweep.

## `ws attach` routes direct

`ws attach` opens a local editor (VS Code, Cursor) connected to the drydock's container via the `vscode-remote+dev-container://` URL pattern. No policy happens there; daemon isn't in the path. In-drydock `ws attach` (drydock → drydock) is not supported.

## Error surface

```json
{
  "error": "narrowness_violated",
  "message": "Requested storage scope exceeds delegatable_storage_scopes",
  "fix": "Add 's3://lab-data/*' to delegatable_storage_scopes in the project YAML",
  "request_id": "018f..."
}
```

`error` is a stable machine-readable code. `message` is human-readable. `fix` is a suggested recovery command; treated as a contract — tests exist to prevent fix-string regressions (per `CLAUDE.md` test discipline).

Common codes: `desk_exists`, `desk_not_found`, `parent_not_found`, `workspace_already_running`, `policy_violation`, `narrowness_violated`, `capability_not_granted`, `capability_unsupported`, `backend_missing_secret`, `backend_permission_denied`, `backend_unavailable`, `desk_not_running`, `materialization_failed`, `storage_backend_not_configured`, `daemon_unavailable`, `invalid_request_id`.
