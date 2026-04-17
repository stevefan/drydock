# Drydock — Vision

## What it is

Drydock is a **personal agent fabric**. It provisions, connects, and governs the **agent-desks** where your Claude agents live and do work.

An agent-desk is a durable addressable place — a workspace with an occupant. It has a name, a policy scope, a git worktree, accumulated session state, and a stable identity that outlives its current container. "Workspace" is the technical artifact (daemon entity, registry row, container); "desk" is the conceptual framing — persistent place where an agent works.

It is not a container launcher. Launching containers is mechanical plumbing; `devcontainer`, Docker, and `git worktree` already do that. What Drydock provides — the thing that can't be replaced by shell scripts and conventions — is the *fabric*: a policy graph, a daemon-enforced trust boundary, audit, identity, and secrets brokering.

At v1, the fabric is still a CLI wrapper over primitives. That's a stepping stone. The complete Drydock is a daemon-mediated control plane for a personal fleet of agent workspaces. [v2-scope.md](v2-scope.md) is the plan to get there.

## Where it sits

Drydock is the **fabric layer**: where workspaces run, how they're governed, how they're reachable. Projects that use Drydock — application repos, monorepos, isolated experiments — each map to one or more workspaces that Drydock spawns, governs, and provides resources to. Drydock itself is infrastructure; the projects that sit on top of it are independent.

## The fabric — what Drydock becomes

These are the properties that make Drydock *infrastructure* rather than a convenience wrapper. V1 does not yet provide them; each is a commitment for the daemon era.

1. **Workspaces as durable addressable places, not container incarnations.** A workspace has a name, a policy scope, a history, and state that outlives its current container. Rebuild the container (`ws upgrade --force`, base-image bump) and the worktree, session state, and accumulated tooling are intact on the other side.

2. **A daemon (`wsd`) as the sole control plane.** Every lifecycle operation — create, spawn-child, stop, destroy — goes through the daemon. Every operation is authenticated, policy-checked, and audited. The `ws` CLI is one client; Claude Code in a workspace is another; a mobile app could be a third.

3. **A policy graph with enforced narrowness.** Each workspace has declared capabilities, delegatable subsets, firewall allowances, secret entitlements. Children are strictly narrower than parents. Compromise of any single workspace cannot laterally expand authority.

4. **Workspace-to-workspace messaging through the daemon.** When one workspace queries another, the daemon mediates and checks "is A allowed to ask B this thing?" Agent coordination without broad network access. Audit trail is a natural consequence.

5. **Drydock as secrets broker.** Workspaces request time-bounded credential leases from the daemon. Real API keys live in exactly one place — the daemon's trust anchor — and are never copied, only leased. Auto-rotate, auto-revoke on destroy.

6. **Always-on durability on a chosen host.** Drydock assumes one host per desk for the desk's lifetime — a laptop, a home server, or a cloud VM, picked when the desk is created. Desks don't migrate between hosts; they're pinned. Hardware refresh is a rebuild-from-config procedure (yaml + registry + worktree branches), not a daemon primitive. Cross-host fleet choreography (migration, placement, identity continuity) was explored and deliberately archived — see `_archive/migration-vision.md`.

7. **Audit as first-class.** "What did `scraper-desk` do yesterday?" returns: container lifetimes, outbound hosts reached, secrets requested, files modified, messages sent to other workspaces.

8. **The workspace is the unit of identity.** On the tailnet, a stable hostname. In audit logs, a principal. "Desk `scraper` asked to reach `example.com` at 14:23" is a coherent sentence.

## What v1 delivers today

Scoped to a single host, no daemon, no cross-workspace messaging:

- **Per-workspace devcontainer override.** `ws create` generates a JSON overlay giving each workspace its own Tailscale hostname, firewall extras, secrets mount, identity env vars. Layered on top of the project's devcontainer.json via `devcontainer up --override-config`.
- **Git worktree per workspace.** Deterministic location; reused if branch exists; cleaned up on destroy.
- **Per-project YAML config** at `drydock/projects/{project}.yaml`. CLI flags win over YAML.
- **SQLite registry** at `~/.drydock/registry.db` tracking workspace state + paths + container id.
- **Full lifecycle**: create (worktree + overlay + `devcontainer up`), stop (`devcontainer down`), destroy (stop + worktree rm + overlay rm + registry delete).
- **Default-deny firewall + Tailscale + Claude Code remote control**, from the `.devcontainer/` template.

You can start using v1 today from the host ([getting-started.md](getting-started.md)). What v1 does *not* do: nested orchestration. The `ws` CLI runs on the host; a Claude agent inside a workspace cannot spawn siblings. That's v2.

## The agent angle

Claude operates at two levels:

- **Operator** — you, or a Claude on your host, call `ws create`. V1 primary mode.
- **Occupant** — Claude runs inside each workspace, reachable via Tailscale-served remote control or SSH.

In the fabric end state, occupants become orchestrators of their own children (through the daemon, with narrowed policy). Today they don't.

## Architecture

### V1 (today)

```
Host machine
├── ws CLI (pip install, venv)
├── devcontainer CLI
├── Docker
├── ~/.drydock/
│     registry.db, overlays/, worktrees/, secrets/<workspace_id>/
         │
         │  ws create myapp  (from host only)
         ▼
    ┌─────────────────────────┐
    │ Workspace container       │
    │  Claude Code + remote     │
    │  iptables firewall        │
    │  Tailscale networking     │
    │  worktree mounted         │
    └─────────────────────────┘
```

### V2 direction

```
Host (or one of several hosts in the fleet)
├── wsd daemon — sole control plane, authenticated
├── policy validator — narrowness enforcement on every spawn
├── parent-child registry — cascade semantics
├── audit log
└── secrets broker — time-bounded credential leases
         ▲
         │  authenticated calls over Tailscale
         │
    ┌─────────────────────────┐
    │ Workspace                 │
    │  ws CLI in workspace mode │
    │    │ detects $DRYDOCK_... │
    │    ▼                      │
    │  daemon client            │
    │  → create sibling         │
    │  → request secret lease   │
    │  → message other workspace│
    └─────────────────────────┘
```

The daemon doesn't replace the v1 primitives (overlay generator, worktree, devcontainer wrapper, registry). It puts a trust boundary in front of them.

## What this is not

Not a general platform. Not multi-tenant. Not trying to replace Kubernetes or GitHub Actions. It is a personal agent fabric for one person running a fleet of maybe 10-50 workspaces across 2-5 machines. The opinions are strong; the abstractions are few; the aim is to make autonomous agent work routine, safe, and legible.
