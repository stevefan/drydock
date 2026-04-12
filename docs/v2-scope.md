# Drydock v2 — `ws` daemon for nested orchestration

## The problem

V1's control plane is a host-side CLI. That works when you're at the keyboard, and it's enough for microfoundry, substrate, and patchwork to each run as host-spawned workspaces. It fails the moment a Claude agent *inside* a workspace wants to spawn a sibling — for example, a microfoundry-root agent that wants to launch `auction-crawl` as a narrower child workspace to handle Playwright work without that authority bleeding back up.

The wrong answer is mounting the Docker socket into the parent container. That collapses the blast radius to the whole fleet: a compromised workspace owns every container on the host, regardless of policy. It also contradicts Drydock's default-deny network posture with a default-allow capability.

The right answer is a host-side `ws` daemon (`wsd`) that workspaces talk to over an authenticated channel. The daemon is the explicit orchestration surface; workspaces ask permission, the daemon enforces policy.

## Forcing function: microfoundry

Microfoundry is the first monorepo with heterogeneous sub-projects whose isolation needs differ by an order of magnitude:

| Sub-project | Stack | Isolation need |
|---|---|---|
| `microfluidics-dsl` | Python (CadQuery) | Low — pure compute |
| `fluid-cad` | Rust (PDE solver, Z3) | Low — pure compute |
| `mold-template` | Python (CadQuery) | Low — pure compute |
| `3duf` | Node / web app | Medium — dev server, browser |
| `auction-crawl` | Python + Playwright | **High** — browser automation hitting arbitrary auction sites, pulls untrusted HTML |
| `confocal-microscope` | Config files (Zeiss) | N/A — data only |

The requirement: a top-level Claude in the microfoundry workspace that can edit across all sub-projects, spawn narrower child workspaces per high-isolation sub-project, and confine the risky ones (`auction-crawl`) so a compromised listing can't reach anything but the whitelisted auction hosts.

Substrate and patchwork are more homogeneous and don't exercise this axis. If Drydock handles microfoundry well, the design generalizes; if not, microfoundry is where it breaks first.

## Principles

- **Layering.** Tools that manage infrastructure live outside the thing they manage. Workspaces stay ignorant of Docker; the daemon mediates.
- **Narrowness.** A parent workspace cannot grant a child any capability the parent does not already hold. Children are strictly narrower than parents. Compromise of any single workspace cannot laterally expand authority.
- **Explicit capability grants.** No workspace is "permissionless." Each gets enumerated capabilities; spawning is one of them.
- **Host mode still works.** The `ws` CLI invoked on the host continues to work as it does today. The daemon is additional plumbing for the nested case, not a replacement for local use.

## Architecture

```
Host
├── ws CLI (host mode)                          direct → registry + devcontainer CLI
├── wsd daemon                                  HTTP/Unix socket, authenticated
├── registry.db (+ parent-child columns)
└── Docker
       │
       │  ws create microfoundry   (from host)
       ▼
  ┌──────────────────────────────────────┐
  │ Workspace "microfoundry"              │
  │                                       │
  │   ws CLI (workspace mode)             │
  │     │ detects DRYDOCK_WORKSPACE_ID    │
  │     │ routes to wsd via Tailscale     │
  │     ▼                                 │
  │   HTTP → wsd → policy check → spawn   │
  │           auction-crawl child         │
  └──────────────────────────────────────┘
```

## Components

| Component | Role |
|---|---|
| `ws` CLI — host mode | Unchanged from v1. Writes registry directly, calls devcontainer CLI directly. |
| `ws` CLI — workspace mode | Detects `$DRYDOCK_WORKSPACE_ID` env, routes commands to daemon over HTTP. |
| `wsd` daemon | Enforces policy, writes to registry, invokes devcontainer CLI on behalf of authorized callers. |
| Parent-child registry | New columns: `parent_workspace_id`, `delegatable_firewall_domains`, `delegatable_secrets`, `capabilities`. |
| Policy validator | Pure function; given a parent's declared policy and a child's requested policy, returns `allow` or `reject with reason`. Extensively tested; this is the trust boundary. |

## Protocol sketch

Workspace → daemon, authenticated:

```
POST   /v2/workspaces                — ws create (child)
GET    /v2/workspaces?parent=<id>    — ws list (children of parent)
POST   /v2/workspaces/<name>/stop
DELETE /v2/workspaces/<name>
```

Each request carries the caller's workspace id and an auth token. The daemon looks up the caller's capabilities in the registry before dispatching.

## Policy validation

Before spawning a child, the daemon verifies:

1. **Firewall narrowness.** `child.firewall_extra_domains ⊆ parent.delegatable_firewall_domains`. A child cannot reach a domain the parent cannot delegate.
2. **Secret narrowness.** `child.secrets ⊆ parent.delegatable_secrets`. Parent declares which of its secrets it may delegate; children request a subset.
3. **Capability narrowness.** `child.capabilities ⊆ parent.capabilities`. A parent that cannot spawn grandchildren cannot grant that authority either.
4. **Resource limits.** Parent has a budget (child count, total CPU/memory); spawning debits it.

## Auth — bearer tokens on Tailscale

Bearer token per workspace, issued at `ws create`, mounted into the workspace at `/run/secrets/drydock-token`. The daemon maps token → workspace id, looks up capabilities, dispatches.

Rejected alternatives:
- mTLS via Tailscale device identity — stronger, but couples v2 to Tailscale beyond the transport layer.
- Unix socket peer credentials — only works single-host, which we want to keep open.

Revisit if the bearer-token model stresses.

## Registry schema additions

New columns on `workspaces`:

| Column | Type | Purpose |
|---|---|---|
| `parent_workspace_id` | TEXT NULL | null for host-created; set for daemon-spawned children |
| `delegatable_firewall_domains` | TEXT (JSON list) | domains this workspace may grant to children |
| `delegatable_secrets` | TEXT (JSON list) | secret keys this workspace may delegate |
| `capabilities` | TEXT (JSON list) | explicit capability grants (`spawn_children`, etc.) |

`destroy` cascades: destroying a parent destroys its children first.

## CLI routing logic

```python
if os.environ.get("DRYDOCK_WORKSPACE_ID"):
    daemon_client.dispatch(sys.argv)   # workspace mode — ask wsd
else:
    main_cli()                         # host mode — direct as today
```

`DRYDOCK_WORKSPACE_ID` is already in the overlay (v1 work). `--parent` flag on `ws create` becomes the nesting affordance.

## In scope

- `wsd` daemon (HTTP/Unix socket + bearer auth over Tailscale)
- CLI routing for in-workspace invocation
- Policy validator module with strong test coverage
- Registry schema additions
- `--parent` flag on `ws create`
- `destroy` cascade semantics
- Audit log of daemon actions at `~/.drydock/audit.log`

## Out of scope for v2

- **Cross-host orchestration.** Still single-host. Workspaces on separate machines is a later milestone.
- **Capability UI / audit tooling.** CLI and log-based only.
- **Automatic policy derivation.** Parents declare their `delegatable_*` stanzas explicitly; the daemon does not infer.
- **Web dashboard.** Not now.
- **Agent-to-agent messaging** beyond spawn/destroy. Siblings still don't talk over the tailnet by default.

## Open questions

1. Where does the daemon live — launchd service, tmux pane, something else? How does it survive reboot?
2. How does the daemon handle devcontainer CLI errors — retry, propagate, mark workspace as error?
3. Does `ws attach` (host → workspace) route through the daemon too, or stay direct?
4. How does workspace-local state (in-progress tasks) survive a daemon restart?
5. Capability revocation: if a parent's policy changes, do running children get re-evaluated?

## Migration from v1

- V1 workspaces (no `parent_workspace_id`) keep working unchanged — they're host-spawned and the daemon ignores them.
- New `ws` version ships with CLI routing logic but defaults to host mode unless the daemon is configured.
- The daemon is opt-in; if `wsd` never starts, v1 behavior is preserved.

## When to build

When microfoundry's nested case becomes painful enough that spawning children from the host feels wrong. That's the signal — not "the design is ready." V2 is infrastructure, and infrastructure should follow demonstrated need.

What microfoundry can do today without the daemon: host-spawn each workspace individually (`ws create microfoundry`, `ws create auction-crawl`, etc.). You lose the "agent spawns children" story but keep isolation, firewall separation, and per-project config. That's enough to dogfood the isolation model and surface real requirements before v2 code is written.
