# Drydock V2 — daemon + agent-desks

## The frame: agent-desks as first-class entities

An **agent-desk** is a durable, addressable place where an agent works. It has a name, an occupant (usually Claude), a policy scope, accumulated state (worktree, session, shell history, tools), and a stable identity that outlives its current container. You create a desk once; you never re-create it.

V1 treats workspaces as container-plus-worktree: useful, but the *container* is where identity effectively lives. When the container goes, so does the in-flight work, the session, the shell state. V2 inverts this — the *desk* is the first-class thing, owned by the daemon; containers are its current embodiment.

Nested spawning — the forcing function for V2 — is the primary consequence: if desks are daemon entities, then an occupying agent can ask the daemon for a sibling desk without Drydock violating its own layering principles. Container rebuild (`ws upgrade --force`, base-image bump, recovery after a crash) is another: when the container is rebuilt, the desk — registry row, worktree, named volumes, tokens — survives intact.

This document scopes **V2: single-host daemon, desk-as-first-class, policy + nested spawn + audit + tailnet identity lifecycle**. Desks are durable on their chosen host; cross-host migration is not a goal (see `_archive/migration-vision.md` for the archived vision and why it was dropped).

## What V2 delivers

- **`wsd` daemon** as sole control plane for every lifecycle operation. The `ws` CLI becomes one client; Claude Code in a desk is another.
- **Desks are daemon entities.** Daemon-authoritative state (policy, entitlements, tokens, parent-child links, audit) lives in the registry and well-known host paths. Project-level container state (caches, `.venv`s, shell history, SQLite WAL files) can live wherever is natural; no daemon correctness property requires banishing it.
- **Policy graph with enforced narrowness.** Children are strictly narrower than parents — firewall, secrets, capabilities.
- **Nested spawn via the daemon.** A desk-occupant asks the daemon to spawn a child; the daemon validates policy and dispatches.
- **Audit log.** Every daemon action recorded with principal + operation + policy check result.
- **Bearer-token auth over Tailscale.** Desks receive a token at creation; they present it when talking to the daemon.
- **Tailnet identity lifecycle.** Daemon authoritatively cleans up the tailnet device record on `DestroyDesk` (closes a v1 gap where `tailscale logout` releases node-side auth but leaves the device record in the tailnet admin). Admin RPC `PruneStaleTailnetDevices` for batch cleanup of orphans. Daemon-level admin token, not a per-desk capability — see [v2-design-tailnet-identity.md](v2-design-tailnet-identity.md).

## What V2 explicitly does *not* do

- **Cross-host migration, fleet daemon, suspend/resume, cross-host identity continuity.** All archived in `_archive/migration-vision.md`. Desks are pinned to the host that creates them; hardware refresh is a rebuild-from-config procedure.
- **Thin multi-host coordination.** Nothing stops two hosts each running their own `wsd` with their own desks, and a `ws` CLI on either host can target the other's socket over tailnet if configured. Not a V2 focus; no shared registry, no placement decisions.

The architectural commitment V2 makes: **desk state is rebuildable from yaml + registry + worktree.** Project yaml + `~/.drydock/projects/*.yaml` + a registry dump + worktree branch names are enough to re-provision a desk on a fresh host in a bounded manual procedure. This is hardware-refresh insurance, not migration. Container-private state (caches, `.venv`s, SQLite WAL files, shell history) can live wherever is natural — Docker volumes, container layers, or host-mounted paths as the project chooses; none of it needs to be portable between hosts.

## Forcing function: heterogeneous monorepos

The concrete case that forces V2 is a monorepo whose sub-projects have isolation needs that differ by an order of magnitude. A representative shape:

| Sub-project | Stack | Isolation need |
|---|---|---|
| `core-lib` | Pure compute | Low — no network, no untrusted input |
| `cli-tool` | Pure compute | Low — no network, no untrusted input |
| `web-app` | Node / web | Medium — dev server, browser |
| `scraper` | Browser automation | **High** — pulls untrusted HTML from arbitrary external hosts |
| `dataset` | Config / data | N/A — data only |

The requirement: a top-level agent in the monorepo desk that can edit across all sub-projects, spawn narrower child desks per high-isolation sub-project, and confine the risky ones (`scraper`) so a compromised page can't reach anything but the hosts that sub-project legitimately needs.

The wrong mechanism is mounting the Docker socket into the parent container — that collapses the blast radius to the whole fleet and contradicts Drydock's default-deny posture. The right mechanism is the daemon: parent asks permission, daemon validates policy, daemon dispatches spawn.

Homogeneous repos (all sub-projects with similar isolation needs) don't exercise this axis. The heterogeneous case is where the design first breaks or generalizes.

## Principles

- **Layering.** Tools that manage infrastructure live outside the thing they manage. Desks stay ignorant of Docker; the daemon mediates every operation.
- **Narrowness.** A desk cannot grant a child any capability it does not hold. Compromise of any single desk cannot laterally expand authority.
- **Explicit capability grants.** No desk is "permissionless." Each gets enumerated capabilities; spawning is one of them. Most desks don't have it.
- **Host mode still works.** The `ws` CLI invoked on the host continues as today. The daemon is plumbing for the desk-occupant case, not a replacement for local use.
- **Rebuildable state.** The daemon owns desk state in ways that survive container rebuilds and support a manual rebuild-from-config procedure on a fresh host. Not: transparent migration between hosts — that's archived.

## Architecture

```
Host
├── ws CLI (host mode)                          direct → registry + devcontainer CLI
├── wsd daemon                                  HTTP/Unix socket, authenticated
├── registry.db (+ parent-child columns, + desk state)
├── secrets broker (leases, not static mounts)
└── Docker
       │
       │  ws create myapp   (from host)
       ▼
  ┌──────────────────────────────────────┐
  │ Desk "myapp"                          │
  │                                       │
  │   ws CLI (desk mode)                  │
  │     │ detects DRYDOCK_WORKSPACE_ID    │
  │     │ routes to wsd via Tailscale     │
  │     ▼                                 │
  │   HTTP → wsd → policy check → spawn   │
  │           scraper child               │
  └──────────────────────────────────────┘
```

## Components

| Component | Role |
|---|---|
| `ws` CLI — host mode | Unchanged from V1. Writes registry directly, calls devcontainer CLI directly. |
| `ws` CLI — desk mode | Detects `$DRYDOCK_WORKSPACE_ID` env, routes commands to daemon over HTTP. |
| `wsd` daemon | Enforces policy, writes to registry, invokes devcontainer CLI on behalf of authorized callers. |
| Desk state (registry columns) | `parent_workspace_id`, `delegatable_firewall_domains`, `delegatable_secrets`, `capabilities`, plus everything V1 already tracks. |
| Policy validator | Pure function; given a parent's declared policy and a child's requested policy, returns `allow` or `reject with reason`. Extensively tested; this is the trust boundary. |
| Secrets broker | Issues capability leases to desks. Replaces V1's static `/run/secrets` directory with daemon-mediated provisioning — policy-enforced entitlement checks, per-desk isolation, revocation on destroy. |
| Audit log | `~/.drydock/audit.log`. Every daemon operation with timestamp, principal, policy check result. |

## Protocol sketch

Desk → daemon, authenticated:

```
POST   /v2/desks                 — ws create (child)
GET    /v2/desks?parent=<id>     — ws list (children of parent)
POST   /v2/desks/<name>/stop
DELETE /v2/desks/<name>
GET    /v2/desks/<name>/secrets  — request scoped secret leases
```

Each request carries the caller's desk id and an auth token. The daemon looks up the caller's capabilities before dispatching.

## Auth — bearer tokens on Tailscale

Bearer token per desk, issued at `ws create`, mounted at `/run/secrets/drydock-token`. Daemon maps token → desk id, looks up capabilities, dispatches.

Rejected alternatives:
- mTLS via Tailscale device identity — stronger, but couples V2 to Tailscale beyond the transport layer.
- Unix socket peer credentials — only works single-host-single-daemon; a bearer token generalizes cleanly if a thin multi-host case ever surfaces.

Revisit if the token model stresses.

## Policy validation

Before spawning a child, the daemon verifies:

1. **Firewall narrowness.** `child.firewall_extra_domains ⊆ parent.delegatable_firewall_domains`. A child cannot reach a domain the parent cannot delegate.
2. **Secret narrowness.** `child.secrets ⊆ parent.delegatable_secrets`. Parent declares which of its secrets it may delegate; children request a subset.
3. **Capability narrowness.** `child.capabilities ⊆ parent.capabilities`. A parent that cannot spawn grandchildren cannot grant that authority either.
4. **Mount narrowness.** `child.extra_mounts ⊆ parent.extra_mounts`. A child cannot receive a mount the parent itself doesn't have.

*(Resource budgets — child count, CPU/memory caps debited on spawn — were sketched here originally but deferred per the design-pass trim. V2's forcing function is one monorepo with a handful of children; budget caps are premature. See `v2-design-capability-broker.md` §4.)*

## Capability primitive: uniform across spawn and occupant

The narrowness validator above describes one case — parent desk granting a child desk a subset of its authority. The same primitive applies to a second case: a desk granting its *occupant* a narrowed subset for a single operation or session.

Concrete example: a scheduled "smart operator" agent inside a scraper desk may be permitted to edit `sites/` (adapt to site-layout changes) but not `firewall-extras.yaml`, not `secrets/`, not `git push`. That's exactly a narrowness grant — holder = desk's full authority, requester = the occupant's bounded action set.

Parameterize the validator over `holder` and `requester` rather than hardcoding `parent → child desk`. Both cases then use the same code path, the same audit records, and the same reasoning about narrowness. The alternative — a separate in-desk scoping mechanism bolted on later — accumulates divergent edge-case behavior and a muddier audit story.

## Registry schema additions

New columns on `workspaces`:

| Column | Type | Purpose |
|---|---|---|
| `parent_workspace_id` | TEXT NULL | null for host-created; set for daemon-spawned children |
| `delegatable_firewall_domains` | TEXT (JSON list) | domains this desk may grant to children |
| `delegatable_secrets` | TEXT (JSON list) | secret keys this desk may delegate |
| `capabilities` | TEXT (JSON list) | explicit capability grants (`spawn_children`, etc.) |

`destroy` cascades: destroying a parent destroys its children first.

## CLI routing logic

```python
if os.environ.get("DRYDOCK_WORKSPACE_ID"):
    daemon_client.dispatch(sys.argv)   # desk mode — ask wsd
else:
    main_cli()                         # host mode — direct as today
```

`DRYDOCK_WORKSPACE_ID` is already in the overlay (V1 work). `--parent` flag on `ws create` becomes the nesting affordance.

## Open questions

1. Where does the daemon live — launchd service, tmux pane, something else? How does it survive reboot?
2. How does the daemon handle devcontainer CLI errors — retry, propagate, mark desk as error?
3. Does `ws attach` (host → desk) route through the daemon, or stay direct?
4. Capability revocation: if a parent's policy changes, do running children get re-evaluated?
5. How does the secrets broker integrate with external sources (1Password, vault, cloud secret managers)? Plugin interface? Hardcoded adapters?

## Migration from V1

- V1 desks (no `parent_workspace_id`) keep working unchanged — host-spawned, daemon ignores them.
- New `ws` version ships with CLI routing logic but defaults to host mode unless the daemon is configured.
- The daemon is opt-in; if `wsd` never starts, V1 behavior is preserved.
- Existing V1 registry is upgraded in place (new columns default to null / empty).

## When to build V2

When the nested case becomes painful enough that spawning children from the host feels wrong. A heterogeneous monorepo can run today in V1 mode (each desk host-spawned); pain surfaces when a top-level agent wants to spawn narrower children and can't.

## The migration vision — archived

An earlier trajectory had V3 making desks **mobile** across hosts: `ws migrate laptop→cloud`, fleet-aware daemon, identity continuity, published `drydock-base` as a migration-correctness gate. Dropped on 2026-04-17 in favor of "always-on durability on a chosen host" — see `_archive/migration-vision.md` for the full preserved vision and the reasoning behind the pivot.

Post-pivot, `drydock-base` remains useful as a deduplication pattern across project devcontainers, but is no longer load-bearing for any correctness property.
