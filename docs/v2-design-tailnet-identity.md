# V2 Design — Tailnet Identity Lifecycle

**Purpose.** Pin the daemon's responsibility for **tailnet device records** as part of desk lifecycle. Today drydock owns per-node tailnet auth (`tailscale up --authkey`) and per-node logout (`tailscale logout`) but **not** the device record on Tailscale's control plane. Logging a node out doesn't delete its device record — that requires an explicit Tailscale API call. V2's daemon, as authoritative owner of desk lifecycle, closes this loop.

Extends `v2-scope.md` — adds tailnet identity cleanup to the V2 deliverables list.
Extends `v2-design-state.md` §1a — adds two audit events to the schema.

---

## 1. The gap

Tailscale's `tailscale logout` releases the node-side auth state. It does **not** delete the device record from the tailnet admin. Per Tailscale's design, the device remains as an "offline" record indefinitely until explicitly removed via the admin UI or the Tailscale API.

Concrete symptom (observed 2026-04-14): the Mac auction-crawl desk was `ws stop`'d, drydock called `tailscale logout` correctly, but the `auction-crawl` device record remained in the tailnet admin. When a new desk on Hetzner came up with the same configured hostname, Tailscale auto-renamed it to `auction-crawl-1`. Identity continuity broken; canonical hostname lost.

Drydock owns desk lifecycle in V2. Tailnet identity IS desk identity (the per-node hostname IS the audit principal in tailnet logs, the SSH target, the firewall identity). Letting it linger contradicts the "desks as durable addressable places" promise.

## 2. Scope

V2 adds:

- **Daemon-side tailnet device deletion** as part of `DestroyDesk` cleanup (and cascaded child destroys).
- **Daemon-level admin credential** (Tailscale API token), stored as daemon-internal infrastructure — NOT a per-desk capability.
- **Two new audit events** (`tailnet.device_deleted`, `tailnet.device_delete_failed`) extending the schema in `v2-design-state.md` §1a.
- **One admin RPC** (`PruneStaleTailnetDevices`) for batch cleanup of orphaned records.
- **V1 coexistence**: host-mode `ws destroy` calls the same primitive when daemon is configured with a token; behavior unchanged when no token is configured.

V2 does **not** add:

- Tailnet ACL management (separate concern, multi-host policy surface).
- Per-desk Tailscale-side device authorization workflows.
- A `TAILNET_DEVICE` capability type. See §3 for why this is the wrong abstraction.

## 3. Why the API token is daemon-level, not a capability

The admin token grants "delete any device on this tailnet." No desk should hold that authority — it's a fleet-wide sysadmin credential. Modeling it as a `CapabilityType.TAILNET_ADMIN` lease (per the capability-broker shape) would either:

(a) require every desk to hold the lease — wrong, blast-radius explosion, and
(b) require special-casing to ensure no desk holds it — exactly the "scoped entitlements that aren't in `DeskPolicy`" anti-pattern Codex flagged in the rule-5 review.

The cleaner framing: the token is **daemon-internal infrastructure**, like the daemon's own SQLite database access. The daemon uses it on behalf of the lifecycle operations it owns; no desk-facing API exposes it.

## 4. Credential storage

| Field | Value |
|---|---|
| Path | `~/.drydock/daemon-secrets/tailscale_admin_token` |
| Mode | `0400`, owned by daemon uid |
| Required scope | `devices` (Tailscale API key generation UI) |
| Tailnet identifier | Configured in `wsd.toml` (`tailnet = "..."`); not fetched dynamically |
| Absence behavior | Non-fatal. Daemon logs `warning: no tailnet admin token configured; device records will not be auto-deleted on destroy`. Behavior matches V1 (logout only, record persists). |

Rotation is operator-driven; no V2 daemon-side rotation logic. (Revisit when the secrets-broker plugin pattern can lease from external sources — capability broker §7.)

## 5. Lifecycle integration

### 5.1 `DestroyDesk` (and cascaded child destroys)

Pseudocode:

```
def destroy_desk(desk_id):
    desk = registry.get_desk(desk_id)
    children = registry.get_children(desk_id)
    for child in children:
        destroy_desk(child.id)  # cascaded; emits its own audit
    devc.tailnet_logout(desk.container_id)   # existing v1 behavior
    devc.stop(desk.container_id)
    devc.remove(desk.container_id)
    checkout.remove(desk.worktree_path)
    registry.delete(desk_id)
    audit.emit("desk.destroyed", {desk_id, cascaded_children: [c.id for c in children]})
    if tailnet_admin_token_present():
        try:
            device_id = tailnet.find_device_by_hostname(desk.tailscale_hostname or desk.id)
            tailnet.delete_device(device_id)
            audit.emit("tailnet.device_deleted", {desk_id, hostname, device_id})
        except TailnetApiError as e:
            audit.emit("tailnet.device_delete_failed", {desk_id, hostname, device_id, error: str(e)})
            log.warning("tailnet device delete failed for %s: %s", desk_id, e)
            # Destroy still succeeds — daemon-side teardown completed.
```

**Failure semantics**: best-effort. Tailnet API failure does not roll back the destroy. The desk is gone from drydock's authoritative state; the orphaned tailnet record is recoverable via `PruneStaleTailnetDevices`.

### 5.2 `CreateDesk` — optional device-id caching (defer to V2.1)

V2.0: don't cache device ID. The destroy-time `find_device_by_hostname` lookup is one Tailscale API call; cheap.

V2.1+: optionally cache `tailnet_device_id` in the `desks` table after `tailscale up` succeeds. Avoids the lookup, but adds registry state and a write-after-up-succeeds step. Not load-bearing for V2.

### 5.3 `PruneStaleTailnetDevices` (admin RPC)

```
PruneStaleTailnetDevices {
  dry_run: bool = true,         # default true; --apply to delete
  hostname_pattern: str = ".*"  # optional regex filter
} → {
  candidates: [
    {
      device_id: str,
      hostname: str,
      last_seen: datetime,
      would_delete: bool,
      reason: str  # "no_matching_desk" | "desk_destroyed" | "host_pattern_match"
    }
  ],
  deleted: [device_id]  # empty if dry_run
}
```

Logic: enumerate `GET /api/v2/tailnet/{tailnet}/devices`, match each against `desks` registry by hostname, mark for deletion any whose hostname matches the drydock pattern but doesn't correspond to a live desk.

CLI: `ws tailnet prune` (alias of dry-run); `ws tailnet prune --apply`. Both list candidates first, before action.

Useful after: force-removed containers, daemon crashes mid-destroy, V1→V2 migration via `ws adopt` (which doesn't retroactively prune V1-era ghosts).

## 6. Audit events (extends `v2-design-state.md` §1a)

| Event | Emitted on | Required `details` keys |
|---|---|---|
| `tailnet.device_deleted` | Successful API DELETE during `DestroyDesk` cleanup or `PruneStaleTailnetDevices` | `desk_id` (nullable for prune of orphan), `hostname`, `device_id` |
| `tailnet.device_delete_failed` | API DELETE returns non-2xx or connection fails | `desk_id` (nullable), `hostname`, `device_id` (nullable if resolve failed), `error` (string excerpt) |

Both extend the existing audit framework. No new audit primitives. The `result` field of the event envelope is `"ok"` for `tailnet.device_deleted` and `"error"` for `tailnet.device_delete_failed`.

## 7. V1 coexistence

| Mode | Behavior |
|---|---|
| Pure V1 (no daemon) | `ws destroy` → `tailscale logout` → tailnet record persists. Same as today. |
| V2 daemon + token configured | `ws destroy` (host-mode CLI routes to daemon) → daemon executes destroy → tailnet device deleted. |
| V2 daemon, no token configured | Same as V1: logout but no device delete. Daemon logs the absence at startup. |
| V1 → V2 migration | Per the V2 on-ramp (`destroy + create` — see overview's Decisions deferred), V1 desks are destroyed and re-created. The destroy step under V2 deletes the tailnet record. V1-era device records orphaned by previous force-removed containers are cleaned up via `ws tailnet prune --apply`. |

## 8. V1.x backport path

The `delete_tailnet_device(hostname, tailnet, api_token)` function is self-contained — no daemon dependency. V1.x can ship it standalone:

- New module `src/drydock/core/tailnet.py`.
- `cli/destroy.py` calls it after the existing `tailnet_logout`, when an admin token is present at `~/.drydock/daemon-secrets/tailscale_admin_token` (or `~/.drydock/tailscale-admin-token` for v1.x).
- New `cli/tailnet.py` wraps `ws tailnet prune`.

V2's daemon then calls the same `core/tailnet.py` function — different caller, same code path. Clean v1→v2 path; no rewrite when the daemon lands.

This backport is **scoped as a separate v1.x release**, not part of the v2 daemon work. V2's design just commits that the daemon will adopt the same primitive when it ships.

## 9. Reversibility

| Decision | Class | Notes |
|---|---|---|
| Daemon-level token (vs. capability-typed) | **Medium** | Reflects architectural commitment that this is daemon infrastructure, not user state. Reversing means moving to capability model after consumers depend on direct admin RPC; breaking. |
| Audit event names (`tailnet.device_deleted`, `tailnet.device_delete_failed`) | Medium | Stable string contracts per `v2-design-state.md` §1a commitment. |
| Admin RPC name + signature (`PruneStaleTailnetDevices`) | Medium | Additive method; rename breaking once clients depend. |
| Best-effort failure semantics (don't block destroy) | Low | Could tighten to fail-on-error later via opt-in flag. |
| Hostname-based device lookup at destroy (vs. cached device ID) | Medium | Switching to cached ID requires registry migration. V2.1 may add caching as an additive optimization. |
| `~/.drydock/daemon-secrets/` path for admin token | Low | Can move; just an installer convention. |

## 10. Open questions deferred

- **Multi-tailnet support**: a daemon handling desks across multiple tailnets. V2 assumes one tailnet per daemon, configured in `wsd.toml`. Revisit if the need surfaces.
- **Tailscale OAuth client tokens vs. user API tokens**: OAuth clients (auto-issued from a tailnet) avoid manual rotation. V2 accepts user API tokens; OAuth-client integration is a v2.1 ergonomic improvement, not architectural.
- **Reauth flow when admin token expires mid-destroy**: V2 logs and proceeds (best-effort). An alerting hook can be added later if this becomes noisy.

## Cross-references

- `v2-scope.md` — feature listed under "What V2 delivers".
- `v2-design-state.md` §1a — audit event schema extended with the two new events above.
- `v2-design-overview.md` — decision log entry recording the addition + Steven sign-off (2026-04-15).
- `v2-design-protocol.md` — `PruneStaleTailnetDevices` RPC method to be added to the surface enumeration.
- Drydock memory: `project_v2_tailnet_lifecycle.md`.
