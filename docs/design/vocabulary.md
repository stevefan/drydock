# Drydock — Vocabulary

**Purpose.** Pin the vocabulary Drydock uses consistently across product docs, design specs, and the RPC surface. Code identifiers (`ws`, `wsd`, `ws_<slug>`, `Workspace` class, `workspaces` SQLite table) are frozen — renaming them would be cosmetic churn — but every human-facing surface uses the vocabulary below.

## The four layers

| Layer | Canonical name | What it is | What it owns |
|---|---|---|---|
| 1 | **Harbor** | The host machine (laptop, home server, Hetzner VM) running `wsd`. Authority lives here. | The daemon, the registry, policy state, capability-broker leases, audit log, daemon-level admin secrets. One `wsd` per harbor. |
| 2 | **Project** | YAML description + work-identity. | Immutable config template (`~/.drydock/projects/<project>.yaml`), repo path, policy template, declared capabilities, entitlements, and delegations. Multiple drydocks can derive from one project. |
| 3 | **DryDock** | A durable, bounded work environment — the runtime unit. | Container lifecycle, git worktree, policy scope, registry row, named volumes, audit principal, bearer token, branch, accumulated tooling state. Persistent across container rebuilds. |
| 4 | **Worker** | The agent bound to a drydock — the thing that actually does work. | Claude Code remote-control process, cron-invoked operator, research agent, schedule job. A worker is durable per-drydock but containers are its body, not its identity. |

Sessions (ephemeral human or agent attachments to a drydock — IDE windows, SSH connections, claude.ai/code sessions) are explicitly *not* first-class. Multiple sessions can attach concurrently; Drydock does not track them.

## The metaphor

A harbor contains multiple drydocks. A drydock is a bounded place where work gets done on a vessel, fitted with what that work needs. A worker operates in the drydock. The vessel (project code, data, in-flight tasks) stays across many sessions of work.

## Canonical phrasings

- "Harbor `drydock-hillsboro` runs `wsd`" — correct.
- "Create a drydock" / "spawn a drydock" — correct.
- "DryDock `auction-crawl` has a worker named remote-control" — correct.
- "The worker requested a lease" — correct (audit records `desk_id` but the *agent* doing it is a worker).
- "Workspace X" — acceptable in code and registry contexts where the technical artifact is what's meant; in product docs prefer "drydock".
- "Employee worker" — a specific class of Worker: long-running, permissioned, judgment-capable, lives on Harbor infra (distinct from interactive Claude on a laptop and from deterministic cron). The original `drydock-employee` pattern.

## Mapping to code / registry / RPC (unchanged)

| Product term | Code identifier |
|---|---|
| Harbor | the host running `wsd`; no separate identifier |
| Project | `ProjectConfig` dataclass; `~/.drydock/projects/<name>.yaml` |
| DryDock | `Workspace` class; `workspaces` SQLite table; `ws_<slug>` ids |
| Worker | not first-class in v2 code; represented by the processes running inside a drydock (remote-control supervisor, cron job invocations, etc.) |
| Session | not first-class; implicit in any attached client |

The v1 identifier `ws_<slug>` remains. Read it as **"workspace id"** = **"drydock id"** — same thing. CLI commands (`ws create`, `ws stop`, etc.) keep the `ws` prefix because `drydock` as a CLI prefix would collide with the project name and add no value.

## Why these layers earn their naming

- **Harbor vs DryDock.** The daemon runs on a host; the host contains multiple work environments; separating them linguistically mirrors what Drydock's design enforces (host-authoritative state vs drydock-scoped state).
- **DryDock vs Worker.** A drydock is a *place*; a worker is an *agent*. The same drydock can have successive workers (as tokens rotate, as agent generations replace each other) or a long-lived worker (the employee pattern). Separating them prevents the "is the desk the agent?" confusion the earlier `desk` + `occupant` split tried to patch.
- **Project vs DryDock.** A project declares what a drydock should be; drydocks are instances. Many drydocks can share one project (`ws create microfoundry auction-crawl` and `ws create microfoundry experimental-auction-crawl` are two drydocks of the same project).

## Implications that earn explicit callouts

### Harbor owns authority
- Policy validation, capability leases, audit emission, token issuance — all Harbor-level operations performed by `wsd`. The drydock is *subject* to these; it doesn't make them.
- Fleet-level admin secrets (`~/.drydock/daemon-secrets/`) live at Harbor scope — not per-drydock.
- One `wsd` per Harbor. Multi-daemon single-host is out of scope.

### DryDock owns durable state
- Policy scoping happens at drydock level. Capability grants, firewall allowlists, delegatable secrets, delegatable firewall domains — all keyed on drydock id.
- Audit principal = drydock id. "DryDock `auction-crawl` requested `ebay.com` at 14:23" is the canonical log shape.
- Parent-child relationships live between drydocks. A parent drydock spawns a child drydock. Workers don't spawn anything directly — they ask Harbor (via `wsd`) to spawn on their behalf.
- Bearer tokens are issued per drydock. Multiple workers / sessions inside the same drydock present the same token.

### Worker is the agent abstraction
- A Worker is who's actually doing work — Claude Code remote-control, a cron-invoked scraper, a smart operator. Always bound to exactly one drydock.
- Worker lifetime is independent of container lifetime: the drydock can rebuild its container (base image bump, upgrade) without losing the Worker's logical identity.
- Worker classes emerge as a useful product abstraction: `employee-worker` (long-running, high-trust, narrow capabilities), `interactive-worker` (short-lived, human-driven), `batch-worker` (scheduled, deterministic). V2 doesn't formalize these as daemon types; product surfaces may.

### Session is explicitly out of scope
- Agent-to-agent coordination within a drydock (two Claudes editing the same repo, pair-agent patterns, reviewer/actor) is a substrate/orchestrator problem, not a Harbor problem.
- The daemon does not track session count, session attribution, or per-session policy. If two sessions on one drydock both call `RequestCapability`, `wsd` sees both calls as coming from the same drydock with the same token.
- **Multi-user note (deferred):** when principals become explicit, a session carries a `principal_id`. The token → `(principal_id, desk_id)` lookup remains at drydock-level; per-session policy is never added.

## Historical note

Earlier drafts of these docs used **workspace** / **desk** / **agent-desk** for the concept now called **DryDock**, and **occupant** for what's now called **Worker**. The older vocabulary came from a slip-box note titled *Drydock vocabulary — project desk session*; the three-layer split that doc pushed is preserved here (Project / DryDock / Worker), just renamed. The language shift landed 2026-04-17 after a session exploring the Harbor/DryDock/Worker framing and finding it cleaner than the desk/occupant anthropomorphism. Code identifiers (`ws_<slug>`, `workspaces` table, `Workspace` class) were intentionally left unchanged.

In-flight references in archived documents (notably `_archive/migration-vision.md`) and in older agent-session logs use the older terms. They are correct for their point in time; don't mass-rewrite them.

## Reversibility

| Decision | Cost of reversing |
|---|---|
| Harbor / DryDock / Worker product vocabulary | Low — pure documentation. Code identifiers untouched. |
| Three-layer Project / DryDock / (Worker\|Session) split | Medium — the daemon RPC surface encodes the split (capabilities keyed on drydock, audit principal = drydock, tokens per drydock). The split is load-bearing; the naming on top of it is cheap. |
| Keeping `ws_<slug>` as technical id | Low — pure naming; can alias later if needed. |
| Session as explicitly out of scope | Medium — if session-level policy becomes required, the token model extends from drydock-only to drydock-plus-principal. Multi-user sketch already reserves this extension. |
