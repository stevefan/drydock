# Yard — grouping of related DryDocks

**Status:** sketch · **Pulls from:** [vocabulary.md](vocabulary.md) §"The seven layers" layer 3, [project-dock-ontology.md](project-dock-ontology.md), [principal-harbormaster-governance.md](principal-harbormaster-governance.md)

## What a Yard is

A Yard is a grouping of related DryDocks that share concerns. The shape comes from the **progressive-differentiation pattern**: you start with one big "main" DryDock for a project, and over time individual services extract into their own smaller DryDocks. The Yard is the persistent home that provides continuity through that differentiation.

Without a Yard layer, sharing concerns across related Drydocks is per-pair-declared and ceremonious (every Dock independently lists every secret it shares with siblings, every domain it can reach in the others). With a Yard, those concerns are intrinsic to the grouping.

Yards are **optional**. A single standalone Drydock doesn't need a Yard. The Yard layer earns its keep when 3+ related Drydocks share substrate.

Maritime metaphor: a real shipyard contains multiple drydocks, often dedicated to a related set of ships (a class of vessels, a customer's fleet). The Yard provides shared infrastructure — power, materials, workforce — that any specific drydock can use without re-provisioning.

## What a Yard owns

- **Shared budget envelope** — collective resource cap across yard members. Member Drydocks draw from a pool rather than declaring per-Drydock budgets that sum.
- **Internal address space** — a yard-private network where member Drydocks can talk to each other by short name. Cross-yard requires explicit narrowness; intra-yard is permissive by default.
- **Yard-wide secrets pool** — secrets all members can request without per-Drydock declaration (e.g., a shared DB connection string, a common deploy key).
- **Yard-wide storage pool** — shared volumes member Drydocks can mount (e.g., a common cache, a shared dataset).
- **Collective metering** — Telegram summaries group by Yard ("microfoundry yard: 3 drydocks, 60% of token budget used").
- **Lifecycle coupling** — when desired, bring up the whole Yard, take down the whole Yard, snapshot the whole Yard.

What a Yard does NOT own:
- Code (each Drydock has its own worktree of its own repo or subdir)
- Per-Drydock policy (narrowness, capabilities — those are per-Drydock)
- The agents (each Drydock has its own Dockworker)

## When a Yard is the right shape

| Pattern | Yard or no? |
|---|---|
| 1 service in 1 Drydock | No yard — standalone is fine |
| 2 related services that share one secret | Probably no yard — per-Drydock declaration is fine |
| 3+ services that share budget + secrets + want internal network | **Yes — Yard pays for itself** |
| Monorepo with N services, each its own Drydock | **Yes — natural fit** |
| Distinct projects on same Harbor that don't share state | No — they're not related |

## How Yards relate to the other layers

```
Harbor
└── (governance roles: Authority + Auditor + future Harbormaster)
    └── Yards (multiple, optional)
        └── Yard "microfoundry"
            ├── Yard-shared substrate (the new layer)
            │   • shared budget
            │   • internal address space  
            │   • yard secrets pool
            │   • collective metering
            │
            └── Drydocks (members of the yard)
                ├── Drydock "main"
                ├── Drydock "auction-crawl"
                ├── Drydock "permits-pdx"
                └── ...
```

Standalone Drydocks (not in any Yard) live alongside Yards under the Harbor — they just don't get the shared substrate.

## Schema (proposed)

```sql
CREATE TABLE yards (
    id              TEXT PRIMARY KEY,           -- 'yd_<slug>'
    name            TEXT NOT NULL UNIQUE,       -- 'microfoundry'
    repo_path       TEXT NULL,                  -- optional: yard's primary repo (monorepo case)
    config          TEXT NOT NULL DEFAULT '{}', -- JSON: shared substrate fields
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

ALTER TABLE workspaces ADD COLUMN yard_id TEXT NULL;  -- FK to yards.id; NULL = standalone Drydock
```

The `config` JSON carries the shared-substrate declarations. Initially mostly empty (per-feature buildout):

```json
{
  "shared_secrets": ["microfoundry_db_url"],
  "shared_storage": [{"bucket": "microfoundry-shared", "mode": "ro"}],
  "shared_budget": {"anthropic_tokens_per_day": 2000000},
  "internal_network": "microfoundry-yard"
}
```

## YAML shape (proposed)

Yard YAML lives at `~/.drydock/yards/<name>.yaml` (parallel to `~/.drydock/projects/`):

```yaml
# ~/.drydock/yards/microfoundry.yaml
name: microfoundry
repo_path: ~/Unified Workspaces/microfoundry   # optional; monorepo case

shared_secrets:
  - microfoundry_db_url

shared_budget:
  anthropic_tokens_per_day: 2_000_000
  egress_bytes_per_day: 50Gi

internal_network: microfoundry-yard            # tailnet tag or docker network name
```

Project YAML opts into a Yard:

```yaml
# ~/.drydock/projects/auction-crawl.yaml
yard: microfoundry            # NEW field; opts into yard membership
workspace_subdir: services/auction-crawl
# ... rest of Project config (capabilities, narrowness, ceilings) ...
```

Existing Projects without a `yard:` field default to "no yard" — backwards-compatible.

## CLI surface (proposed)

| Command | Purpose |
|---|---|
| `ws yard create <name> [--repo <path>]` | Register a new Yard |
| `ws yard list` | List Yards on this Harbor |
| `ws yard show <name>` | Show Yard config + member Drydocks |
| `ws yard destroy <name> [--with-members]` | Remove Yard (refuses if members exist unless --with-members) |
| `ws yard add <name> <project>` | Add a Project to a Yard (sets the `yard:` field in its YAML) |
| `ws yard remove <name> <project>` | Remove a Project from a Yard |

## Implementation phasing

**Phase Y0 — primitive (this turn):** `yards` table, `yard_id` FK on workspaces, `ws yard create/list/show`, `yard:` field in project YAML wired through. NO shared-substrate features yet — Yards exist as a grouping with no functional difference from standalone Drydocks. Tests verify the schema, CRUD, and FK.

**Phase Y1 — shared secrets:** member Drydocks can request secrets declared at Yard level via the broker (`RequestCapability(SECRET, scope.source_yard=microfoundry)`). Capability-handler change.

**Phase Y2 — shared budget:** Authority's resource ceiling enforcement targets the Yard for Yard-budget-declared resources (anthropic_tokens, egress_bytes). Member Drydocks consume from the pool.

**Phase Y3 — internal network:** member Drydocks get tailnet tags or docker-network membership tying them to the Yard's internal address space. NETWORK_REACH within yard becomes permissive-by-default.

**Phase Y4 — collective metering / lifecycle:** Auditor groups by Yard in Telegram summaries; `ws yard up/down` brings whole Yards.

This doc is what unblocks Phase Y0. Y1+ build on top.

## Open questions

1. **Cross-yard policy default.** Should a Drydock in microfoundry Yard need explicit narrowness to call something in personal-tools Yard, or is cross-yard explicit-deny? Probably explicit-narrowness (same as cross-domain network reach today). Document explicitly.
2. **Yard YAML in source repo?** A Yard tied to a monorepo could have its YAML at `<repo>/.drydock/yard.yaml` instead of Harbor-local. Defers the same Phase 5 question from project-dock-ontology.md.
3. **Cross-Harbor Yards.** If microfoundry Yard exists on both Mac and Hetzner, are they "the same Yard"? Probably no — Yard is a Harbor-local grouping; cross-Harbor coordination is its own concern. But naming convention (microfoundry@mac vs microfoundry@hetzner) might matter.
4. **Yard authority for Auditor.** The Auditor (when implemented) presumably has read access across all Yards on its Harbor. Does it have *write* access (Bucket-2 actions like throttle) at Yard level too, or only per-Drydock? Probably both — throttling a Yard's egress affects all members.
