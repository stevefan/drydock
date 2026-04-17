# V2 Design — Vocabulary Reconciliation

**Purpose.** Pin the vocabulary V2 uses consistently across code, docs, and RPC surface. This is a narrow but load-bearing decision: it fixes how the daemon names what it owns, how the registry schema is organized, and how audit entries read.

## Current inconsistency

Three sets of terms coexist across the corpus:

| Source | Terms used |
|---|---|
| `vision.md`, `v2-scope.md` | "workspace" (technical artifact) / "agent-desk" (conceptual framing); used somewhat interchangeably |
| CLI + registry | `workspace`, `ws_<name_slug>` ids, `workspaces` SQLite table |
| Slip-box note `Drydock vocabulary - project desk session.md` | **Project → Desk → Session** as three distinct layers |
| Limitless transcript `Drydock — Agent Workspace Orchestration` | "workspace" as the durable unit |

The slip-box three-layer split is the most structured proposal and is foundational enough that several other design decisions (policy scoping, audit principal, RPC namespace) depend on which vocabulary V2 commits to.

## Decision

V2 adopts the three-layer model from the slip-box, with one concession to the v1 codebase: **"workspace" remains the technical-artifact identifier** (`ws_<name>`, `workspaces` table) because renaming is cosmetic churn and v1 has already shipped the term. At every other level — user-facing docs, daemon RPC surface, audit log — the three layers are named explicitly.

| Layer | Canonical name | What it is | What it owns |
|---|---|---|---|
| 1 | **Project** | YAML description + work-identity | Immutable config template (`drydock/projects/<project>.yaml`), repo path, policy template, declared capabilities and secrets. Multiple desks can derive from one project. |
| 2 | **Desk** | Concrete runtime instance — a durable addressable place | Container lifecycle, git worktree, policy scope, registry row (aka "workspace" technically), named volumes, audit principal, token, branch, accumulated state |
| 3 | **Session** | An attached client (agent or human) connected to a desk | Ephemeral. Multiple sessions may attach to one desk concurrently. Drydock does not own session state |

The v1 identifier `ws_<slug>` remains. Read it as **"workspace id"** = **"desk id"** — same thing.

## What each layer owns — implications

### Project
- Lives in YAML only; no runtime representation.
- Multiple desks per project is `ws create <project> <desk-name-2>`.
- `<desk-name>` defaults to `<project>` when omitted (common case: one desk per project).

### Desk (the daemon's primary entity)
- Policy scoping happens at **desk** level. Capability grants, firewall allowlists, delegatable-secrets lists, delegatable-firewall-domains — all keyed on desk id.
- Audit principal = desk id. "Desk `scraper-desk` requested `ebay.com` at 14:23" is the canonical log shape.
- Parent-child relationships live between desks. A parent desk spawns a child desk. Sessions don't spawn anything.
- Bearer tokens are issued per desk. Multiple sessions attached to the same desk present the same token.

### Session
- Explicitly **out of Drydock's scope.** Agent-to-agent coordination within a desk (two Claudes editing the same repo, pair-agent patterns, reviewer/actor) is a substrate/orchestrator problem, not a daemon problem.
- The daemon does not track session count, session attribution, or per-session policy. If two sessions on one desk both call `RequestCapability`, the daemon sees both calls as coming from the same desk with the same token.
- **Multi-user note (deferred):** when principals become explicit, a session carries a `principal_id`. The token → `(principal_id, desk_id)` lookup remains at desk-level; per-session policy is never added.

## Canonical phrasings

- "Spawn a desk" / "create a desk" — correct.
- "Spawn a session" — wrong; sessions attach, not spawn.
- "Desk X is a child of desk Y" — correct.
- "Workspace X" — acceptable in code and registry contexts; prefer "desk X" in user-facing docs.
- "Project microfoundry has three desks" — correct.
- "Session has a policy" — wrong; desk has the policy.

## Migration of existing docs

Low-priority follow-up, not V2 blocker:

- `vision.md` already introduces both terms. Amend to explicitly cite the three-layer split with a pointer here.
- `v2-scope.md` uses "desk" consistently in the V2 section. Fine.
- `getting-started.md` uses "workspace" throughout. Acceptable — that's a user-facing CLI doc and `ws` is the command.
- `secrets-design.md`, `secrets-roadmap.md` use "workspace" in `per-workspace` constructions. Keep — it's the v1 scoping keyword.

No rename of identifiers (`ws_<slug>`, `workspaces` table). Pure documentation alignment.

## Reversibility

| Decision | Cost of reversing | Notes |
|---|---|---|
| Three-layer Project/Desk/Session split | **Medium** | Once daemon RPC surface encodes the split (capabilities keyed on desk, audit principal = desk, tokens per desk), the layering is expensive to change. The split itself is load-bearing; the naming on top of it is cheap. |
| Keeping `ws_<slug>` as the technical id | Low | Pure naming; can alias later if needed. |
| Session as explicitly out-of-scope | Medium | If session-level policy becomes required (per-user secrets scoping, per-session audit), we'd need to extend the token model from desk-only to desk-plus-principal. Multi-user sketch already reserves this extension. |
