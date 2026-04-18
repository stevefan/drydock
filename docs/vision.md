# Drydock — Vision

## What it is

Drydock is a **personal agent fabric**. It provisions, connects, and governs the **drydocks** where Claude workers live and do work.

A drydock is a durable addressable place — a bounded work environment with a worker bound to it. It has a name, a policy scope, a git worktree, accumulated state, and a stable identity that outlives its current container. "Workspace" is the technical artifact (daemon entity, registry row, container); "drydock" is the product-level name — the unit of durable, bounded work.

Three pieces of vocabulary you'll see throughout these docs; full definitions in [v2-design-vocabulary.md](v2-design-vocabulary.md):

- **Harbor** — the host machine (laptop, home server, cloud VM) running `wsd`. Authority lives here.
- **DryDock** — a durable, bounded work environment (the runtime unit — one per project, roughly). Many drydocks per harbor.
- **Worker** — the agent bound to a drydock; the thing that actually does work.

Drydock is not a container launcher. Launching containers is mechanical plumbing; `devcontainer`, Docker, and `git worktree` already do that. What Drydock provides — the thing that can't be replaced by shell scripts and conventions — is the *fabric*: a policy graph, a daemon-enforced trust boundary, audit, identity, and capability brokerage.

At v1, the fabric was a CLI wrapper over primitives. V2 makes the daemon the control plane; v1.0.0 rc1 is shipped. [v2-scope.md](v2-scope.md) is the plan and status.

## Where it sits

Drydock is the **fabric layer**: where drydocks run, how they're governed, how they're reachable. Projects that use Drydock — application repos, monorepos, isolated experiments — each map to one or more drydocks that Harbor spawns, governs, and provides resources to. Drydock itself is infrastructure; the projects that sit on top of it are independent.

## The fabric — what Drydock becomes

These are the properties that make Drydock *infrastructure* rather than a convenience wrapper. Each is either shipped in V2 rc1 or committed for V2.x.

1. **DryDocks as durable addressable places, not container incarnations.** A drydock has a name, a policy scope, a history, and state that outlives its current container. Rebuild the container (`ws upgrade --force`, base-image bump) and the worktree, state, and accumulated tooling are intact on the other side. Harbor reboot → drydocks auto-resume via systemd (shipped 2026-04-17).

2. **A daemon (`wsd`) as the sole control plane on each Harbor.** Every lifecycle operation — create, spawn-child, stop, destroy — goes through the daemon. Every operation is authenticated, policy-checked, and audited. The `ws` CLI is one client; a Worker inside a drydock is another; a mobile app could be a third.

3. **A policy graph with enforced narrowness.** Each drydock has declared capabilities, delegatable subsets, firewall allowances, secret entitlements. Children are strictly narrower than parents. Compromise of any single drydock cannot laterally expand authority.

4. **DryDock-to-drydock messaging through the daemon.** When one drydock queries another, the daemon mediates and checks "is A allowed to ask B this thing?" Worker coordination without broad network access. Audit trail is a natural consequence. Cross-drydock secret delegation shipped in V2.1.

5. **Drydock as capability broker.** Workers request time-bounded leases from the daemon. Real credentials live in exactly one place — Harbor's trust anchor — and are never copied ambiently, only leased. Auto-rotate, auto-revoke on destroy.

6. **Always-on durability on a chosen Harbor.** Drydock assumes one Harbor per drydock for the drydock's lifetime — a laptop, a home server, or a cloud VM, picked when the drydock is created. DryDocks don't migrate between harbors; they're pinned. Hardware refresh is a rebuild-from-config procedure (yaml + registry + worktree branches), not a daemon primitive. Cross-host fleet choreography (migration, placement, identity continuity) was explored and deliberately archived — see `_archive/migration-vision.md`.

7. **Audit as first-class.** "What did `scraper` do yesterday?" returns: container lifetimes, outbound hosts reached, secrets requested, files modified, messages sent to other drydocks.

8. **The drydock is the unit of identity.** On the tailnet, a stable hostname. In audit logs, a principal. "DryDock `scraper` asked to reach `example.com` at 14:23" is a coherent sentence.

## What v1 / V2.0 deliver today

Harbor-scoped, daemon-backed (V2 rc1):

- **Per-drydock devcontainer override.** `ws create` generates a JSON overlay giving each drydock its own Tailscale hostname, firewall extras, secrets mount, identity env vars. Layered on top of the project's devcontainer.json via `devcontainer up --override-config`.
- **Git worktree per drydock.** Deterministic location; reused if branch exists; cleaned up on destroy.
- **Per-project YAML config** at `~/.drydock/projects/{project}.yaml`. CLI flags win over YAML. Supports V2 capability/entitlement/delegation fields.
- **SQLite registry** at `~/.drydock/registry.db` tracking drydock state + paths + container id + policy columns.
- **Full lifecycle**: create (worktree + overlay + `devcontainer up`), stop (container down, worktree preserved), destroy (stop + worktree rm + overlay rm + registry delete + tailnet device delete).
- **Default-deny firewall + Tailscale + Claude Code remote control**, from the `drydock-base` image.
- **`wsd` daemon** with 11 RPC methods (lifecycle, introspection, capability broker, audit). Bearer-token auth. Resume-on-`ws create` for suspended drydocks.
- **Systemd integration on Linux harbors** — `wsd` and drydock lifecycle survive reboot.
- **Cross-drydock secret delegation** (V2.1) — a worker in one drydock can request a secret from another via the daemon, narrowness-validated.

## The worker angle

Claude (and agents in general) operate at two levels:

- **Operator** — you, or a Claude on your harbor, calls `ws create` from the host. The Operator is the thing with harbor-admin authority.
- **Worker** — bound to a drydock, reachable via Tailscale-served remote-control or SSH. In the V2 fabric, workers become orchestrators of their own children (via the daemon, with narrowed policy). Today nested spawn is supported for host-initiated create; worker-initiated spawn via in-drydock RPC is the next V2.x piece.

A specific Worker class: the **employee worker** — long-running, permissioned, judgment-capable, lives on Harbor infra. Distinct from interactive Claude on a laptop (short-lived) and deterministic cron (no judgment). The first concrete employee is the fleet-auth worker in the `infra` drydock on `drydock-hillsboro` (see [employee-fleet-auth.md](employee-fleet-auth.md)).

## Architecture

### V1 (what v1 was)

```
Harbor (host machine)
├── ws CLI (pip install, venv)
├── devcontainer CLI
├── Docker
├── ~/.drydock/
│     registry.db, overlays/, worktrees/, secrets/<ws_id>/
         │
         │  ws create myapp  (from harbor only)
         ▼
    ┌─────────────────────────┐
    │ DryDock container         │
    │  Claude Code + remote     │
    │  iptables firewall        │
    │  Tailscale networking     │
    │  worktree mounted         │
    └─────────────────────────┘
```

### V2 (now, rc1)

```
Harbor
├── wsd daemon — sole control plane, authenticated (systemd-managed on Linux)
├── policy validator — narrowness enforcement on every spawn + capability grant
├── parent-child registry — cascade semantics
├── audit log
└── capability broker — leases for secrets today; storage/compute reserved
         ▲
         │  authenticated calls over Unix socket
         │
    ┌─────────────────────────┐
    │ DryDock                   │
    │  ws CLI in desk mode      │
    │    │ detects $DRYDOCK_... │
    │    ▼                      │
    │  daemon client            │
    │  → spawn child drydock    │
    │  → request secret lease   │
    │  → cross-drydock delegate │
    └─────────────────────────┘
```

The daemon doesn't replace v1 primitives (overlay generator, worktree, devcontainer wrapper, registry). It puts a trust boundary in front of them.

## What this is not

Not a general platform. Not multi-tenant. Not trying to replace Kubernetes or GitHub Actions. It is a personal agent fabric for one person running a fleet of maybe 10–50 drydocks across 2–5 harbors. The opinions are strong; the abstractions are few; the aim is to make autonomous worker execution routine, safe, and legible.
