# Drydock — Vision

## What it is

Drydock is a **personal agent fabric**. It provisions, connects, and governs the drydocks where Claude workers live and do work.

A drydock is a durable, bounded work environment — a named place with a policy scope, a git worktree, accumulated state, and a stable identity that outlives its current container. "Workspace" is the technical artifact (registry row, container, code identifier); "drydock" is the product name.

Three pieces of vocabulary you'll see throughout these docs; full definitions in [design/vocabulary.md](design/vocabulary.md):

- **Harbor** — the host machine (laptop, home server, cloud VM) running `wsd`. Authority lives here.
- **DryDock** — a durable, bounded work environment (the runtime unit — typically one per project). Many drydocks per Harbor.
- **Worker** — the agent bound to a drydock; the thing that actually does work.

Drydock is not a container launcher. Launching containers is mechanical plumbing; `devcontainer`, Docker, and `git worktree` already do that. What Drydock provides — the thing that can't be replaced by shell scripts and conventions — is the *fabric*: a policy graph, a daemon-enforced trust boundary, audit, identity, and capability brokerage.

## Where it sits

Drydock is the **fabric layer**: where drydocks run, how they're governed, how they're reachable. Projects that use Drydock — application repos, monorepos, isolated experiments — each map to one or more drydocks that the Harbor spawns, governs, and provides resources to. Drydock itself is infrastructure; the projects that sit on top of it are independent.

## The fabric — what Drydock is

These are the properties that make Drydock infrastructure rather than a convenience wrapper.

1. **DryDocks as durable addressable places, not container incarnations.** A drydock has a name, a policy scope, a history, and state that outlives its current container. Rebuild the container (`ws upgrade`, base-image bump) and the worktree, state, and accumulated tooling are intact on the other side. Harbor reboot → drydocks auto-resume via systemd.

2. **A daemon (`wsd`) as the sole control plane on each Harbor.** Every lifecycle operation — create, spawn-child, stop, destroy — goes through the daemon. Every operation is authenticated, policy-checked, and audited. The `ws` CLI is one client; a worker inside a drydock is another; a mobile app could be a third. See [design/in-desk-rpc.md](design/in-desk-rpc.md).

3. **A policy graph with enforced narrowness.** Each drydock has declared capabilities, delegatable subsets, firewall allowances, secret entitlements, storage scopes. Children are strictly narrower than parents. A worker cannot exceed the drydock it runs in. Compromise of any single drydock cannot laterally expand authority. See [design/narrowness.md](design/narrowness.md).

4. **Drydock as capability broker.** Workers request time-bounded leases from the daemon — scoped secrets (same-drydock or delegated from another drydock), scoped AWS STS credentials for specific S3 buckets/prefixes. Real credentials live in one place on the Harbor; the daemon mints narrow session creds on demand and revokes them on release. See [design/capability-broker.md](design/capability-broker.md) and [design/storage-mount.md](design/storage-mount.md).

5. **Audit as first-class.** "What did `scraper` do yesterday?" returns: container lifetimes, outbound hosts reached, secrets requested, storage scopes leased, files modified, messages sent to other drydocks. Stable event vocabulary, consumer-facing contract. See [design/persistence.md](design/persistence.md).

6. **The drydock is the unit of identity.** On the tailnet, a stable hostname. In audit logs, a principal. "DryDock `scraper` asked to reach `example.com` at 14:23" is a coherent sentence. Daemon owns the full lifecycle of that identity, including tailnet device-record cleanup on destroy ([design/tailnet-identity.md](design/tailnet-identity.md)).

7. **Always-on durability on a chosen Harbor.** A drydock is pinned to the Harbor that creates it. Hardware refresh is a rebuild-from-config runbook (yaml + registry dump + worktree branches on a fresh box), not a daemon primitive. Cross-host fleet choreography (migration, placement, identity continuity) was explored and deliberately archived — see [_archive/migration-vision.md](_archive/migration-vision.md).

## What ships today

Harbor-scoped, daemon-backed (v1.0.0):

- **`wsd` daemon** — 11 RPC methods (lifecycle, introspection, capability broker, audit). Bearer-token auth over a Unix socket bind-mounted into each drydock. Systemd-managed on Linux; launchd on macOS.
- **Per-drydock devcontainer override.** `ws create` generates a JSON overlay giving each drydock its own Tailscale hostname, firewall allowlist, secrets mount, identity env vars, daemon-socket bind-mount, and embedded `drydock-rpc` client.
- **Git worktree per drydock.** Deterministic location; reused if the branch exists; cleaned up on destroy.
- **Per-project YAML** at `~/.drydock/projects/{project}.yaml` with V2 capability/entitlement/delegation fields (`capabilities`, `secret_entitlements`, `delegatable_secrets`, `delegatable_storage_scopes`, `delegatable_firewall_domains`).
- **SQLite registry** at `~/.drydock/registry.db` tracking drydock state, paths, container id, policy columns, leases, tokens, task log.
- **Full lifecycle**: create/resume/stop/destroy; parent-child cascade; tailnet device-record cleanup on destroy.
- **Default-deny firewall + Tailscale + Claude Code remote-control**, from the `drydock-base` image.
- **Resume-on-`ws create`** for suspended drydocks with overlay regeneration (code-level overlay changes land without `--force`).
- **Reboot recovery on Linux Harbors** — systemd units + ExecStop hook + boot-sweep recover every drydock through power cycles and daemon restarts.
- **Cross-drydock secret delegation** — `RequestCapability(type=SECRET, scope={secret_name, source_desk_id})` lets one drydock receive a secret held by another via daemon-mediated copy.
- **Scoped cloud storage credentials** — `RequestCapability(type=STORAGE_MOUNT, scope={bucket, prefix, mode})` calls `sts:AssumeRole` with an inline session policy; materializes AWS session creds into the drydock's `/run/secrets/`.
- **Narrowness enforcement** — firewall, secrets, capabilities, mounts, and storage scopes all flow through the same uniform validator for spawn and lease-request.

## The worker angle

Claude (and agents in general) operate at two levels:

- **Operator** — you, or a Claude on the Harbor, calls `ws create` from the host. Operator has Harbor-admin authority.
- **Worker** — bound to a drydock, reachable via Tailscale-served remote-control or SSH. Workers ask the daemon for their own siblings (nested spawn), request scoped capabilities, or run as employees — long-lived permissioned processes that refresh credentials, hold fleet-auth state, or provision infrastructure for peer drydocks. See [design/employee-worker.md](design/employee-worker.md).

## Architecture

```
Harbor
├── wsd daemon           sole control plane, systemd-managed (Linux) or launchd (Mac)
├── policy validator     narrowness enforcement on every spawn + lease request
├── registry             parent-child links, leases, tokens, task log
├── capability broker    SECRET + STORAGE_MOUNT leases; COMPUTE_QUOTA, NETWORK_REACH reserved
└── audit log            stable event vocabulary, ~/.drydock/audit.log
         ▲
         │  JSON-RPC 2.0 over ~/.drydock/run/wsd.sock (bind-mounted into every drydock)
         │
    ┌─────────────────────────┐
    │ DryDock                   │
    │  drydock-rpc client        │
    │  Claude Code + remote ctl  │
    │  iptables firewall         │
    │  Tailscale identity        │
    │  worktree + named volumes  │
    └─────────────────────────┘
```

The daemon doesn't replace the primitives (overlay generator, worktree, devcontainer wrapper, registry). It puts a trust boundary in front of them.

## What this is not

Not a general platform. Not multi-tenant. Not trying to replace Kubernetes or GitHub Actions. It is a personal agent fabric for one person running a fleet of maybe 10–50 drydocks across 2–5 harbors. The opinions are strong; the abstractions are few; the aim is to make autonomous worker execution routine, safe, and legible.
