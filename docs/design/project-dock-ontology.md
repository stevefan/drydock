# Project ↔ Dock — ontology revisit

**Status:** position paper · revisit invited · **Pulls from:** [vocabulary.md](vocabulary.md), [narrowness.md](narrowness.md), [capability-broker.md](capability-broker.md), [principal-harbormaster-governance.md](principal-harbormaster-governance.md)

This doc is a deep look at the Project ↔ Dock relationship as drydock has it today, the assumptions that produced the current shape, the friction those assumptions are now generating in real use, and what a more first-principled design might look like. It's meant to be argued with, not adopted whole.

---

## 1. How we got here — history of the concept

The original drydock concept (V1) had no Project layer. There was just a **Workspace** (now called DryDock / Dock): a single durable bounded environment, created with `ws create`, parameterized with command-line flags. Each Workspace was its own snowflake — its devcontainer config, repo path, branch, capabilities all decided at create time and stored as registry state.

Three pressures pulled the Project layer into existence:

**(a) Reproducibility.** Steven kept creating Docks for the same kind of work — "I want a Dock for `auction-crawl` with these mounts and these env vars" — and the command-line surface was getting unwieldy. A YAML file ("the project config") let one declaration be replayed.

**(b) Source-controllability.** Putting that declaration in a file (and eventually in a git-versioned location) let policy/config evolve under git history rather than as undocumented operator memory.

**(c) The narrowness model arriving.** Once V2's capability-broker landed, declarative policy (`delegatable_secrets`, `delegatable_firewall_domains`, `capabilities`) needed a place to live that wasn't "we set it at one specific create-time and never see it again." YAML at a known location filled that role.

The Project was conceived as **a template that gets instantiated**, by analogy to a class-instance relationship in OOP. One Project = one YAML; many Docks of that Project = many runtime instances created from that template. The `ws create <project> <dock-name>` command syntax baked this in.

What did NOT happen alongside this conceptual move: the daemon never started tracking Projects as first-class entities. There's no `projects` table in the registry, no Project ID, no "this Project was edited at time T" audit event. The Project remained a *YAML file* on disk; the daemon's view of it is "a string key that happens to match a filename in `~/.drydock/projects/`."

This had a clear elegance: Projects are just files; you `ls ~/.drydock/projects/` to see them; you edit one with any text editor; you `git diff` to see what changed; nothing is hidden in a database. The cost of that elegance is the focus of this doc.

The pinning model (Project policy snapshotted onto the Dock at create-time) came in via a different chain: V2's narrowness invariant says "children are strictly narrower than parents, and post-spawn changes don't propagate." That's a *security* property — it defends against a compromised parent widening a child's authority retroactively. The Project → Dock relationship was *modeled* on the parent → child relationship from the narrowness work, even though the security threat doesn't quite fit (Projects are static YAML, not running adversarial parents). The pinning shape was inherited; the threat model behind it wasn't re-examined.

---

## 2. The current ontology, spelled out

### Project — what it is in code

`ProjectConfig` dataclass in `src/drydock/core/project_config.py`. Loaded by `load_project_config(name)` from `~/.drydock/projects/<name>.yaml`. Carries:

| Category | Fields | What they declare |
|---|---|---|
| Code pointer | `repo_path`, `workspace_subdir`, `devcontainer_subpath` | Where the source lives, which subdir (if monorepo), which devcontainer config |
| Network identity | `tailscale_hostname`, `tailscale_serve_port`, `tailscale_authkey_env_var`, `remote_control_name` | How the Dock presents on the tailnet |
| Static firewall | `firewall_extra_domains`, `firewall_ipv6_hosts`, `firewall_aws_ip_ranges` | What egress is open at container start |
| Per-Dock policy (the "narrowness" block) | `capabilities`, `delegatable_secrets`, `delegatable_firewall_domains`, `delegatable_storage_scopes`, `delegatable_provision_scopes`, `delegatable_network_reach`, `network_reach_ports` | What capabilities a Dockworker can request from the broker |
| Resource ceilings | `resources_hard` (Phase A) | Hard cgroup limits at container creation |
| Container shape | `extra_mounts`, `extra_env`, `forward_ports`, `image`, `claude_profile` | Container runtime config |
| Workload declarations | `storage_mounts`, `secret_entitlements` | Declarative S3 mounts; secret slots expected to exist |
| Observability | `deskwatch` | Health check expectations |
| Schedule | (separate `deploy/schedule.yaml` in repo) | Cron jobs |

These get parsed once at YAML load. **No daemon record exists for the Project**. The string `<project>` is the only handle.

### Dock — what it is in code

`Workspace` dataclass + `workspaces` table. Created by `ws create <project> [<dock-name>]` which:

1. Loads the Project YAML
2. Validates (KNOWN_KEYS, narrowness rules)
3. Generates a `desk_id` (`ws_<slug>` of the dock-name)
4. Inserts a `workspaces` row with: `id`, `name`, `project: <string>`, `repo_path`, `worktree_path`, `branch`, `state`, `container_id`, plus **the snapshot of the Project's policy fields** (`delegatable_*`, `capabilities`, `resources_hard`)
5. `git worktree add ~/.drydock/worktrees/<desk_id>/ ws/<dock-name>` — separate git worktree of the source repo, on its own branch
6. Generates an overlay devcontainer.json (combines base devcontainer + drydock additions)
7. Spawns the container via `devcontainer up`

After create, the Dock's policy is **frozen** — the `workspaces` row carries the values that were in the Project YAML *at the moment of create*. Editing the YAML afterwards has no effect until `ws project reload <dock>` is run, which re-reads YAML + re-pins onto the Dock's row + regenerates the overlay (but doesn't recreate the container; for that you need `ws stop && ws create`).

### The relationship

| Property | Project | Dock |
|---|---|---|
| Identity | YAML filename (`<name>.yaml`) | `desk_id` (UUID-like in registry) |
| Tracked by daemon? | No | Yes |
| Mutable? | Yes (edit YAML) | Yes (registry update) |
| Audit trail? | Git history of YAML (if checked in) | Audit log of state changes + leases |
| Lifetime? | Indefinite | Until destroyed |
| Carries running state? | No | Yes (container, leases, secrets, history) |
| Many of these per … | many Docks per Project | one Dock per (Project × name) |

### The "create" verb is doing a lot of work

`ws create auction-crawl prod` doesn't just spawn a container. It:
- Resolves the Project YAML
- Pins the Project's pinnable policy onto a new Dock
- Allocates a new git worktree (separate filesystem state)
- Allocates a new branch
- Allocates a Dock identity in the registry
- Allocates secrets dir, name volumes, tailnet identity
- Spawns a container

**There's no separate "register the Project" step.** The Project comes into the daemon's awareness only at the moment some Dock is created from it. If you write a YAML file at `~/.drydock/projects/auction-crawl.yaml` and never create a Dock from it, the daemon has no record it exists.

---

## 3. The tensions, explicit

Here's where the current ontology bends under real use. Each is a real friction point I or others have noticed:

### Tension A — Pinned vs Living policy

Background: Dock pins Project's policy at create. Editing the YAML doesn't propagate.

This was inherited from the narrowness model where pinning serves a security purpose (a compromised parent can't retroactively widen a child). But Project ≠ parent-Dock — Project is static YAML edited by the principal.

**The friction**: when the principal edits a Project YAML and wants the change live, they must run `ws project reload` on **every** Dock of that Project. For a Project with 5 Docks, that's 5 separate commands and 5 separate audit events. Forgetting one is silent — the forgotten Dock keeps running on stale policy.

**The cost is real but bounded**: most Projects today have 1-2 Docks. The principal mostly remembers to reload, and `ws project reload` is one of the simpler commands. The friction is annoying, not blocking.

**The deeper cost**: there's no easy answer to "what policy is Dock X currently running under?" without querying the registry directly. The YAML on disk is *not* canonical; the registry row is. Users (including the principal) tend to assume the YAML is canonical because it's source-controllable and visible. Mismatches between assumption and reality cause real bugs (like trying to debug "why doesn't this Dock have access to X?" by reading the YAML, not realizing the Dock was created before X was added).

### Tension B — Project as YAML-string-name vs Project as identity

Background: The daemon knows a Dock's project as a string. Renaming `auction-crawl.yaml` → `auction-crawl-v2.yaml` doesn't update the Workspace row. Worse: deleting `auction-crawl.yaml` doesn't notify anyone — the Docks of that Project keep running, and `ws project reload` on them fails because the YAML is gone.

**The friction**: Project as a name-pointing-at-a-file means edits/renames/deletes can silently break things. There's no "Project deleted while N Docks still depend on it" warning.

**The deeper issue**: the daemon can't answer questions like:
- "How many Docks are instantiated from this Project?"
- "When was this Project's YAML last edited?"
- "Are any Docks running policy that's drifted from the current YAML?"

All of these require either scanning every workspace row (for #1) or shelling out to git/stat (for #2/3). They could be one query each if Project were tracked.

### Tension C — Multi-Dock-per-Project lacks first-class differentiation

Background: One Project can have many Docks. The only way they differ today is by name (and name-derived branch + worktree path).

**The friction**: looking at `ws list`, you see `auction-crawl`, `auction-crawl-experimental`, `auction-crawl-test`. The relationship between these (they're all instances of the same Project, but for different *purposes*) is implicit. The daemon has no idea that `experimental` is the experimental fork and `test` is for one-shot test runs — that knowledge lives only in Steven's head and the Dock names.

**The deeper issue**: when the Harbormaster (or future automated systems) want to make decisions about "treat experimental Docks more leniently than prod Docks," there's no signal to distinguish them. We'd have to either parse Dock names heuristically or add a new field.

### Tension D — Cross-Harbor Project drift

Background: Project YAML lives in `~/.drydock/projects/` on each Harbor. If you have two Harbors and run "the same Project" on each, you have two YAMLs that you have to keep in sync manually.

**The friction**: Project YAML drift between Harbors is silent. Mac's `auction-crawl.yaml` could say one thing, Hetzner's another. Two Docks ostensibly of "the same Project" run different policies. There's no detection.

**The deeper issue**: Project as a Harbor-local file fundamentally conflicts with the multi-Harbor / archipelago model. The right place for Project YAML is probably *the project's own repo* (under `.devcontainer/drydock/project.yaml` or similar), not Harbor-local config. Drydock currently doesn't go this far — it copies/references the YAML in `~/.drydock/projects/` per-Harbor.

### Tension E — Project mixes orthogonal concerns

Background: One Project YAML carries: code pointer + network identity + firewall + per-Dock policy + resource ceilings + container shape + workload declarations + observability + schedule. Eight categories.

**The friction**: changing one category often shouldn't affect the others, but they all live in one file. Someone editing the deskwatch block has to scroll past all the policy. CI/policy review of the YAML conflates "this is a security change" (capabilities, narrowness) with "this is a mundane operational change" (deskwatch interval).

**The deeper issue**: the categories naturally split into:
- **Code identity**: what code, what subdir, what devcontainer
- **Policy**: capabilities, narrowness, ceilings (security-sensitive — should be principal-authored, audit-tracked)
- **Operational**: deskwatch, schedule, storage_mounts (less security-sensitive — could be more freely edited)
- **Infrastructure wiring**: tailnet, container shape, mounts (Harbor-specific concerns)

Bundling them means the principal can't grant "you can edit deskwatch but not capabilities" delegation — there's no surface for that.

---

## 4. The pinning question, in depth

This is the most load-bearing of the tensions. Worth a deeper look.

### Why pinning made sense for spawn

In the narrowness model, a parent Dock spawns a child Dock. The validator runs at spawn time: "child's requested scope ⊆ parent's delegatable scope." Once the child is spawned with its pinned scope, the parent narrowing later does NOT propagate to the running child. Why?

- A compromised parent shouldn't be able to retroactively widen the child (the spawn-time check is the security boundary).
- The child's authorities should be a stable contract for what the child believes it can do.
- Audit trails are clearer if "the child was granted X" happens at one timestamp, not continuously.

These are good properties for spawn-time grants between two distrustful parties.

### Why pinning is awkward for Project → Dock

Project is **not** an adversarial party. It's a YAML file written by the principal. The threat model "compromised Project widens Dock authority" doesn't apply — the Project is whatever the principal wrote. If the principal widens it, they meant to.

What pinning *does* defend against in the Project case:
- **Operator surprise**: "I edited the YAML to widen, but the Dock still rejects me — must be a bug." Pinning makes this explicit (you have to reload).
- **Stability mid-execution**: a Dockworker doesn't see its capabilities change underneath it during a running task.
- **Audit clarity**: "Dock's policy changed at time T because principal ran reload" is a recordable event; "policy continuously tracks YAML" doesn't have a single change-event.

These are real, but mild. The first is a UX concern (could be solved by a clearer status/diff command). The second matters mostly for long-running operations (and even then, only certain field changes matter). The third matters for forensics (and could be solved by hashing YAML + emitting an event when the hash diverges from the Dock's pinned hash).

### What pinning costs

- **Per-Dock reload friction**: N Docks × one reload each per Project edit
- **Authority ambiguity**: what's "real" — the YAML or the registry row?
- **Stale-Dock risk**: a Dock created six months ago might be running on policy that the YAML hasn't represented for five months
- **Cognitive load**: principal has to remember which mental model applies (pinned vs live) for each field

### The graduated alternative

A first-principles look says: not all fields have the same security profile, and not all benefit equally from pinning. Specifically:

| Field category | Should pin? | Reasoning |
|---|---|---|
| Capabilities (`request_*_leases`) | **Pin** | Adding a capability is a security expansion; should require explicit reload event |
| `delegatable_*` (narrowness) | **Pin** | Same — widening attack surface, principal-explicit action |
| `resources_hard` (cgroup ceilings) | **Pin** | Lowering a ceiling on a running container can't be enforced without recreate; pinning makes this honest |
| `network_reach_ports` | **Pin** | Same as capabilities |
| `firewall_extra_domains` (static allowlist) | **Live** (with caveat) | Adding a domain is additive; could propagate. But removing one needs container restart anyway |
| `deskwatch` block | **Live** | Pure observability config; no security implication; convenient to edit and have take effect |
| `extra_env` | **Pin** | Affects container startup; would need recreate to take effect anyway |
| `extra_mounts`, `forward_ports` | **Pin** | Same — needs recreate |
| `tailscale_*` | **Pin** | Same |
| `storage_mounts` | **Pin** | Implies capability + scope grants; security-relevant |
| Schedule (separate file) | **Live** | Cron entries; sync runs separately |

The pattern: things that *need a container recreate to take effect anyway* are de-facto pinned by the substrate. The choice exists for things that COULD propagate but currently don't (notably deskwatch, possibly firewall_extra_domains).

This suggests a cheap first move: **only deskwatch becomes live**. That single change resolves a real friction (editing deskwatch + having to reload + having to potentially recreate) without touching any security-relevant pinning behavior.

---

## 5. Downstream consequences of the current model

Where the current Project = YAML model leaks into machinery:

### `ws project reload` exists as its own command

Tellingly: this command is its own thing, not a flag on something else. That's evidence the friction is real enough to deserve a verb. In a fully-living-policy world, `reload` wouldn't exist — YAML edits would just take effect.

### Audit log records Dock-level changes only

When `ws project reload` runs, the audit log gets `desk.policy_updated` events. The *Project YAML edit itself* is not audited (only its effects via reload). To answer "when was this Project's policy last changed," you have to git-log the YAML or check the most recent reload event across all the Project's Docks. Asymmetric.

### `ws inspect <dock>` shows pinned values, not Project drift

If the Dock is on stale policy, `ws inspect` shows the stale state. There's no flag like "this Dock is 3 reloads behind the current YAML." The Dock looks fine.

### `ws host audit` (this session's addition) doesn't surface Project staleness

The audit shows per-Dock policy but doesn't compare it to current Project YAML. Could — would need: read YAML, hash it, compare to a `pinned_yaml_sha` column on the Dock that doesn't exist yet.

### `ProjectConfig` dataclass is parsed at create AND reload, never persisted

The code path for "load Project YAML, parse, validate, send to daemon" exists in two places (create.py and project.py reload). The daemon then snapshots into the Workspace row. The dataclass itself is ephemeral — every load re-parses. That's fine for performance but means the YAML *content* has no canonical in-memory representation in the daemon between operations.

### Cross-Harbor Project drift has no detection

The Mac's `auction-crawl.yaml` and Hetzner's could diverge silently. There's no command like `ws project diff <project> --across-harbors`. If a principal manually copies a YAML from Mac to Hetzner and forgets one field, the two Harbors' Docks of that Project run different policies.

### `ws schedule sync` is the closest existing analogue to "live"

`ws schedule sync <dock>` reads the Project's `deploy/schedule.yaml`, syncs to Harbor cron/launchd. Note this is NOT pinned — it pulls from the source on every sync. The schedule is *operationally* live, just gated behind an explicit sync command. A future "live deskwatch" would look like the same pattern (or even more automatic).

### Tests that touch ProjectConfig assume a YAML on disk

This is fine but does mean tests have to write YAMLs to tmp dirs to exercise create-flows. A daemon-tracked Project entity could be exercised more directly.

---

## 6. First-principled alternative

Imagine designing this from scratch with everything we know now. The cleanest shape I can construct:

### Project as a daemon-tracked entity

A `projects` table:

```sql
CREATE TABLE projects (
    id              TEXT PRIMARY KEY,           -- 'pj_<slug>'
    name            TEXT NOT NULL UNIQUE,       -- 'auction-crawl'
    yaml_uri        TEXT NOT NULL,              -- 'file:///root/.drydock/projects/auction-crawl.yaml'
                                                --   or 'git+https://github.com/.../auction-crawl#main:.devcontainer/drydock/project.yaml'
    yaml_sha256     TEXT NOT NULL,              -- hash of the YAML content most recently loaded
    parsed_config   TEXT NOT NULL,              -- canonicalized JSON of the parsed config
    registered_at   TEXT NOT NULL,
    last_loaded_at  TEXT NOT NULL,
    last_modified   TEXT NOT NULL               -- mtime / git-commit timestamp
);
```

Projects are explicitly registered: `ws project register <name> --from <uri>`. Future Docks of that Project use the registered entity. The YAML URI can be a local path (today's behavior) or a git URL (future: project YAML lives in the project's own repo, single source of truth).

### Per-field bind classification

Project schema declares which fields are PINNED vs LIVE:

```yaml
# In the Project YAML's metadata
binding:
  pinned: [capabilities, delegatable_*, resources_hard, extra_env, extra_mounts,
           forward_ports, tailscale_*, storage_mounts, network_reach_ports]
  live:   [deskwatch, firewall_extra_domains]   # additive only — removals need restart
```

Default is PINNED (safe). Authors opt fields into LIVE explicitly; opting in says "I accept that edits to this field take effect on running Docks without explicit per-Dock action."

The daemon tracks each Dock's `pinned_yaml_sha256` — the hash of the Project YAML at the moment the Dock was created OR last reloaded. When a LIVE field changes (detected by hash diff), the daemon reads the new value, updates the Dock's effective config, emits an audit event. When a PINNED field changes, the daemon emits a "drift detected" warning that the principal can act on with `ws project propagate <project>` (which reloads all of that Project's Docks atomically).

### Dock has a `purpose` field

Free-form for V1, possibly enum-ified later:

```sql
ALTER TABLE workspaces ADD COLUMN purpose TEXT DEFAULT '';
-- Values: 'prod', 'experimental', 'scheduled-jobs', 'sandbox', 'ephemeral', or any string
```

Set at create: `ws create auction-crawl prod-main --purpose prod`. Surfaces in `ws list`, `ws inspect`, `ws host audit`. The Harbormaster can use it for differential treatment ("experimental Docks get tighter resource ceilings by default").

### Project edit/audit/propagate workflow

```
$ ws project edit auction-crawl
# (opens YAML in $EDITOR; on save, validates + computes new hash)
# Validation passes → updates projects table → emits 'project.edited' audit event
# Lists Docks affected; suggests `ws project propagate auction-crawl` to apply pinned changes

$ ws project status auction-crawl
# Shows: current YAML hash, count of Docks pinned to current vs older hashes,
# count of Docks with live-field deltas already applied

$ ws project propagate auction-crawl [--dry-run]
# Re-reads YAML for each Dock of this Project, updates pinned values atomically,
# emits 'desk.policy_updated' per Dock, regenerates overlays.
# (Does NOT recreate containers — same as today's reload.)
```

### What this composes with

- **Harbormaster**: can subscribe to `project.edited` audit events; can auto-propagate live fields; can escalate to principal when pinned fields drift ("auction-crawl Project edited; 3 Docks pinned to old policy — propagate?").
- **`ws host audit`**: surfaces project staleness per Dock cleanly (`pinned_yaml_sha` column directly comparable).
- **Cross-Harbor**: if Project YAML lives in the project's own git repo (yaml_uri = git URL), drift between Harbors is reduced to "did each Harbor pull the latest commit?" — same problem as code drift, same solution.

### What changes for code

| Component | Change |
|---|---|
| `core/project_config.py` | Add `binding` block parsing; expose pinned vs live field lists |
| `core/registry.py` | Add `projects` table; add `pinned_yaml_sha256`, `purpose` columns to `workspaces` |
| `cli/project.py` | Add `register`, `edit`, `status`, `propagate`; keep `reload` as a thin alias for `propagate <one-dock>` |
| `wsd/handlers.py` | Add `RegisterProject`, `PropagateProject` RPCs; `CreateDesk` now validates against a registered Project (with auto-register fallback for backwards compat) |
| `cli/create.py` | Auto-registers Project if not already; accepts `--purpose` flag |
| `core/host_audit.py` | Surfaces per-Dock pinned-vs-current YAML drift |
| `wsd/recovery.py` | On startup, scan `~/.drydock/projects/` and ensure each YAML has a `projects` row (one-shot migration) |

Backward compatibility:
- Existing Docks have no `pinned_yaml_sha256` — treat as "drift unknown," trigger first reload to record current sha
- Existing Project YAMLs without `binding` block default to all-PINNED (current behavior)
- The string `project: <name>` field on Workspace rows continues to work; daemon resolves to `projects` table by name

### What I'm deliberately NOT recommending

- **Repo as a layer above Project.** Could be useful for monorepos but isn't pulling its weight at current scale. Adding it requires schema migration, new commands, more vocabulary. Defer until monorepo pain is concrete.
- **Splitting Project into Project + Policy + Infrastructure.** Tension E says these are orthogonal, but splitting them adds three concepts where one currently suffices. The principal isn't drowning in YAML complexity yet. Defer until the volume of YAML-per-Project is actually painful.
- **Full Project lifecycle (`ws project archive`, `ws project clone`).** Nice to have eventually; not needed for the core ontology fix.
- **Renaming "Project."** Steven asked about this earlier; my read remains: "Project" is generic but familiar. The cost of renaming is high and the benefit is small. Keep.

---

## 7. The path I'd actually recommend, in order

If you wanted to land this incrementally rather than all-at-once:

**Phase 0 — make staleness visible (smallest possible step).**
- Add `pinned_yaml_sha256` column to `workspaces` (defaulting to '').
- `ws create` and `ws project reload` both write the SHA at the moment of pin.
- `ws host audit` surfaces per-Dock "policy SHA matches current YAML?" — yes/no/unknown.
- Cost: tiny (one column, one hash function call, one audit-output addition).
- Value: closes the silent-drift problem. You can see what's stale without needing to know.

**Phase 1 — make Project a registered entity.**
- Add `projects` table.
- `ws create` auto-registers if Project not yet known.
- `ws project list` and `ws project status <name>` work.
- Cost: schema migration + a few CLI commands.
- Value: enables "which Docks of this Project?" queries; foundation for everything else.

**Phase 2 — `ws project propagate <project>`.**
- Reloads all Docks of a given Project atomically; emits per-Dock audit.
- Replaces the per-Dock reload-loop pattern.
- Cost: one new RPC + CLI command, modest.
- Value: closes the N-Dock-reload friction without changing pinning semantics.

**Phase 3 — per-field bind classification.**
- Project YAML opts fields into LIVE; default remains PINNED.
- Daemon detects YAML-hash changes (file watcher or polling) and applies LIVE field updates automatically.
- Cost: file watcher infrastructure or polling logic; per-field handling.
- Value: removes friction for the operationally-mundane fields (deskwatch first); preserves security pinning.

**Phase 4 — `purpose` field on Dock.**
- Free-form string at create time; surfaced in inspect/audit.
- Could happen alongside any of the above phases — independent.
- Cost: one column + one CLI flag.
- Value: enables differential treatment (Harbormaster Phase C onwards).

**Phase 5+ (deferred, only if needed):**
- Project YAML in project's own repo (git URI).
- Cross-Harbor Project sync.
- Repo-as-a-layer.
- Project + Policy + Infrastructure split.

---

## 8. The honest summary

The current Project ↔ Dock ontology was built by analogy to the parent-child spawn relationship from the narrowness model, but the analogy doesn't fully fit (Projects aren't adversarial parents). The result is a model that's elegant in its simplicity (Project = YAML file) but generates real friction in operation (per-Dock reloads, silent drift, no Project-level audit trail).

The most leveraged single change would be making YAML drift visible (Phase 0). The most architecturally coherent move is the full sequence above, which preserves the security properties of pinning where they matter while removing it where it's just friction.

The least coherent thing in the current model is the **interaction between** "Project as YAML on Harbor-local disk" and "the archipelago model." If we genuinely commit to multi-Harbor as the steady state, Project YAML in the project's own repo (Phase 5) becomes the right answer — but that's a deeper refactor that's worth deferring until cross-Harbor Project drift is something Steven has actually been bitten by, not just something we can imagine.

The conservative recommendation: ship Phase 0, see if it changes how you operate, then Phase 2 if reload-friction is still annoying. Phase 1, 3, 4 add real value but aren't urgent.

The aggressive recommendation: ship Phase 0-3 as one cohesive V2-of-Project work, on the bet that the per-field bind classification is what unlocks the Harbormaster's autonomous-operation story (Harbormaster can apply LIVE changes without principal involvement, escalates only on PINNED drift).

Either is defensible. The current model is also defensible — it's just generating friction that someone (you or the Harbormaster) will absorb.

---

## Open questions

1. **Should Project YAML live in the project's own repo by default?** If yes, the cross-Harbor drift problem largely dissolves but `ws create` needs to clone-or-pull to get the YAML.
2. **What's the right granularity for `purpose`?** Free-form string risks proliferation (40 unique values across 50 Docks); enum risks not capturing real usage. Could start free-form, observe, enum-ify.
3. **Should the Harbormaster have authority to auto-propagate LIVE field changes, or always require principal action?** Current `principal-harbormaster-governance.md` §6 says policy mutation is static V1 (principal hand on policy file). LIVE field auto-application is in tension with that — it's the daemon applying YAML changes without explicit principal action per Dock. Probably consistent (principal still authored the YAML edit), but worth saying explicitly.
4. **Do we need a `Project` audit principal?** Today audit tracks `desk_id`. If Project edits become first-class events, they need their own principal-id; probably the human principal directly (no Project-level agent in the trust model).
