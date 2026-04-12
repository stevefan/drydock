# Drydock — Vision

## What it is

Drydock is a **personal agent fabric**. It provisions, connects, and governs the sandboxed workspaces where your Claude agents live and do work.

It is not a container launcher. Launching containers is mechanical plumbing; `devcontainer`, Docker, and `git worktree` already do that. What Drydock provides — the thing that can't be replaced by shell scripts and conventions — is the *fabric*: a policy graph, a daemon-enforced trust boundary, audit, identity, secrets brokering, and cross-host placement.

At v1, the fabric is still a CLI wrapper over primitives. That's a stepping stone. The complete Drydock is a daemon-mediated control plane for a personal fleet of agent workspaces. [v2-scope.md](v2-scope.md) is the plan to get there.

## Where it sits in the stack

```
Substrate    — semantic layer: hypergraphs, dialogue, LLM-native knowledge processing
Patchwork    — context layer: life data (transcripts, tweets, finance, search)
Drydock      — fabric layer: where workspaces run, how they're governed, how they're reachable
```

Each project that uses Drydock (Substrate, Patchwork, Microfoundry, ASI, others) is a workspace or set of workspaces that Drydock spawns, governs, and provides resources to.

## The fabric — what Drydock becomes

These are the properties that make Drydock *infrastructure* rather than a convenience wrapper. V1 does not yet provide them; each is a commitment for the daemon era.

1. **Workspaces as durable addressable places, not container incarnations.** A workspace has a name, a policy scope, a history, and state that outlives its current container. Suspend on your laptop, resume on your home server; the worktree, session state, and in-flight tasks move with it.

2. **A daemon (`wsd`) as the sole control plane.** Every lifecycle operation — create, spawn-child, suspend, migrate, destroy — goes through the daemon. Every operation is authenticated, policy-checked, and audited. The `ws` CLI is one client; Claude Code in a workspace is another; a mobile app could be a third.

3. **A policy graph with enforced narrowness.** Each workspace has declared capabilities, delegatable subsets, firewall allowances, secret entitlements. Children are strictly narrower than parents. Compromise of any single workspace cannot laterally expand authority.

4. **Workspace-to-workspace messaging through the daemon.** When Substrate queries Patchwork, the daemon mediates and checks "is A allowed to ask B this thing?" Agent coordination without broad network access. Audit trail is a natural consequence.

5. **Drydock as secrets broker.** Workspaces request time-bounded credential leases from the daemon. Real API keys live in exactly one place — the daemon's trust anchor — and are never copied, only leased. Auto-rotate, auto-revoke on destroy.

6. **The host fleet.** Workspaces run on a dynamic set of machines: laptop, home server, cloud VM. Placement follows resource availability, persistence needs, data locality. You say `ws create microfoundry`; you don't say where.

7. **Audit as first-class.** "What did auction-crawl do yesterday?" returns: container lifetimes, outbound hosts reached, secrets requested, files modified, messages sent to other workspaces.

8. **The workspace is the unit of identity.** On the tailnet, a stable hostname. In audit logs, a principal. "Microfoundry asked to reach ebay.com at 14:23" is a coherent sentence.

## What v1 delivers today

Scoped to a single host, no daemon, no cross-workspace messaging:

- **Per-workspace devcontainer override.** `ws create` generates a JSON overlay giving each workspace its own Tailscale hostname, firewall extras, secrets mount, identity env vars. Layered on top of the project's devcontainer.json via `devcontainer up --override-config`.
- **Git worktree per workspace.** Deterministic location; reused if branch exists; cleaned up on destroy.
- **Per-project YAML config** at `drydock/projects/{project}.yaml`. CLI flags win over YAML.
- **SQLite registry** at `~/.drydock/registry.db` tracking workspace state + paths + container id.
- **Full lifecycle**: create (worktree + overlay + `devcontainer up`), stop (`devcontainer down`), destroy (stop + worktree rm + overlay rm + registry delete).
- **Default-deny firewall + Tailscale + Claude Code remote control**, from the `.devcontainer/` template.

Microfoundry can start using v1 today from the host ([getting-started.md](getting-started.md)). What v1 does *not* do: nested orchestration. The `ws` CLI runs on the host; a Claude agent inside a workspace cannot spawn siblings. That's v2.

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
         │  ws create microfoundry  (from host only)
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
