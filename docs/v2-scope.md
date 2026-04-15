# Drydock V2 — daemon + agent-desks

## The frame: agent-desks as first-class entities

An **agent-desk** is a durable, addressable place where an agent works. It has a name, an occupant (usually Claude), a policy scope, accumulated state (worktree, session, shell history, tools), and a stable identity that outlives its current container. You create a desk once; you never re-create it.

V1 treats workspaces as container-plus-worktree: useful, but the *container* is where identity effectively lives. When the container goes, so does the in-flight work, the session, the shell state. V2 inverts this — the *desk* is the first-class thing, owned by the daemon; containers are its current embodiment.

Nested spawning — the forcing function for V2 — is one consequence of this inversion: if desks are daemon entities, then an occupying agent can ask the daemon for a sibling desk without Drydock violating its own layering principles. Migration (V3) is another: if state belongs to the desk rather than to the container, the desk can move.

This document scopes **V2: single-host daemon, desk-as-first-class, policy + nested spawn + audit**. Migration itself is V3 — but V2's interfaces must not preclude it.

## What V2 delivers

- **`wsd` daemon** as sole control plane for every lifecycle operation. The `ws` CLI becomes one client; Claude Code in a desk is another.
- **Desks are daemon entities.** State lives in registry and well-known host paths owned by the daemon, not in container-private layers.
- **Policy graph with enforced narrowness.** Children are strictly narrower than parents — firewall, secrets, capabilities.
- **Nested spawn via the daemon.** A desk-occupant asks the daemon to spawn a child; the daemon validates policy and dispatches.
- **Audit log.** Every daemon action recorded with principal + operation + policy check result.
- **Bearer-token auth over Tailscale.** Desks receive a token at creation; they present it when talking to the daemon.

## What V2 explicitly does *not* do (belongs in V3)

- **Migration.** `ws migrate myapp laptop→cloud` is V3.
- **Suspend/resume.** The foundation for migration. On a single host, Docker Desktop's VM suspension already covers laptop close/open. Building Drydock-level suspend/resume before there's a reason (migration) is ceremony.
- **Fleet-aware daemon.** Multi-host coordination, placement decisions, lead election.
- **`drydock-base` image.** Publishing a base image so project devcontainers can `FROM ghcr.io/.../drydock-base`. Only becomes load-bearing when desks migrate between hosts with potentially different base-template versions.
- **Cross-host identity continuity** (same Tailscale hostname across hosts, audit principal that follows the desk).

All of these are V3, and the V3 design starts from whatever V2 ships. The architectural commitment V2 makes, to unblock V3 later: **desk state must be serializable.** What lives in the daemon's ownership (registry, overlay, secrets broker leases) has portable representations; what lives in the container is either derived from that state (rebuildable from devcontainer + worktree) or volume-mounted to host-owned paths (session files, bash history, tool caches). V2 gets this discipline right from day one, even without implementing migration itself.

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
- **Serializable state, even before migration.** The daemon owns desk state in ways that can later transfer between hosts, even though V2 doesn't transfer anything.

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
| Secrets broker | Issues time-bounded credential leases to desks. Replaces V1's static `/run/secrets` directory with daemon-mediated provisioning. Migration-ready: re-leasing on a new host is cleaner than copying files. |
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
- Unix socket peer credentials — only works single-host, which V3 wants to move beyond.

Revisit if the token model stresses.

## Policy validation

Before spawning a child, the daemon verifies:

1. **Firewall narrowness.** `child.firewall_extra_domains ⊆ parent.delegatable_firewall_domains`. A child cannot reach a domain the parent cannot delegate.
2. **Secret narrowness.** `child.secrets ⊆ parent.delegatable_secrets`. Parent declares which of its secrets it may delegate; children request a subset.
3. **Capability narrowness.** `child.capabilities ⊆ parent.capabilities`. A parent that cannot spawn grandchildren cannot grant that authority either.
4. **Resource limits.** Parent has a budget (child count, total CPU/memory); spawning debits it.

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

## What V3 adds (preview)

V3 takes the agent-desk further: desks become **mobile**. The architectural bet of V2 — desks are serializable daemon entities — pays off in V3 as actual portability.

- **Migration primitive.** `ws migrate myapp laptop→cloud`. Suspend on source, serialize state, transfer, deserialize, resume on destination.
- **Fleet-aware daemon.** Multiple hosts running `wsd`, coordinated. Placement decisions driven by policy (prefer cloud for heavy compute, prefer interactive host for desks you're currently attached to).
- **Identity continuity across hosts.** Same Tailscale hostname, same audit principal, same `DRYDOCK_WORKSPACE_ID` across host changes.
- **`drydock-base` image.** Published base image so project devcontainers `FROM` it. Migration between hosts requires base-template consistency; duplication across project-owned devcontainers is tolerable in V2, unacceptable in V3.
- **Suspend/resume as a first-class primitive** (because it's the same operation as migration, just without the transfer step).

The user-facing outcome V3 targets: *seamless remote development*. You work on a desk from your laptop; you close the lid, walk to the lab; overnight, heavy work keeps going on a cloud VM; next morning, the desk has migrated back to your laptop and in-flight work is exactly where you left it. The desk is the stable thing; the host is implementation detail.
