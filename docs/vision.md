# Drydock — Vision

## What it is

Drydock is a **personal agent fabric**. It provisions, connects, and governs the sandboxed workspaces where your Claude agents live and do work.

It is not a container launcher. Launching containers is mechanical plumbing; `devcontainer`, Docker, and `git worktree` already do that. What Drydock provides — the thing that can't be replaced by shell scripts and conventions — is the *fabric*: a policy graph, a daemon-enforced trust boundary, audit, identity, secrets brokering, and cross-host placement.

At v1 the fabric is still just a CLI wrapper over primitives. That's a stepping stone. The complete Drydock is a daemon-mediated control plane for a personal fleet of agent workspaces.

## Where it sits in the stack

```
Substrate    — semantic layer: hypergraphs, dialogue, LLM-native knowledge processing
Patchwork    — context layer: life data (transcripts, tweets, finance, search)
Drydock      — fabric layer: where workspaces run, how they're governed, how they're reachable
```

Each project that uses Drydock (Substrate, Patchwork, Microfoundry, ASI, others) is a workspace or set of workspaces that Drydock spawns, governs, and provides resources to.

## The fabric — the properties v1 is a stepping stone toward

These are what make Drydock *infrastructure* rather than a convenience wrapper. V1 does not yet provide them; [v2-scope.md](v2-scope.md) is the plan to get there.

1. **Workspaces as durable addressable places, not container incarnations.** A workspace has a name, a policy scope, a history, and state that outlives its current container. Suspend a workspace on your laptop, resume it on your home server; the worktree, session state, and in-flight tasks move with it. Rebooting the host doesn't lose anything.

2. **A daemon (`wsd`) as the sole control plane.** Every lifecycle operation — create, spawn-child, suspend, migrate, destroy — goes through the daemon. Every operation is authenticated, policy-checked, and audited. The `ws` CLI is one client; Claude Code in a workspace is another; a mobile app could be a third. The daemon is the authority.

3. **A policy graph with enforced narrowness.** Each workspace has declared capabilities, delegatable subsets, firewall allowances, secret entitlements. When a parent spawns a child, the daemon walks the graph: the child cannot exceed the parent's grant; the parent cannot grant what it doesn't hold. Compromise of any single workspace can't laterally expand authority.

4. **Workspace-to-workspace messaging through the daemon.** When Substrate wants to query Patchwork — not via a direct TCP connection across the tailnet, not via the filesystem — it goes through a daemon-mediated call that checks "is A allowed to ask B this thing?" Agent coordination without broad network access. Audit trail is a natural consequence.

5. **Drydock as secrets broker.** Workspaces don't hold long-lived secrets. They request credentials from the daemon, which fetches (from 1Password, vault, or direct), scopes to the keys this workspace is entitled to, and time-bounds (auto-rotate, auto-revoke on destroy). Your real API keys live exactly one place — the daemon's trust anchor — and are never copied, only leased.

6. **The host fleet.** Workspaces run on a dynamic set of machines: your laptop, a home server, a cloud VM. Daemons on each host coordinate (mesh or lead-elected). Placement decisions are informed by resource availability, persistence needs, data locality. You say `ws create microfoundry`; you don't say where.

7. **Audit as first-class.** "What did auction-crawl do yesterday?" returns: container lifetimes, outbound hosts reached, secrets requested, files modified, messages sent to other workspaces. Table stakes for autonomous agents; Drydock is where it's recorded because Drydock is where the operations happen.

8. **The workspace is the unit of identity.** On the tailnet, each workspace has a stable hostname. In audit logs, each workspace is a principal. "Microfoundry asked to reach ebay.com at 14:23" is a coherent sentence.

## What v1 actually delivers today

Scoped to a single host, no daemon, no cross-workspace messaging:

1. **Per-workspace devcontainer override.** `ws create` generates a JSON overlay that gives each workspace its own Tailscale hostname, firewall extras, secrets mount, and identity env vars. Layered on top of the project's devcontainer.json via `devcontainer up --override-config`.
2. **Git worktree per workspace.** Deterministic location, reused if the branch already exists, cleaned up on destroy.
3. **Per-project YAML config** at `drydock/projects/{project}.yaml`. Workspaces resolve defaults from it; CLI flags win.
4. **SQLite registry** at `~/.drydock/registry.db` tracking workspaces (name, state, branch, container id, paths).
5. **Full lifecycle**: create (worktree + overlay + `devcontainer up`), stop (`devcontainer down`), destroy (stop + remove worktree + remove overlay + delete registry row).
6. **Default-deny firewall + Tailscale + Claude Code remote control** — from the `.devcontainer/` template that ships with Drydock.

None of this enables nested orchestration. The `ws` CLI runs on the host; a Claude agent inside a workspace cannot currently spawn siblings. Microfoundry's nested case (see [requirement-microfoundry-nested-orchestration.md](requirement-microfoundry-nested-orchestration.md)) is the forcing function for v2.

## The agent angle

Claude operates at two levels:

- **Operator:** you, or a Claude on your host, call `ws create` to provision workspaces. This is the v1 primary mode.
- **Occupant:** Claude runs *inside* each workspace as the development agent, accessible via remote control or SSH over Tailscale.

Each workspace is a self-contained Claude agent environment: code, tools, scoped network access, a remote-control endpoint. You check in from wherever you are — laptop, phone, another Claude session. Drydock is the workshop where you outfit the ships.

In the fabric end state, occupants become orchestrators of their own children (through the daemon, with narrowed policy). Today they don't.

## Network and firewall

Default-deny egress from every workspace container. Only explicitly whitelisted domains are reachable. Base whitelist covers GitHub, npm, Anthropic API, VS Code marketplace, Tailscale infrastructure. Per-project `firewall_extra_domains` add to it. The daemon (v2) will enforce that each child's firewall is strictly narrower than its parent's.

Conservative tailnet policy: your devices reach workspaces, workspaces reach their internet whitelist, workspaces don't reach each other by default. Cross-workspace communication is a future capability gated on explicit policy — in the fabric model, mediated by the daemon.

## Architecture

### Today (v1)

```
Host machine
├── ws CLI (pip install, venv)
├── devcontainer CLI
├── Docker
├── ~/.drydock/
│     registry.db, overlays/, worktrees/
└── /srv/secrets/<workspace_id>/   (operator populates)
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

### V2 direction (see [v2-scope.md](v2-scope.md))

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

The daemon doesn't replace the v1 primitives (overlay generator, worktree, devcontainer wrapper, registry) — it puts a trust boundary in front of them.

## Why it matters

Workspaces are persistent places, not ephemeral sessions. You don't "start working on Patchwork" — you check in on the Patchwork workspace. Claude is a coworker with a desk, not a tool you invoke. It has a persistent session with context, history, and ongoing tasks. You can leave, come back, and ask "what did you do while I was gone?"

The firewall is what makes autonomous agent work safe. Default-deny means Claude literally cannot reach services you haven't approved. It can't accidentally hit production APIs, can't leak code to unauthorized endpoints. The sandbox is what lets you close your laptop and trust the work continues.

Tailscale makes location irrelevant. A workspace has a stable tailnet hostname. You think in workspace names, not machine names. You check in from your phone, your laptop, or another Claude session — it doesn't matter.

The daemon (v2) is what turns this from "a trust boundary at `ws create` time" into "a trust boundary at every operation." That's the difference between "I ran this yesterday" and "a dozen agents run concurrently, each with narrow capabilities, accountable."

## What v1 is

A local CLI, a SQLite file, and a `.devcontainer/` template with good defaults. Microfoundry can start using it today (see [getting-started.md](getting-started.md)). It's enough to validate the isolation model, firewall policy differentiation, and per-project config flow.

## What v2 becomes

Drydock becomes infrastructure the moment the daemon exists. Until then it's a convenience wrapper; after that, it's where policy, identity, and audit for your agent fleet live.

## What this is not

Not a general platform. Not multi-tenant. Not trying to replace Kubernetes or GitHub Actions. It is a personal agent fabric for one person running a fleet of maybe 10-50 workspaces across 2-5 machines. The opinions are strong; the abstractions are few; the aim is to make autonomous agent work routine, safe, and legible.
