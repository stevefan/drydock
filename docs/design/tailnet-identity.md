# Tailnet identity lifecycle

The daemon owns Tailscale device-record cleanup as part of drydock lifecycle. Drydock owns per-node tailnet auth (`tailscale up --authkey`) and per-node logout (`tailscale logout`), but logout doesn't delete the device record on Tailscale's control plane — that requires an explicit Tailscale API call. Without the daemon closing this loop, stale records accumulate and canonical hostnames get held by offline ghosts.

See [vocabulary.md](vocabulary.md) for Harbor / DryDock / Worker. See [persistence.md](persistence.md) for the audit event schema (`tailnet.device_deleted`, `tailnet.device_delete_failed`).

---

## 1. The gap

Tailscale's `tailscale logout` releases the node-side auth state. It does **not** delete the device record from the tailnet admin. Per Tailscale's design, the device remains as an "offline" record indefinitely until explicitly removed via the admin UI or the Tailscale API.

Concrete symptom (observed 2026-04-14): the Mac auction-crawl drydock was `ws stop`'d, drydock called `tailscale logout` correctly, but the `auction-crawl` device record remained in the tailnet admin. When a new drydock on Hetzner came up with the same configured hostname, Tailscale auto-renamed it to `auction-crawl-1`. Identity continuity broken; canonical hostname lost.

The daemon owns drydock lifecycle in V2. Tailnet identity IS drydock identity (the per-node hostname IS the audit principal in tailnet logs, the SSH target, the firewall identity). Letting it linger contradicts the "drydocks as durable addressable places" promise.

## 2. Scope

What ships:

- **Daemon-side tailnet device deletion** as part of `DestroyDesk` cleanup (and cascaded child destroys).
- **Daemon-level admin credential** (Tailscale API token), stored as daemon-internal infrastructure at `~/.drydock/daemon-secrets/` — not a per-drydock capability.
- **Two audit events** (`tailnet.device_deleted`, `tailnet.device_delete_failed`) in the schema — see [persistence.md](persistence.md).
- **One admin RPC** (`PruneStaleTailnetDevices`) for batch cleanup of orphaned records. CLI surface: `ws tailnet prune [--apply]`.
- **Harbor-mode parity**: Harbor-mode `ws destroy` calls the same primitive when the daemon is configured with a token; behavior unchanged when no token is configured.

Not in scope:

- Tailnet ACL management (separate concern, multi-Harbor policy surface).
- Per-drydock Tailscale-side device authorization workflows.
- A `TAILNET_DEVICE` capability type. See §3 for why this is the wrong abstraction.

## 3. Why the API token is daemon-level, not a capability

The admin token grants "delete any device on this tailnet." No drydock should hold that authority — it's a archipelago-wide sysadmin credential. Modeling it as a `CapabilityType.TAILNET_ADMIN` lease (per the capability-broker shape) would either:

(a) require every drydock to hold the lease — wrong, blast-radius explosion, and
(b) require special-casing to ensure no drydock holds it — exactly the "scoped entitlements that aren't in `DeskPolicy`" anti-pattern Codex flagged in the rule-5 review.

The cleaner framing: the token is **daemon-internal infrastructure**, like the daemon's own SQLite database access. The daemon uses it on behalf of the lifecycle operations it owns; no drydock-facing API exposes it.

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

**Failure semantics**: best-effort. Tailnet API failure does not roll back the destroy. The drydock is gone from Harbor's authoritative state; the orphaned tailnet record is recoverable via `PruneStaleTailnetDevices`.

### 5.2 `CreateDesk` — optional device-id caching (defer to V2.1)

V2.0: don't cache device ID. The destroy-time `find_device_by_hostname` lookup is one Tailscale API call; cheap.

V2.1+: optionally cache `tailnet_device_id` in the `workspaces` table after `tailscale up` succeeds. Avoids the lookup, but adds registry state and a write-after-up-succeeds step. Not load-bearing for V2.

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

Logic: enumerate `GET /api/v2/tailnet/{tailnet}/devices`, match each against the `workspaces` registry by hostname, mark for deletion any whose hostname matches the drydock pattern but doesn't correspond to a live drydock.

CLI: `ws tailnet prune` (alias of dry-run); `ws tailnet prune --apply`. Both list candidates first, before action.

Useful after: force-removed containers, daemon crashes mid-destroy, stale records left by ad-hoc operations.

## 6. Audit events

Two events extend the schema in [persistence.md](persistence.md):

| Event | Emitted on | Required `details` keys |
|---|---|---|
| `tailnet.device_deleted` | Successful API DELETE during `DestroyDesk` cleanup or `PruneStaleTailnetDevices` | `desk_id` (nullable for prune of orphan), `hostname`, `device_id` |
| `tailnet.device_delete_failed` | API DELETE returns non-2xx or connection fails | `desk_id` (nullable), `hostname`, `device_id` (nullable if resolve failed), `error` (string excerpt) |

The `result` field of the event envelope is `"ok"` for deleted and `"error"` for delete_failed.

## 7. Operational modes

| Mode | Behavior |
|---|---|
| Daemon + token configured | `ws destroy` → daemon executes destroy → tailnet device deleted. |
| Daemon, no token configured | `ws destroy` → `tailscale logout`, tailnet record persists. Daemon logs the absence at startup. |

## 8. Open questions

- **Multi-tailnet support**: a daemon handling drydocks across multiple tailnets. Today assumes one tailnet per daemon, configured in `wsd.toml`. Revisit if the need surfaces.
- **Tailscale OAuth client tokens vs. user API tokens**: OAuth clients (auto-issued from a tailnet) avoid manual rotation. Today accepts user API tokens; OAuth-client integration is an ergonomic improvement, not architectural.
- **Reauth flow when admin token expires mid-destroy**: logs and proceeds (best-effort). Alerting hook addable if this becomes noisy.
- Drydock memory: `project_v2_tailnet_lifecycle.md`.
