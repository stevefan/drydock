# Drydock — Vocabulary

**Purpose.** Pin the vocabulary Drydock uses consistently across product docs, design specs, and the RPC surface. Code identifiers (`ws`, `wsd`, `ws_<slug>`, `Workspace` class, `workspaces` SQLite table) are frozen — renaming them would be cosmetic churn — but every human-facing surface uses the vocabulary below.

## The six layers

| Layer | Canonical name | What it is | What it owns |
|---|---|---|---|
| 0 | **Archipelago** *(internal term)* | The collection of Harbors a principal owns — currently a small set of static island machines (your Mac, Hetzner). Not a "fleet" because Harbors aren't mobile; they're place-bound landmasses connected by tailnet. (The forward-looking "carrier-group" / "mobile factory at sea" shape — cloud-elastic infrastructure with Harbors on it — is aspirational, not where we are.) | The cross-Harbor view: peer-RPC channels, fleet-monitor probes, principal's bird's-eye visibility. No daemon owns the archipelago; it's a perspective the principal holds. |
| 1 | **Harbor** | The host machine (laptop, home server, Hetzner VM) running `wsd`. An island in the archipelago. Authority lives here. | The daemon, the registry, policy state, capability-broker leases, audit log, daemon-level admin secrets. One `wsd` per harbor. |
| 2 | **Harbormaster** | The governing agent on a Harbor — broad-but-shallow authority across all Docks on its Harbor and (via peer-RPC) the rest of the archipelago. | Auth-broker refresh loop, capability grants on behalf of standing policy, throttle/stop/restart actions on misbehaving Dockworkers, escalation to principal via Telegram. Bounded against itself: cannot rewrite its own policy or reach its own master credentials directly. See [harbormaster-authority.md](harbormaster-authority.md). |
| 3 | **Project** | The logical work unit — a body of code + the policy / capability template that should govern any Dock instantiated to work on it. | Immutable config template (`~/.drydock/projects/<project>.yaml`), repo path, policy template, declared capabilities, entitlements, and delegations. **One project may have many Docks** (different worktrees, different branches, different parts of the same codebase, an experimental fork, etc.) — Project is the type, Dock is the instance. |
| 4 | **DryDock** *(formal)* / **Dock** *(shorthand)* | A durable, bounded work environment — the runtime unit. An *instantiation* of a Project on a Harbor. (The metaphor: a drydock is where work building or maintaining the ship happens; the ship itself ventures out into prod / function calls and the metaphor doesn't have to extend that far.) | Container lifecycle, git worktree, policy scope, registry row, named volumes, audit principal, bearer token, branch, accumulated tooling state. Persistent across container rebuilds. |
| 5 | **Dockworker** | The agent bound to a Dock — the thing that actually does work. | Claude Code remote-control process, cron-invoked operator, research agent, schedule job. A Dockworker is durable per-Dock but containers are its body, not its identity. |

Sessions (ephemeral human or agent attachments to a drydock — IDE windows, SSH connections, claude.ai/code sessions) are explicitly *not* first-class. Multiple sessions can attach concurrently; Drydock does not track them.

## The metaphor

An archipelago of Harbors — each Harbor an island machine the principal owns. A Harbor contains multiple Docks (instantiated DryDocks, each housing one Project's worktree), governed by a Harbormaster. A Dock is a bounded place where work gets done on a vessel, fitted with what that work needs. A Dockworker operates in the Dock. The vessel (project code, data, in-flight tasks) stays across many sessions of work — and ventures out as prod / function calls / external service interactions when the work is done. The metaphor stops at the harbor's edge; what the ship does at sea is its own concern.

Multiple Docks per Project is the common case, not the exception: a Project might have one Dock for steady-state work on `main`, a second Dock for an experimental branch, a third Dock dedicated to running the project's scheduled jobs. They share the Project's policy template but each Dock has its own runtime state, its own Dockworker, its own audit trail.

### Internal note: archipelago vs. carrier group

"Fleet" is the wrong word for what we have today. A fleet is mobile — a collection of vessels under way. Our Harbors aren't moving; they're durable, address-stable, place-bound. **Archipelago** captures this better: an island chain where each landmass is independently self-sufficient and the connection between them is the sea (the tailnet). Use "the archipelago" or "across Harbors" in prose; reserve "fleet" for the legacy CLI command name (`ws fleet status`) until that gets renamed too.

The aspirational shape, when/if drydock runs on cloud-elastic infrastructure (Harbors that scale on demand, drydocks that float between hosts), is more like a **carrier group** — a coordinated mobile force with a flagship and supporting vessels — or a **mobile factory at sea**, large enough to do its own work without needing land. We're not there. The vocabulary should reflect what we are (archipelago of islands), not what we might become (carrier group at sea), so the mental model stays calibrated to actual capability.

## Canonical phrasings

- "Harbor `drydock-hillsboro` runs `wsd`" — correct.
- "The Harbormaster on `drydock-hillsboro` granted a lease" — correct.
- "Create a Dock" / "spawn a Dock" — correct (instantiate a DryDock from a Project).
- "Dock `auction-crawl` has a Dockworker named remote-control" — correct (Dock as the instance shorthand).
- "The auction-crawl Project has three Docks: prod-main, exp-arbitrage, and scheduled-jobs" — correct (one Project, multiple Docks).
- "Across the archipelago" / "across Harbors" — correct (multi-Harbor reference).
- "The Dockworker requested a lease" — correct (audit records `desk_id` but the *agent* doing it is a Dockworker).
- "Workspace X" — acceptable in code and registry contexts where the technical artifact is what's meant; in product docs prefer "Dock" or "DryDock".
- "Employee worker" — a specific class of Dockworker: long-running, permissioned, judgment-capable, lives on Harbor infra. The Harbormaster is itself an employee-Dockworker — the canonical instance of the pattern.

### Dock vs DryDock — the type/instance distinction

- **DryDock** (formal) is the *type* — the concept, the class, the pattern. Use in design docs, schema descriptions, anywhere you're talking about "what a DryDock is."
- **Dock** (shorthand) is the *instance* — a specific running thing. "The auction-crawl Dock," "spin up a fresh Dock," "this Dock has been running for two weeks." Reads more naturally in operational prose.
- They refer to the same kind of thing; the distinction is register, not ontology.

## Retired terms

These appear in older docs and code identifiers but should not be used in new prose:

- "**desk**" — colloquial alias for **Dock** / **DryDock**. Historical leak from office-metaphor talk; retired so the codebase stops mixing metaphors. Code identifiers (`desk_id`, `deskwatch`, `delegatable_*`) remain frozen.
- "**deputy**" / "**Harbor agent**" — replaced by **Harbormaster**. The harbormaster is a real maritime role (the official who governs everything in a harbor — allocates berths, enforces regulations, mediates disputes between vessels) and matches what the role does in this fabric far better than "deputy."
- "**Worker**" (capitalized as a role) — replaced by **Dockworker**. Lowercase "worker" remains acceptable in mechanical/computing contexts where the metaphor isn't load-bearing.
- "**fleet**" (as the noun for "all my Harbors") — replaced by **archipelago** in prose. "Fleet" survives in the legacy `ws fleet` CLI command name; expected to rename to `ws harbors` or `ws archipelago` in a future pass.

## Mapping to code / registry / RPC (unchanged)

| Product term | Code identifier |
|---|---|
| Harbor | the host running `wsd`; no separate identifier |
| Project | `ProjectConfig` dataclass; `~/.drydock/projects/<name>.yaml` |
| DryDock | `Workspace` class; `workspaces` SQLite table; `ws_<slug>` ids |
| Dockworker | not first-class in v2 code; represented by the processes running inside a drydock (remote-control supervisor, cron job invocations, etc.) |
| Harbormaster | not yet first-class; will be a `harbormaster_desks` registry table + `scope: "harbormaster"` token grade per [harbormaster-authority.md](harbormaster-authority.md) §2 |
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
