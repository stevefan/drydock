# Drydock — Vocabulary

**Purpose.** This is the canonical model for what each layer of drydock is, what owns what, and how the pieces relate. Every other doc points here; if a doc disagrees with this one, this one wins. **Last full revision: 2026-05-05** following the V3 architectural reframe (DryDock-as-harness, Harbormaster-as-bouncer, Yard layer).

Code identifiers (`Workspace` class, `workspaces` SQLite table, `ws_<slug>` ID prefix, `ws` CLI binary, `wsd` daemon name) are V1 historical artifacts. Renaming them is doable but scope-explosive (every test, every fixture, every import); it can happen as an explicit refactor when the appetite exists. **In the meantime: human-facing surfaces use the vocabulary below, even if the code identifiers don't yet.**

---

## The seven layers

| Layer | Canonical name | What it is | What it owns |
|---|---|---|---|
| 0 | **Archipelago** *(textual metaphor)* | The collection of Harbors a principal owns. Currently a small set of static island machines (Mac, Hetzner). | A perspective the principal holds. No daemon owns the archipelago; it's how we *describe* the harbors collectively. The technical/CLI term is just **Harbors** (`ws harbors status`). |
| 1 | **Harbor** | The host machine running `wsd`. An island in the archipelago. | The daemon, the registry, policy state, capability-broker leases, audit log, daemon-level admin secrets. One `wsd` per Harbor. |
| 2a | **Harbor Authority** | The rule-enforcement role. The gate — capability broker, narrowness check, resource ceiling enforcement. | Auth, narrowness validation at every capability request, throttle/deny actions per standing policy, hard cgroup ceiling enforcement. Mostly what `wsd` *already does* in V1/V2; just needs naming as a distinct role. |
| 2b | **Port Auditor** | The observation role with **bounded defensive action authority**. The watch with a fire-axe. | Reads declared resource profiles and compares to actual; flags anomalies; escalates novel cases to principal via Telegram. Can take **defensive + reversible** actions directly (throttle, stop, freeze, revoke). Cannot take **destructive or expensive** actions (delete, provision, spend) — those always escalate. |
| 2c | **Harbormaster** *(deferred — manager role, optional)* | The orchestration role. The big-picture manager. | Cross-DryDock coordination, scheduling decisions, balancing resources across Yards. Deferred until there's enough fleet activity that holistic orchestration earns its keep. Not why drydock exists initially. |
| 3 | **Yard** *(optional)* | A grouping of related DryDocks that share concerns. | Shared budget envelope, internal address space (private inter-Drydock network), Yard-wide secrets pool, collective metering. Maps naturally to monorepos but isn't strictly tied. Standalone DryDocks live outside any Yard or in a Yard-of-one. |
| 4 | **Project** | The infrastructure-as-code declaration for one DryDock. The YAML recipe. | Code pointer (repo + subdir), narrowness (capabilities, network reach, secrets), resource ceilings, fleet declaration (which containers to run), inter-Drydock service exposure. **Project = the IaC; DryDock = what gets instantiated from it.** |
| 5 | **DryDock** *(formal)* / **Dock** *(shorthand)* | The infrastructure harness for one logical service. **NOT the runtime — the harness around the runtime.** | Network policy enforcement points, storage mounts, secret holdings, capability scope, resource budget, identity, audit principal. The Fleet of containers (layer 6) lives within the DryDock's harness. |
| 6 | **Fleet** | The containers/ships running within one DryDock. | Multiple containers (service workers, API servers, DB, the agent's container). Each container is a **Ship**. The Fleet is the collection. |
| 7 | **Ship** *(= container)* / **Service** | A single running container — one process group bounded by one security/isolation boundary. | The actual computation. One Ship typically runs one logical service. The agent (layer 8) lives in one specific Ship. |
| 8 | **Dockworker** *(= the agent)* | The judgment-capable agent running inside one of the Fleet's Ships, with broker access. Responsible for *the work* of the DryDock. | Maintaining the DryDock's other services (restart crashed Ships, tune schedules, decide work strategy), proposing infrastructure amendments via the broker, exposing the DryDock's services to siblings. **Owns the work; consumes the harness.** |

Sessions (ephemeral human or agent attachments — IDE windows, SSH connections, claude.ai/code sessions) are explicitly *not* first-class. Multiple sessions can attach concurrently; drydock does not track them.

---

## The metaphor, fully extended

An archipelago of Harbors — each Harbor an island machine the principal owns. On each Harbor, two governance roles operate the port-business concerns: a **Harbor Authority** that enforces the rules (paperwork, declarations, resource quotas, capability gates) and a **Port Auditor** that watches for divergence between what was declared and what's actually happening. Neither tells the captains how to navigate or what to load — those are the Dockworkers' decisions.

A future third role, **Harbormaster**, would be a holistic manager — coordinating across DryDocks, scheduling, balancing. Drydock doesn't need this initially; infrastructure provisioning + rule enforcement + audit-observation is the V3 starting point.

A Harbor contains **Yards** (groups of related DryDocks with shared concerns) and standalone DryDocks. A **DryDock** is a structure — a harness — that holds a Fleet of Ships while work happens on them. The DryDock is *infrastructure*: the cranes, the scaffolding, the water-control, the power feeds, the dock-side warehousing. It exists to *serve* the Ships.

A **Fleet** of Ships sits in the DryDock. Each Ship is a single container running one service. The Ships are bounded individually (each is its own security boundary) but share the DryDock's harness collectively. They communicate with each other internally; they communicate with the outside world through the harness's controlled gates (proxy, firewall, NETWORK_REACH grants).

The **Dockworker** (the agent) lives in one of the Ships. The Dockworker is the human-equivalent skilled worker who maintains the rest of the Fleet and decides what work needs doing. The Dockworker consumes the harness's resources (asks the broker for secrets, opens egress via NETWORK_REACH, registers heavy workloads); the Harbormaster watches that consumption and ensures it matches what was declared.

The **Project** YAML is the **infrastructure-as-code** description of all of this for one DryDock — what Ships to launch, what harness to provide, what narrowness applies. The principal authors it directly; the Dockworker can propose amendments through a structured contract; the Harbormaster auto-applies amendments that fit standing policy and escalates novel ones to the principal.

The metaphor stops at the Harbor's edge. What the Ships do at sea (in production, when called as a service from outside, when interacting with external APIs) is their own concern. We model the *workshop where work happens*, not the open ocean where the work-product is used.

---

## Infrastructure-as-code + the amendment contract

The Project YAML is canonical IaC. Three classes of change:

| Change author | Approval gate | Examples |
|---|---|---|
| **Principal direct** | None (you're the principal) | Edit YAML, `ws project edit auction-crawl`, git commit + reload |
| **Dockworker, within standing policy** | Harbormaster auto-applies | Open a domain that matches `delegatable_network_reach` glob; register a workload within `workload_max`; request a secret already in `delegatable_secrets` |
| **Dockworker, novel** | Harbormaster escalates to principal | Request a new domain not covered by glob; request permanent ceiling lift; propose a new Ship in the Fleet; request a new capability type |

The Dockworker's amendment proposals carry structured metadata (the EGRESS_GRANTS prototype shape, generalized): `requested_by` service name, `reason` prose, `tos_notes`, `status: pending|approved|denied`, `reviewed_by/at/note` for review trail. The Harbormaster's audit log records every proposal, every approval/denial, every application. Principal review happens via Telegram (one-word reply) or by editing the YAML directly.

This is the multi-author IaC pattern. Traditional IaC has one class of author (human + CI); we have three (principal, Dockworker-within-policy, Dockworker-novel-via-escalation). The novelty is one of the authors is an LLM and the approval surface is conversational.

---

## Container vs Resource boundaries — the V3 separation

Pre-V3 drydock conflated two boundaries inside one container:
- *Security boundary*: what code runs, what it can see internally
- *Resource boundary*: what external resources it can reach (firewall in container, cgroup at create-time)

V3 separates them:
- **Security boundary = the Ship (container).** A bounded place where one service's code runs. Compromise stays inside. Recreating the Ship = a real security operation (fresh isolation slate).
- **Resource boundary = the DryDock (harness).** External infrastructure the Ship consumes. Network policy, storage mounts, secrets, compute scaling, capability scope. **Mutable without touching the Ship** — proxy rules update, IAM grants change, secret rotates, all without container recreate.

This separation is *the* V3 insight that resolves most of the "live vs pinned" friction from earlier designs. Most policy lives outside the container, so changing it doesn't require recreate. Container recreate becomes the rare event (security re-baseline, code update), not the routine one.

---

## Canonical phrasings

- "Harbor `drydock-hillsboro` runs `wsd`" — correct.
- "The Port Auditor on `drydock-hillsboro` flagged a resource anomaly" — correct (Port Auditor watches and flags).
- "The Harbor Authority denied auction-crawl's NETWORK_REACH request for evil.com" — correct (Authority enforces narrowness).
- "Principal escalation from Port Auditor: actual usage diverging from declared on dock auction-crawl @ hetzner" — correct (intrusive actions escalate; Auditor doesn't auto-stop).
- "Yard `microfoundry` contains 3 DryDocks: main, auction-crawl, permits-pdx" — correct (Yard groups related Drydocks).
- "Dock `auction-crawl` has a Fleet of 4 Ships: crawl-worker, api-server, db, agent" — correct (Fleet = containers within one Drydock).
- "The Dockworker in auction-crawl restarted its crawl-worker Ships" — correct (Dockworker maintains the Fleet within its DryDock).
- "Across the archipelago" / "across Harbors" — correct (multi-Harbor reference; archipelago is metaphor, harbors is technical).
- "auction-crawl Project declares network_reach for govdeals.com" — correct (Project is the IaC declaration).
- "The auction-crawl Dockworker requested NETWORK_REACH for a new domain; Harbormaster escalated" — correct (amendment outside standing policy → escalation).

### Dock vs DryDock — the type/instance distinction

- **DryDock** (formal) is the *type* — the concept, the class, the pattern. Use in design docs, schema descriptions, anywhere you're talking about "what a DryDock is."
- **Dock** (shorthand) is the *instance* — a specific running thing. "The auction-crawl Dock," "spin up a fresh Dock," "this Dock has been running for two weeks." Reads more naturally in operational prose.
- They refer to the same kind of thing; the distinction is register, not ontology.

### Ship vs Container vs Service — the same thing, different angles

- **Container** is the technical artifact — `docker run`, a process group bounded by namespaces and cgroups.
- **Ship** is the metaphor — what's docked in the DryDock, what the Dockworker operates on.
- **Service** is the function — what the Ship is *for* (serve API requests, run a crawler, hold a DB).

Choose based on register: code/audit → container, prose/architecture → ship, business-purpose → service. They all refer to the same runtime entity.

---

## Retired terms

These appear in older docs and code identifiers but should not be used in new prose:

- "**workspace**" — was the original V1 name for what's now called **DryDock** (and historically the runtime concept; now the harness). Code keeps `Workspace` class and `workspaces` table as historical, but prose should use DryDock or Dock.
- "**desk**" — colloquial alias for what's now called **Dock**. Office-metaphor leak. Code identifiers (`desk_id`, `deskwatch`, `delegatable_*`) remain frozen.
- "**deputy**" / "**Harbor agent**" — was replaced by Harbormaster, now further split into **Harbor Authority** (rule enforcement) + **Port Auditor** (observation), with **Harbormaster** reserved as a future deferred manager role. Older docs that say "the Harbormaster does X" should be re-read as "the Authority does X" or "the Auditor does X" depending on whether X is enforcement or observation.
- "**Worker**" (capitalized as a role) — replaced by **Dockworker**.
- "**fleet**" (in the old "all my Harbors" sense) — replaced by **Harbors** (technical) and **archipelago** (metaphor). **Fleet has a NEW meaning in V3**: the containers/ships within one DryDock.
- "**occupant**" — historical alias for what's now **Dockworker**.

---

## Mapping to code identifiers (V1 frozen, prose updated)

| Product term | Code identifier (frozen) | Notes |
|---|---|---|
| Harbor | host running `wsd`; no separate identifier | n/a |
| Harbor Authority | mostly `wsd` itself today (capability handlers, narrowness validation, bearer-token auth) | the role mostly exists; just needs the name |
| Port Auditor | not yet built; will combine deskwatch + harbors-monitor + new resource-anomaly detection | next-to-implement role |
| Harbormaster (manager) | deferred; would be a separate Dockworker on a special drydock with cross-Yard read access + orchestration RPC | not for V3 |
| Yard | not yet implemented | new; would be `yards` table + `yard_id` FK on Workspace |
| Project | `ProjectConfig` dataclass; `~/.drydock/projects/<name>.yaml` | unchanged |
| DryDock / Dock | `Workspace` class; `workspaces` SQLite table; `ws_<slug>` ids | renamable; deferred |
| Fleet | not yet first-class; today each Workspace has one container_id | will need `dock_containers` table for multi-Ship |
| Ship / Container | one row in (future) `dock_containers` table | n/a today; one container per Workspace |
| Dockworker | not first-class in code; represented by the agent process running in its Ship | n/a |
| Session | not first-class; implicit in any attached client | n/a |

---

## Why the layers earn their names

- **Archipelago vs Harbor.** Harbors are durable, address-stable, not mobile. "Archipelago" captures the static-islands shape better than "fleet" did. Archipelago is metaphor; "the harbors" is what you say in CLI/code.

- **Harbor vs Harbor Authority vs Port Auditor.** A Harbor is a *place* (the host). The Harbor Authority is the *role* that enforces rules at the gate. The Port Auditor is the *role* that watches whether declared activity matches actual. Splitting these from a single "Harbormaster" lets us implement what's needed first (Authority + Auditor — both narrow, both well-shaped) and defer the holistic-manager role (true Harbormaster) until there's orchestration that needs doing.

- **Yard vs DryDock.** A Yard groups DryDocks that share concerns (budget, network, secrets). Without a Yard, sharing is per-pair-declared and ceremonious. With a Yard, it's intrinsic. Optional layer — single DryDocks don't need it.

- **Project vs DryDock.** A Project is the *declaration* (IaC); a DryDock is the *instance*. Same shape as Class vs Object. Multi-Dock-per-Project is rare in V3 (typically 1 Dock per Project per Harbor).

- **DryDock vs Fleet vs Ship.** The DryDock is the *harness* (infrastructure around the work). The Fleet is *what's running* in the harness (one or more Ships). A Ship is *one* container/service. The DryDock holds a Fleet; the Fleet contains Ships. This corrects the V1/V2 conflation where DryDock was conflated with the single container running inside it.

- **Ship vs Dockworker.** A Ship is a *runtime artifact*; the Dockworker is *the agent that maintains it*. The Dockworker happens to live in one of the Ships (its agent container) but is conceptually separate from the Ship-as-runtime. One Dockworker maintains the whole DryDock's Fleet from inside one of its Ships.

---

## Implications worth calling out

### Harbor owns authority for resource enforcement
- Capability broker, audit emission, token issuance — all Harbor-level performed by `wsd`. The DryDock is *subject* to these; it doesn't make them.
- One `wsd` per Harbor. Multi-daemon single-host is out of scope.

### Harbor Authority + Port Auditor split — bucket-shaped action authority

Three buckets of action, each with a different authority shape:

**Bucket 1 — pure deterministic enforcement (Authority handles).** Capability gate, narrowness check, cgroup ceiling, lease expiry. No LLM; predictability + speed required. The Authority is mostly `wsd` as it already exists.

**Bucket 2 — defensive + reversible actions (Auditor can take directly).** Throttle egress, stop a Dock (state preserved, resumable), freeze storage (unfreezable), revoke a lease (re-mintable), shut down cloud resources (re-launchable). The Auditor's LLM judges *when* to act; the Authority's RPC methods do the actual enforcement (Auditor calls them with reasoning attached). **Reversibility is the constraint** — every Auditor action must be undoable by principal within a short window.

**Bucket 3 — destructive / expensive / irreversible (always escalate to principal).** Delete a database, destroy a Dock, provision new expensive compute (GPUs especially), modify policy files, rotate master credentials, spend above threshold. The Auditor flags + escalates; principal acts. Structural defense-in-depth: these RPCs reject the Auditor's bearer-token scope at the Authority's gate.

**Why give the Auditor bucket-2 authority instead of escalating everything:** the cost of an Auditor false-positive on a reversible action is *recoverable* (Telegram override lifts it in seconds). The cost of damage running unchecked while waiting for the principal is sometimes *not*. The asymmetry favors letting the LLM intervene defensively, structurally bounded by what it's allowed to touch.

**Why Bucket 3 stays principal-only:** material consequences with no recovery path. False positive on "delete the database" can't be undone. False positive on "provision $50K of GPUs" already cost the money. These are the actions where principal-in-the-loop is non-negotiable.

The principal is the manager. A formal "Harbormaster" manager role is a deferred future addition, not a V3 requirement.

### DryDock owns the harness; Ships own their isolation
- Network policy enforced at the DryDock harness (proxy, NETWORK_REACH grants). One change at the harness level affects all Ships.
- Each Ship is its own security boundary internally. Compromised Ship doesn't see siblings' state.
- Storage mounts, secrets, capability scope — all DryDock-level (apply to all Ships in the Fleet).

### Dockworker owns the work
- Maintains the Fleet (restart crashed Ships, tune schedules, decide work strategy).
- Proposes infrastructure amendments via broker (which the Harbormaster auto-applies or escalates).
- Communicates with sibling DryDocks via narrowed network reach.
- Does *not* have authority to modify its own DryDock's standing policy directly; goes through the amendment contract.

### Project is the IaC declaration
- One YAML per DryDock (or one Yard-level YAML covering several, depending on tooling).
- Lives at `~/.drydock/projects/<name>.yaml` today; could move to source repo (`.drydock/project.yaml` in the project's own repo) in a future Phase.
- Editable by principal directly; amendable by Dockworker via broker contract.
- Declares: fleet shape, narrowness, ceilings, infrastructure wiring, observability expectations.

---

## Versioning

- **V1** (~early 2026): Workspace as the only concept. No Project layer.
- **V2** (~mid 2026): Project YAML added. Capability broker. Narrowness. Pinning at create-time (inherited from spawn-time narrowness pattern).
- **V3** (2026-05-05 reframe, this doc): DryDock-as-harness vs Ship-as-runtime separation. Yard layer. Harbormaster as bouncer/auditor only. Multi-Ship Fleets. Container = security boundary, DryDock = resource boundary. IaC + agent-amendment contract.

V3 is the conceptual model going forward. Implementation hasn't fully caught up — the code still has many V1/V2 shapes (one-container-per-Workspace, Project-as-YAML-not-tracked, Harbormaster-as-not-yet-implemented). The implementation work is to migrate code to this model incrementally; the docs (this one and others) describe the *target* model so all future work points the same direction.
