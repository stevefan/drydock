# Migration Vision — Archived 2026-04-17

**Status.** Archived. No longer a design target. This doc preserves the coherent V3 migration story as it stood at end-of-V2-design so the thinking isn't lost, in case the pivot reverses.

## Why archived

Drydock pivoted from "portable desks across a host fleet" to "always-on agent fabric pinned to a durable host." The pivot weighed V2's daily serializability discipline against V3 migration as a capability that might never ship.

Verdict on 2026-04-17: the discipline costs more than the feature is worth for a 10-50 desk personal fleet whose actual usage pattern is "always on one host, attach from anywhere via tailnet." Hardware refresh — the one universal forcing function — is adequately served by a rebuild-from-config procedure (yaml + registry dump + worktree branches). A bad afternoon every few years is a fair price for zero daily architectural cost.

## What was dropped

- `ws migrate myapp laptop→cloud` primitive — suspend, serialize, transfer, resume
- Fleet-aware daemon — coordinated multi-host placement, lead election
- Cross-host identity continuity — same Tailscale hostname / audit principal / `DRYDOCK_WORKSPACE_ID` after a move
- Suspend/resume as a Drydock primitive (Docker Desktop's VM suspension already covers laptop-sleep on a single host)
- **The "desk state must be serializable" V2 architectural commitment** — the load-bearing daily discipline that enabled migration
- `drydock-base` image as a migration-correctness gate (the image itself is still useful for deduplication across project devcontainers; just not load-bearing for portability)

## What replaced it

- **"Always-on host" framing.** Desks are durable on *this host*, not across the fleet. Pick one always-on host (home server, M-series mini, cloud VM), run `wsd` there, attach from anywhere via Tailscale.
- **Thin multi-host as a quiet extension.** Nothing prevents a second host from running its own `wsd`; a `ws` CLI on any host can target a remote daemon's socket over tailnet. No coordination between daemons. No shared registry. No placement logic. Each desk pinned to the daemon that created it.
- **Hardware refresh = rebuild, not migrate.** `~/.drydock/projects/*.yaml` + a registry dump + worktree branch list is enough to re-provision on a new host. Not zero-downtime; not seamless. Bounded and manual, invoked once every few years.

## The migration vision, preserved verbatim

The text below is carried over from the docs at the moment of the pivot. Paragraphs pulled from `vision.md`, `v2-scope.md`, and `v2-design-state.md`. Not edited except to remove cross-references that would break.

### From vision.md — "The host fleet" (item 6 of the fabric properties)

> **The host fleet.** Workspaces run on a dynamic set of machines: laptop, home server, cloud VM. Placement follows resource availability, persistence needs, data locality. You say `ws create myapp`; you don't say where.

### From vision.md — item 1 (the suspend/resume line)

> Suspend on your laptop, resume on your home server; the worktree, session state, and in-flight tasks move with it.

### From v2-scope.md — V3 preview

> ## What V3 adds (preview)
>
> V3 takes the agent-desk further: desks become **mobile**. The architectural bet of V2 — desks are serializable daemon entities — pays off in V3 as actual portability.
>
> - **Migration primitive.** `ws migrate myapp laptop→cloud`. Suspend on source, serialize state, transfer, deserialize, resume on destination.
> - **Fleet-aware daemon.** Multiple hosts running `wsd`, coordinated. Placement decisions driven by policy (prefer cloud for heavy compute, prefer interactive host for desks you're currently attached to).
> - **Identity continuity across hosts.** Same Tailscale hostname, same audit principal, same `DRYDOCK_WORKSPACE_ID` across host changes.
> - **`drydock-base` image.** Published base image so project devcontainers `FROM` it. Migration between hosts requires base-template consistency; duplication across project-owned devcontainers is tolerable in V2, unacceptable in V3.
> - **Suspend/resume as a first-class primitive** (because it's the same operation as migration, just without the transfer step).
>
> The user-facing outcome V3 targets: *seamless remote development*. You work on a desk from your laptop; you close the lid, walk to the lab; overnight, heavy work keeps going on a cloud VM; next morning, the desk has migrated back to your laptop and in-flight work is exactly where you left it. The desk is the stable thing; the host is implementation detail.

### From v2-scope.md — the serializability commitment

> The architectural commitment V2 makes, to unblock V3 later: **desk state must be serializable.** What lives in the daemon's ownership (registry, overlay, secrets broker leases) has portable representations; what lives in the container is either derived from that state (rebuildable from devcontainer + worktree) or volume-mounted to host-owned paths (session files, bash history, tool caches). V2 gets this discipline right from day one, even without implementing migration itself.

### From v2-design-state.md — §2 "Serializability for V3 (forward-compat)"

> V2 doesn't migrate, but every piece of state must be serializable so V3 can:
>
> | Property | How V2 enforces |
> |---|---|
> | No host-specific absolute paths in registry | All paths constructed from `ws_id` + host prefix at runtime; registry stores relative paths where possible |
> | No host-clock-relative timestamps | All timestamps UTC-absolute; lease `expiry` is absolute instant, never "5 minutes from now" at persistence |
> | No host-specific tokens in container | Token is opaque; re-issue on migration (source host's tokens invalidated on migrate-out) |
> | Leases portable | `issuer` field lets V3 track which host issued; on migration, issuer rewrites to destination host |
> | Container state recoverable | Anything in container-local filesystem that mustn't be lost is volume-mounted to `~/.drydock/...` (host-owned, serializable) |
>
> Practical V3 migration is `tar ~/.drydock/{worktrees,overlays,secrets,leases}/<ws_id>/` + registry row export + tokens re-issued on destination. V2 doesn't implement this; V2 just doesn't preclude it.
>
> **Reversibility: HIGH** on "no host-specific state in container." Breaking this invariant by V2 means V3 is either a rewrite or a buggy migrator. Guarded by CI lint: registry-write helpers reject absolute paths that aren't under `~/.drydock/`.

### From v2-design-state.md — §1 host filesystem paths footnote

> **All paths are addressable by `ws_id`** so V3 migration is a tar-and-ship of owned paths + DB export.

### From v2-design-capability-broker.md — lease issuer field

> `issuer` reserved for V3 fleet federation (`wsd@hostname`). V2 always emits `"wsd"`.

## If this pivot ever reverses

Bringing migration back would require:

1. **Audit every registry-touching code path.** Host-absolute paths may have crept in once the CI lint guarding the serializability invariant was removed. Expect a nontrivial cleanup pass.
2. **Secrets broker re-tightening.** Post-pivot, V2 likely ships static `/run/secrets/` mounts populated by the daemon at create time. Finite-TTL leases + rotation would need to be added back — the Phase 4 capability-broker shape from the secrets roadmap is the template.
3. **drydock-base discipline.** Resume the requirement that all hosts run a consistent base image tag for a given desk's lifetime; add version negotiation on migration.
4. **Session state audit.** Anything that drifted into container layers since the pivot (caches, WAL files, shell history) needs to move to host-mounted paths.
5. **The V3 features themselves.** The migration primitive, fleet daemon, identity continuity, suspend/resume — all still need to be built.

The trigger to revisit: an actual forcing function. E.g., a heavy workload that only makes sense on a cloud GPU plus an interactive workload that only makes sense on a local machine, tied by the same desk identity. Until something real pushes on it, the always-on framing wins.

## Cross-references at time of archive

- `v2-scope.md` — primary doc where V3 preview lived
- `vision.md` — fabric framing including "host fleet" property
- `v2-design-state.md` §2 — serializability rules
- `v2-design-capability-broker.md` — lease model with `issuer` field reserved
- `v2-design-protocol.md` — HTTP/Tailscale transport (also reserved-for-V3 at time of pivot)
- `changelog.md` — "What got designed but not built (roadmap)" entry for V3 fleet-awareness
