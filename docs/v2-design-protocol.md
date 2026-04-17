# V2 Design — RPC Surface, Auth, Wire Protocol

**Purpose.** Pin down the wire between clients (host CLI, in-desk CLI, later: mobile apps) and `wsd`. Names the methods, the transport, the auth model, and what happens on daemon restart.

Extends `v2-scope.md` §Protocol sketch + §Auth and addresses open questions OQ#1 (daemon location) and OQ#3 (`ws attach` routing).

---

## 1. Transport

**Decision.** JSON-RPC 2.0 over a Unix domain socket at `~/.drydock/wsd.sock` (mode `0600`, owner = host user). Socket is bind-mounted into every desk via the overlay so desk-mode callers reach the daemon without networking.

**HTTP/Tailscale transport is deferred, not shipped in V2.** `v2-scope.md` originally proposed HTTP/Tailscale; in V2 (single-host, single-daemon) the socket is simpler, cheaper, and removes an authentication surface that has no V2 caller. Wire protocol is identical either way; adding HTTP later reuses the same JSON-RPC envelope with bearer auth — useful if a thin multi-host case ever surfaces (second host runs its own daemon, `ws` CLI on either host can target either daemon). Not a V2 goal.

**Rejected alternative: gRPC.** Over-engineered for V2's method count (~10). JSON-RPC is human-debuggable (can `nc` the socket), has no tooling overhead, and stays out of the way.

**Reversibility: MEDIUM.** Switching wire protocol later (to gRPC, Cap'n Proto, etc.) means every client re-implementation. JSON-RPC 2.0 is conservative and debuggable; this is the choice most likely to age well. Adding HTTP transport later is additive — same envelope.

## 2. Method surface

```
# Lifecycle
CreateDesk(spec)                          → DeskRef
SpawnChild(parent_id, child_spec)         → DeskRef      (nested; policy-validated)
StopDesk(desk_id)
DestroyDesk(desk_id, force?)                              (cascades to children)

# Introspection
ListDesks(filter?)                        → [DeskSummary]
ListChildren(parent_id)                   → [DeskSummary]
InspectDesk(desk_id)                      → DeskDetail

# Capabilities (see v2-design-capability-broker.md)
RequestCapability(type, scope, ttl?)      → Lease         (subject = authenticated desk from bearer token)
RenewCapability(lease_id, ttl?)           → Lease
ReleaseCapability(lease_id)

# Audit / ops
GetAudit(filter?)                         → stream of AuditEvent
```

`RotateDeskToken` reserved (no V2 caller; token rotation is rare, and for V2 the recovery mechanism is destroy+create). Ship in V2.5 if operational need surfaces.

**Three shapes to note.**

- **`CreateDesk` vs. `SpawnChild`** are intentionally distinct methods. `CreateDesk` is host-authorised (no parent; caller is the host user). `SpawnChild` is desk-authorised (caller is a desk with `spawn_children` capability; policy validator runs). Collapsing them would require every `CreateDesk` to pass a null-parent sentinel and the validator to special-case it. Splitting them up front keeps the trust boundary visible in the RPC surface.
- **`RequestCapability` is the single entry point** for capability leases. In V2 the only implemented type is `SECRET`; V4's cloud types plug in additively. `GetSecret` and friends are not distinct methods. See `v2-design-capability-broker.md`.
- **`RequestCapability` does NOT take a `desk_id` argument.** The subject desk is derived from the caller's bearer token via the auth middleware. This avoids a confused-deputy class of bugs where a compromised or buggy client could request leases targeted at another desk. Admin impersonation (where an operator acts on behalf of a desk) uses a separate `AdminRequestCapability(desk_id, ...)` method guarded by a host-admin token, not the daemon's standard bearer auth — **deferred to V2.5 if ever needed**; no V2 callers today.
- **Read-only introspection (`ListDesks`, `InspectDesk`, `GetAudit`) can optionally bypass the daemon** when called by host CLI — direct SQLite reads produce the same result. Desk-mode clients always go through the daemon for narrowness.

## 3. Idempotency and request IDs

Every RPC request carries a client-generated `request_id` (UUIDv7 for ordering). Daemon maintains a persistent task log (see `v2-design-state.md`) mapping `request_id → {method, status, outcome}`.

| Method | Idempotency guarantee |
|---|---|
| `CreateDesk`, `SpawnChild` | By `request_id`. Retry with same id → returns cached outcome. Retry with new id + same `(name, parent_id)` where desk already exists → reject with `desk_exists`. |
| `StopDesk` | Naturally idempotent. Stopping a stopped desk returns success. |
| `DestroyDesk` | Naturally idempotent. Destroying a gone desk returns success. |
| `RequestCapability` | By `request_id`. Without id, the call is **not** safe to retry — the daemon would issue two leases. Clients MUST send `request_id`. |
| `RenewCapability`, `ReleaseCapability` | By `lease_id`; replay safe. |
| `ListDesks`, `InspectDesk`, `GetAudit` | Read-only; always safe. |

The daemon keeps the task log bounded: LRU-evicts entries older than 24h **and** in a terminal state. In-flight entries never evict.

## 4. In-flight requests on daemon restart

The dangerous case: daemon crashes mid-`CreateDesk`, between "devcontainer up started" and "registry row written + response sent."

**Recovery flow** (detailed in `v2-design-state.md`):
1. On startup, daemon scans the task log for entries in `in_progress` state.
2. For each: reconcile against Docker + filesystem state.
   - Container running + matches spec → mark `completed`, register desk if missing.
   - Container absent or partial → roll back (remove overlay file, delete registry row if present), mark `failed`.
3. Client-side: desk-mode CLI retries the same `request_id`. Daemon returns the reconciled outcome. If the task failed, the client sees the failure. If it succeeded, the client sees the DeskRef.

**Why this matters.** Without this, a flaky network during `CreateDesk` can produce a phantom desk: container exists, no registry row, client saw a timeout. Nested-spawn scenarios make this worse because the parent desk's retry logic must be deterministic.

## 5. Auth — bearer tokens

**Format.** Opaque base64, 32 bytes of entropy, server-generated. No JWT: we don't need claims, and rejecting structure reduces parsing surface.

**Lifecycle.**
- Issued at `CreateDesk` or `SpawnChild`.
- Mounted into the desk at `/run/secrets/drydock-token`, tmpfs, mode `0400`, owner container-uid.
- Daemon persists a SHA-256 of the token in SQLite (not the plaintext); the mounted file is the only copy. If the desk's tmpfs is wiped, the desk is unauthenticated until a rotation.
- Rotation reserved (design allows `RotateDeskToken` admin method; not shipped in V2). For V2, re-issue token by destroy+create.
- Revoked automatically on `DestroyDesk`. Token's hash removed from daemon memory + SQLite before teardown completes.

**Scope.** Token → `desk_id`. Token does NOT carry capability claims; the daemon looks up capabilities live per request. This means:
- Capability grants/revocations take effect immediately (no token reissue).
- Leaked token grants whatever the desk currently has, nothing more.

**Per-principal generalization (deferred multi-user).** The map becomes `token → (principal_id, desk_id)`. Multi-user tokens include a principal. V2 ships with `principal_id` implicitly = owner; the schema accommodates explicit principals without a breaking change. See `project_multi_user_sketch.md`. Not tied to migration; can ship when the multi-user case surfaces.

**Rejected alternatives:**
- **mTLS via Tailscale device identity.** Strong but couples V2 to Tailscale beyond transport. If a desk ever runs outside Tailscale (dev host without tailnet, CI), identity breaks.
- **Unix-socket peer credentials only.** Works single-host-single-daemon. If a thin multi-host case ever surfaces, peer creds don't travel, and bolting bearer tokens on then is more work than just shipping them now.

**Reversibility: MEDIUM.** Adding mTLS *on top of* bearer tokens is additive (stronger mutual authentication for specific callers). Removing bearer tokens in favor of something else is a breaking change for every desk's `/run/secrets/drydock-token`.

## 6. Daemon location and supervision (addresses OQ#1)

**Decision.**
- macOS: launchd user agent at `~/Library/LaunchAgents/com.drydock.wsd.plist`.
- Linux: systemd user unit at `~/.config/systemd/user/wsd.service`.
- Both: `RestartOnFailure` semantics, log to `~/.drydock/logs/wsd.log`.

**Not tmux.** Daemon lifecycle shouldn't depend on a user's terminal session.

**Survives reboot.** User-level services, not root. Starts on login. If the user isn't logged in, no daemon — fine for V2 (solo user). Promote to a system service if an always-on headless host genuinely needs the daemon up regardless of login session.

**Configuration** at `~/.drydock/wsd.toml`:
```toml
socket_path = "~/.drydock/wsd.sock"
http_listen = "100.x.y.z:9090"     # Tailscale IP; omit to disable HTTP transport
log_level = "info"
secrets_backend = "file"            # v2-design-capability-broker.md
```

## 7. `ws attach` routing (addresses OQ#3)

**Decision.** `ws attach` stays direct — host CLI opens an editor (VS Code, Cursor) connected to the desk's container via the existing `vscode-remote+dev-container://` URL pattern. Daemon is NOT in the path.

**Why.** `attach` is a client-UI concern: it opens a local editor at the right vscode-remote URI. Nothing policy-relevant happens. Routing through the daemon adds latency and an authentication step to a read-only operation.

**Caveat: in-desk `ws attach` (desk → other desk).** Not supported in V2. If a use case emerges, it would need to go through the daemon (the source desk is authenticated; the daemon validates that source can reach target). Defer until pressure exists.

## 8. Error surface

All errors return JSON with this shape (aligns with `WsError.to_dict()` bimodal shape on current main — see current-state notes):
```json
{
  "error": "workspace_already_running",
  "message": "Desk 'myapp' is already running",
  "fix": "ws create myapp --force",
  "request_id": "018f..."
}
```

`error` is a stable machine-readable code. `message` is human-readable. `fix` is the suggested recovery command, treated as a stable contract per the CLAUDE.md test discipline (tests exist to prevent fix-string regressions).

**Codes V2 adds** (non-exhaustive): `desk_exists`, `desk_not_found`, `parent_not_found`, `policy_violation`, `narrowness_violated`, `capability_unsupported`, `lease_expired`, `daemon_unavailable`, `invalid_request_id`.

## 9. Reversibility audit

| Decision | Cost | Notes |
|---|---|---|
| JSON-RPC 2.0 envelope | Medium | Wire format churn is expensive; debuggability wins over alternatives |
| Unix socket only (HTTP deferred) | Low | Adding HTTP later reuses the same envelope; no client churn |
| `CreateDesk`/`SpawnChild` as distinct methods | Medium | Once clients, audit, and tests depend on the split (host-admin vs. desk-authenticated policy-validated authority), reshaping is expensive. Codex flagged the initial `Low` rating as understated |
| `RequestCapability` as the single lease entry point | Medium | Generalization is the point; splitting into type-specific methods later means RPC churn |
| `RequestCapability` subject derived from token, not arg | Medium | Changing the subject-derivation rule (e.g., permitting admin impersonation without a separate method) is a trust-model change, not a refactor |
| Bearer tokens (vs. mTLS) | Medium | Adding mTLS is additive; removing bearer tokens is breaking |
| Token subject model (`token → desk_id`, not `→ (principal_id, desk_id)`) | Medium | Multi-user sketch reserves the extension; formally adding a principal later is additive but every audit consumer and policy-keyed store has to participate |
| Daemon lives in launchd/systemd user service | Low | Can promote to system service later without API change |
| `ws attach` bypasses daemon | Low | Can route through daemon later by flipping a single call site |
| Task-log-based crash recovery | Low | Internal implementation; schema additive |
