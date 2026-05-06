# Roadmap — May 2026 snapshot

**Status:** working roadmap · captures what's next after the V3 architectural reframe (2026-05-05) · revisable

This doc organizes the conversations and implementation work that are "on the table" after the V3 model landed in vocabulary.md. It's organized by **what kind of conversation each item is** (design vs implementation vs operational vs strategic) and **what's blocking what**. Each item has a recommended position where I have one.

The headline observation: V3 conceptual landing is *substantial* and the implementation lag is large. The most-leveraged near-term moves are **operationalizing what's already built** + **the Auditor LLM architecture conversation Steven flagged**. Deeper architectural moves (multi-Ship Fleet, backend abstractions, cloud commitment) are real but can wait until usage of what's built informs the design.

---

## ✓ Shipped this session (2026-05-05 → 2026-05-06)

| Item | Status | Commits |
|---|---|---|
| Phase 0 — `pinned_yaml_sha256` drift visibility | ✓ shipped | `2c06b6f` |
| Yard primitive (Phase Y0) | ✓ shipped | `36ef1a5` |
| PA0 measurement layer + `drydock auditor snapshot` | ✓ shipped | `184fd56` |
| Deadman switch + heartbeat + host-side telegram | ✓ shipped | `b3721b5` |
| PA1 watch_once + LLM client + watch.md prompt | ✓ shipped | `1e996fc` |
| PA1 daemon wrapper with adaptive cadence | ✓ shipped | `ecf8b7f` |
| PA2 deep analysis (Sonnet) on flagged events | ✓ shipped | `e781d3d` |
| Amendment contract A0 (schema + CRUD CLI) | ✓ shipped | `f031f13` |
| Amendment contract A1-conservative (capability handler side-effects) | ✓ shipped | `3081255` |
| Auditor V1 architecture decisions resolved | ✓ in vocabulary.md | `9c08a7d` |
| Vocabulary V3 anchor (Authority+Auditor+Yard+Dock+Fleet) | ✓ canonical | `e685b0f` |

Test count: started session at 590, now 773 (+183). All green.

**Implementation lag closed substantially.** Auditor architecture is end-to-end functional with API key + Telegram configured: cheap classifier every 1-15 min adaptive → on flag, Sonnet deep analysis → on `should_send_telegram`, principal Telegram alert with structured reasoning. Deadman fires if loop dies.

---

## Tier 1 — most-load-bearing, do first

### 1.1 Auditor LLM architecture (Steven explicitly flagged)

**What:** the design conversation about how the Port Auditor's LLM judgment layer actually works. `port-auditor.md` sketches a 3-layer architecture (Python measurement + Haiku watch loop + Sonnet/Opus deep analysis) but several decisions are open.

**Why now:** Steven explicitly said "we gotta talk more about what that is." Without this conversation, we can't implement the Auditor; without the Auditor, the bucket-2 defensive-action layer doesn't exist; without that, the V3 model is conceptual-only.

**Decisions to settle:**
- **Model selection.** Haiku 4.5 (or Haiku 3.5 — cheaper, less capable) for the watch loop? Sonnet 4.6 vs Opus 4.7 for deep analysis? The cost-vs-quality tradeoff is real and depends on Steven's spend tolerance.
- **Where prompts live.** Inline in Python (versioned with code, harder to iterate)? Separate prompt files (easier to iterate, harder to keep synced)? A claude-skills-style packaged set?
- **Watch-loop scheduling.** Cron-fired (`*/1 * * * *`)? Daemon-loop with internal scheduler? Event-triggered by audit-log activity (more responsive, more complex)?
- **OAuth-backup mechanics.** What's the failover trigger when the API key fails? How does the Auditor get OAuth credentials when the principal isn't around to do `claude /login`? (The auth-broker work from earlier might apply here.)
- **Tool-call surface.** I sketched a list (`read_metrics`, `escalate_telegram`, etc.) — is that the right granularity? Should there be one big tool with sub-commands, or many small tools?
- **Daily-summary cadence.** Once per day? Per-Harbor? Skip on quiet days? What's the format?

**What I bring:** the architecture sketch in port-auditor.md, the bucket model from vocabulary.md, the friction model from principal-friction.md, and concrete options for each decision with my preference + reasoning.

**What Steven brings:** preference / redirect on each open decision. Especially: spend tolerance, prompt-iteration ergonomics, "do I want to get woken up on Sunday for this?" type calibrations.

**Size:** one substantial conversation turn → unblocks several implementation phases. The implementation itself is multi-phase (PA0-PA4 in port-auditor.md); each phase is ~1-2 days of work.

**My recommended position:** start the conversation by walking through port-auditor.md section by section, surfacing the open decisions, and offering my recommended defaults so Steven can mostly say "yes" or redirect specific ones. Don't try to design from scratch in the conversation — refine an existing draft.

---

### 1.2 Operationalize what's built (Phase 0 + Yard primitive)

**What:** actually use the things that landed this session. Phase 0 (`pinned_yaml_sha256` drift visibility) and Phase Y0 (Yard primitive) are live in code. Pull them onto Hetzner, exercise them, see what they teach us.

**Why now:** these are the smallest things shipped most recently; whatever they reveal in real use should inform the next design conversations. If the drift display is confusing or the Yard primitive feels wrong-shaped, better to know now than after we build more on top.

**What to do, concretely:**
- Pull latest on Hetzner; reinstall pipx; restart daemon
- Run `drydock host audit` and see the drift surface against current Hetzner state (probably some Drydocks will show drift since YAMLs may have been edited)
- Try `drydock yard create microfoundry --repo /root/src/microfoundry`
- Try editing a Project YAML to add `yard: microfoundry` and see what happens
- Try `drydock project reload` on a drifted Dock; verify the SHA re-pins

**What this might surface:**
- Drift display is too noisy (several legacy Docks always-drift) → maybe filter by recency, or auto-pin once on startup for legacy rows
- Drift display doesn't catch what we actually care about → reconsider "hash of YAML bytes" vs "hash of expanded ProjectConfig"
- Yard primitive's CLI is awkward → adjust commands
- The error "Yard X doesn't exist, register first" is frustrating in practice → maybe auto-create Yards on first reference

**Size:** under an hour of operational time. Gets us back-pressure for the next design conversations.

**My recommended position:** do this BEFORE the deep Auditor LLM conversation. Live operational signal beats whiteboarding.

---

## Tier 2 — clear next implementation work

These are things where the design is settled enough to build; they just need turn-time.

### 2.1 Mechanical V3 vocabulary sweep across remaining design docs ✓ DONE 2026-05-06

**What was done:** Authority/Auditor split applied across `resource-ceilings.md`, `harbor-authority.md`, `vocabulary.md`, `auth-broker.md`, `project-dock-ontology.md`, `port-auditor.md`, `yard.md`, `employee-worker.md`. The umbrella position paper `principal-harbormaster-governance.md` was retired to `docs/design/archive/` since the V3 split made it incoherent — the per-feature docs now stand on their own. Code-comment cleanup in `src/drydock/core/resource_ceilings.py`. Stale "fleet" usages in vision.md, harbor-monitor.md, employee-worker.md updated to "archipelago." Remaining "Harbormaster" references in vocabulary.md/yard.md/port-auditor.md are intentional — they refer to the deferred manager role.

`narrowness.md`, `capability-broker.md`, `network-reach.md` were already clean — no Harbormaster references when checked.

---

### 2.2 Telegram bidirectional channel implementation

**What:** the actual code for the Auditor (or anything) to send + receive Telegram messages with structured response routing. The `tg.py` helper exists on Steven's collab Drydock; needs to be folded into `drydock-base` per the AGENT_NOTES.md "if a third workspace copies it" criterion.

**Why on the list:** every part of the V3 model that mentions principal escalation, Form A notes, or amendment review depends on this channel existing. Without it, escalations have nowhere to go.

**Decisions to settle:**
- **One bot per Harbor or one bot for the principal?** Per-Harbor means independent failure modes; per-principal means simpler chat UI.
- **Reply routing.** How does a `'approve auction-crawl'` Telegram reply route back to the right pending amendment? Need a correlation mechanism.
- **Threading.** Long incidents have many messages — should they live in a Telegram thread? Probably yes.
- **Out-of-band auth.** Bot tokens are credentials; how do they get rotated?

**Size:** ~1-2 days. Largely lifting tg.py + adding amendment-correlation routing.

**My recommended position:** wait until Auditor LLM design is settled (Tier 1.1), then implement together. The channel exists to serve the Auditor; designing them in isolation risks divergence.

---

### 2.3 Amendment contract Phase A0 (schema + envelope)

**What:** the `amendments` table + the basic CRUD. No auto-approval logic yet — every amendment goes to `pending` and requires manual `drydock amendment approve`. Proves the schema and audit shape before building the auto-gate.

**Why on the list:** generalizing the EGRESS_GRANTS prototype unblocks the IaC + agent-as-proposer pattern. Foundation for everything multi-author-IaC-shaped.

**Size:** ~1 day for the schema + minimal CLI.

**My recommended position:** do this *after* operationalizing Phase 0 + Yard (Tier 1.2). The amendment lifecycle benefits from existing capability handlers being audit-traceable, and seeing how Phase 0's drift surface lands tells us what amendment events should look like.

---

## Tier 3 — architectural decisions that gate further work

### 3.1 Yard Phase Y1+ shape (shared substrate features)

**What:** Y0 landed the Yard layer as a grouping primitive. Y1+ adds the actual shared substrate: shared budget, internal address space, yard-wide secrets pool, collective metering.

**Decisions to settle:**
- **Budget model.** A pool that members draw from, or a sum that members can borrow from each other against? Pool is simpler; borrowing is more flexible.
- **Internal network mechanism.** Tailscale tags (uses existing tailnet identity)? Docker bridge networks (Harbor-local only)? Both?
- **Yard secrets vs member secrets.** Are yard secrets a *separate* secret pool, or a delegation pattern (yard "owns" a secret, members request leases)?

**Why this is Tier 3 not Tier 1:** we just shipped Y0 minutes ago. Live testing (Tier 1.2) will inform what shared-substrate features actually pull their weight. Don't design Y1+ until we've operated Y0 for a bit.

**Size:** Y1 (shared secrets) is ~1-2 days. Y2 (shared budget) is more work because it touches Authority's enforcement logic. Y3 (internal network) might require Tailscale ACL changes.

**My recommended position:** defer Y1+ design until you've actually used Y0 against microfoundry-style real deployment. The pain points there will tell us which Y1+ feature to prioritize.

---

### 3.2 Multi-Ship Fleet refactor

**What:** today, one DryDock = one container. The V3 model says one DryDock = a *fleet* of containers (workers + API server + DB + agent, e.g.). This is a real schema change (`workspaces` table → `workspaces` + `dock_containers`) and lifecycle complexity (start fleet, stop fleet, restart-only-the-agent-Ship).

**Why this is Tier 3:** it's the single largest implementation in the V3 backlog. Doing it before the use case is concrete risks designing the wrong abstraction. The use case is *service extraction* (auction-crawl moves from collab Dock to its own multi-Ship Drydock with worker + API + DB + agent containers) — until that's actively happening, the multi-Ship abstraction is speculative.

**Decisions to settle:**
- **Compose-shaped declaration?** YAML's `fleet:` field would look like docker-compose's `services:`. Easy familiarity; risk of becoming docker-compose-shaped (large).
- **Lifecycle granularity.** Per-Ship restart? Whole-fleet down? Ship dependencies (start DB before workers)?
- **The agent Ship's special status.** It gets broker access; others don't. How is that distinguished in YAML?
- **State persistence.** Each Ship's volumes? Shared per-Drydock volumes? Both?

**Size:** ~1-2 weeks, careful work. Many touch points: registry schema, docker invocation, project_config parsing, overlay generation, lifecycle commands, audit shape.

**My recommended position:** **defer until first service extraction is actively wanted.** When you decide to extract auction-crawl from collab into its own Drydock, the question becomes concrete and we can design with real constraints. Until then, premature.

---

### 3.3 Service extraction pattern (the auction-crawl-from-collab example)

**What:** the operational pattern for taking a service that lives inside one big Drydock (like collab today) and promoting it to its own Drydock with its own narrow agent + own DB + own narrowness.

**Why on the list:** this is what V3 is *for*. It's also the thing that informs Tier 3.1 (Yard Phase Y1+) and Tier 3.2 (Multi-Ship Fleet) by providing real constraints.

**Decisions to settle:**
- **Migration path.** New Drydock with new Project YAML, then move data over? Or transform existing Drydock in place?
- **State handoff.** auction-crawl currently has SQL DB inside collab's container; how do we get it into its own bounded volume?
- **Agent handoff.** The "auction-crawl agent" today is just a sub-context of Steven's collab Claude; how does it become its own Dockworker with its own session continuity?
- **Inter-service comm setup.** microfoundry consumers need to know auction-crawl moved; tailnet hostnames + NETWORK_REACH declarations need updating.

**Size:** medium operational work, possibly with new tooling (`drydock extract <service-name> --from <source-dock> --to <new-dock-name>`?) if it needs to be repeatable.

**My recommended position:** wait until Steven actively wants to do this for one specific service (probably auction-crawl). Design from the concrete case rather than abstractly. Likely conversation: "OK I'm ready to extract auction-crawl, walk me through it" → that's when we figure out the pattern, possibly tool it.

---

## Tier 4 — bigger architectural pieces (real, not urgent)

### 4.1 Backend abstractions for cloud retargeting

**What:** the `EgressBackend`, `ComputeBackend`, `DNSBackend`, `StorageBackend` (already partial) protocol layer. Lets drydock target Linux+Docker (today), AWS (future), k8s (further future) without rewriting the model.

**Why this is Tier 4:** real architectural value but only matters when you actually want to run on a different backend. Steven's not committing to AWS yet.

**Decisions to settle:**
- **Which backends first?** EgressBackend for the proxy-on-Harbor work (closes the in-container-iptables anti-pattern). ComputeBackend for cloud-bursting (much later).
- **Protocol shape.** Plugin classes (current pattern)? Subprocess-based plugins (more isolation)? Out-of-process services?
- **Configuration.** Per-Harbor "I use these backends"? Per-Project? Per-DryDock?

**Size:** months of work spread out. Each backend is its own implementation.

**My recommended position:** defer until cloud retargeting is genuinely on the table. Phase 1.4 of every major doc is "cloud backends" — good to keep in the architecture's grammar but don't build prematurely.

---

### 4.2 Cross-Harbor coordination (peer RPC, archipelago-level operations)

**What:** drydock today is mostly Harbor-local. The `drydock harbors status` SSH-channel is the only cross-Harbor surface. Real archipelago-level operations (move a Drydock between Harbors, run an Auditor that sees across Harbors, sync Project YAMLs across Harbors) need more.

**Why this is Tier 4:** Steven has 2 Harbors today. Cross-Harbor coordination is a 5+ Harbor problem. The Tailscale + SSH channel is fine for now.

**Decisions to settle:**
- **Federation vs sovereign-peer (settled — sovereign per `project_peer_harbors_decision.md`).**
- **Specific cross-Harbor RPC channel.** Tailscale Funnel? mTLS over tailnet? SSH-shell continuing?

**Size:** sketchy until forced.

**My recommended position:** defer until concrete use case. Per the existing decision, no federation; cross-Harbor stays sovereign-peer.

---

### 4.3 Cloud platform commitment

**What:** the strategic question of whether/when drydock commits to AWS as the primary cloud target, vs staying platform-neutral.

**Why this is Tier 4:** real choice with real implications, but defer-able. Local Linux + Docker works for current scale.

**My recommended position:** drydock-as-spec, not drydock-on-AWS. Keep the abstractions clean enough that AWS is *a* backend, not *the* backend. When/if cloud-elastic compute becomes a real need, build the AWS backend then (it's a bounded project, ~weeks not months, with the abstractions in place).

---

## Tier 5 — long-horizon

These are real but distant. Listed for completeness so they don't get forgotten.

### 5.1 Code identifier rename (`Workspace` → `DryDock` etc.)

**What:** rename the V1 frozen code identifiers to match V3 vocabulary. Big refactor; CLAUDE.md previously called them frozen.

**Why this is Tier 5:** scope-explosive (every test, every fixture, every import). Real value (new contributors don't have to learn the historical mapping) but no urgent forcing function.

**My recommended position:** do as a focused refactor when there's appetite. Plan: rename in one big PR, exhaustive test pass, ship as a major version bump.

---

### 5.2 Project YAML in source repo (Phase 5 of project-dock-ontology.md)

**What:** today Project YAML lives in `~/.drydock/projects/` (Harbor-local). Phase 5 moves it to `<repo>/.drydock/project.yaml` (lives with the code). Closes the cross-Harbor drift problem.

**Why this is Tier 5:** real elegance, but the cross-Harbor drift problem isn't biting yet (Steven only has 2 Harbors and most projects only run on one).

**My recommended position:** revisit when cross-Harbor drift actually bites. Probably ~6 months out.

---

### 5.3 Drydock-as-product vs personal-infra

**What:** the strategic question of whether drydock wants to be packaged for other principals (other people) to use.

**Why this is Tier 5:** Steven has explicitly framed this as personal infrastructure. Productizing changes everything (multi-tenancy, security model, support burden). Not on the table for V3.

**My recommended position:** keep as personal infra. The design is sharper because it's one-principal-shaped. If someone else wants it later, they can fork.

---

## Dependency graph (what blocks what)

```
Tier 1.2 (operationalize Phase 0 + Y0)    ← do first; informs everything else
    ↓ teaches us
Tier 1.1 (Auditor LLM architecture)        ← do next; central to V3
    ↓ unblocks
Tier 2.2 (Telegram channel impl)           ← Auditor needs it
Tier 2.3 (Amendment contract A0)           ← Auditor consumes from this
    ↓ unblocks
Tier 3.x (Yard Y1+, multi-Ship, service extraction)
    ↓ informs
Tier 4.x (backend abstractions, cloud)

Tier 5.x (rename, source-repo YAML, productization) — independent of above
Tier 2.1 (V3 vocab sweep) — independent; do whenever
```

The critical path: **operationalize → Auditor design → Telegram + amendments → use it for a while → then bigger architectural moves.**

---

## What's NOT on this list (and why)

- **Bug fixes / observed issues from Phase 0 + Yard usage.** Will surface in Tier 1.2; not predictable in advance.
- **New capability types beyond NETWORK_REACH / SECRET / STORAGE_MOUNT / INFRA_PROVISION.** None are concretely needed yet; speculation.
- **Anthropic API model selection conversations beyond Auditor.** Other LLM uses (e.g., the principal's own Claude Code sessions) don't need new infrastructure.
- **Performance / scaling work.** Drydock is single-principal at ~10 Drydocks. No performance issue exists.
- **Security audits.** Worth doing eventually, but not until V3 implementation has settled.
- **Documentation reorganization.** The design docs are growing; might need an index doc later. Not now.

---

## Suggested next conversation order

1. **First (this week):** Tier 1.2 — operationalize Phase 0 + Yard. ~1 hour of operational work + observation.
2. **Next (after observing):** Tier 1.1 — Auditor LLM architecture. ~1 deep conversation turn.
3. **Following:** implement Tier 2.2 + 2.3 together (Telegram + Amendment A0). ~1-2 days of code.
4. **Then:** use the operational system for 2-4 weeks. Let real friction inform Tier 3.x.
5. **As needed:** specific Tier 3 items as they become concrete.
6. **Eventually:** Tier 4 + 5 as forcing functions appear.

This roadmap is meant to be revised. Update items, mark complete, re-prioritize. Snapshot date in the title is when the list was last fully reviewed.
