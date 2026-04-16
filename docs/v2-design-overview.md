# V2 Design — Overview and Reading Order

**Status.** Design layer on top of `v2-scope.md`. Written 2026-04-14. Extends where `v2-scope.md` left questions open; amends where design surfaced a better answer. Not a spec — see `v2-scope.md` for the shipping spec.

**Codex review complete, post-review simplification applied (2026-04-14).** See §Codex review below.

**Further scope trim (2026-04-14, post-Steven review).** Budget narrowness, live `ws reconcile`, `ws adopt`, 24h auto-renew, HTTP transport, and `RotateDeskToken` were removed from V2 as premature-for-forcing-function. All reserved in design; reinstate when demonstrated need surfaces. See §Decisions deliberately deferred.

**Tailnet identity lifecycle ADDED to V2 (2026-04-15, Steven sign-off).** Daemon authoritatively cleans up tailnet device records on `DestroyDesk` and exposes `PruneStaleTailnetDevices` for orphan cleanup. Justified by the same "forcing function surfaced" criterion the trim line uses — auction-crawl Mac→Hetzner deployment 2026-04-14 broke identity continuity (canonical hostname `auction-crawl` held by an offline ghost; new desk took `auction-crawl-1`). Bounded scope: one new admin RPC, two audit events, daemon-level admin token (NOT a per-desk capability — see design doc §3 for why). See [v2-design-tailnet-identity.md](v2-design-tailnet-identity.md). Steven explicitly signed off on inclusion in V2 over a v1.x backport.

**Secrets backend default CONFIRMED: file-backed (2026-04-16, Steven sign-off).** V2.0 ships with `FileBackend` as the only concrete `SecretsBackend` implementation, per `v2-design-capability-broker.md` §7. Plugin protocol reserved; 1Password, Vault, cloud secret managers are additive future backends, no RPC changes required. Rationale: personal-fleet scale doesn't yet demand centralized rotation; 1P would require bootstrapping a service-account token per host (new secret-to-transport chicken-and-egg); file-backed inherits Phase 1 conventions that already work. `wsd.toml` should accept `[secrets] backend = "file"` (default) with future values rejected as `unknown_secrets_backend` until they ship. Drydock-employee patterns that need centralized rotation are V2.1+ (via additional backend) or cross-desk capability delegation in V3 — not a V2.0 blocker.

## Reading order

Read in this order; each doc assumes the ones before it.

1. **[v2-design-vocabulary.md](v2-design-vocabulary.md)** — Project / Desk / Session. Everything else uses these terms.
2. **[v2-design-protocol.md](v2-design-protocol.md)** — RPC surface, wire transport, auth model, daemon location, `ws attach` routing.
3. **[v2-design-capability-broker.md](v2-design-capability-broker.md)** — Lease data model, policy validator as pure function, narrowness rules, revocation, plugin protocol for secret backends.
4. **[v2-design-state.md](v2-design-state.md)** — State ownership (SQLite, daemon memory, host paths, container). Crash recovery. Devcontainer-CLI error handling. V1 coexistence contract.
5. **[v2-design-tailnet-identity.md](v2-design-tailnet-identity.md)** — Tailnet device-record lifecycle (the daemon-side complement to per-node `tailscale logout`). Daemon-level admin credential, `PruneStaleTailnetDevices` admin RPC, `tailnet.*` audit events. Added 2026-04-15 with Steven sign-off after auction-crawl deployment surfaced the identity-continuity gap.

Earlier drafts included a `v2-design-vault-bridge.md`. It was cut: vault-mount narrowness is now handled by the generic mount-narrowness rule in capability-broker §4 (rule 4); the vault-bridge reconciliation desk is not a daemon concern. Concept doc remains in the slip-box at `~/Notebooks/commonplace/slip-box/Vault bridge - when two substrates earn their complexity.md`.

## How this relates to `v2-scope.md`

`v2-scope.md` says **what** V2 ships. These docs pin down the **how** for the parts that were open:

| `v2-scope.md` point | Extended in |
|---|---|
| "Desks as first-class entities" | vocabulary (Project/Desk/Session layering), state (ownership split) |
| "Policy graph with enforced narrowness" | capability-broker (validator contract + rules) |
| "Bearer-token auth over Tailscale" | protocol (Unix socket transport, per-principal generalization reserved; HTTP transport and `RotateDeskToken` deferred to V3) |
| "Secrets broker (replaces V1 static mount)" | capability-broker (generalized lease shape) |
| §Open questions 1–5 | All addressed: daemon location (protocol), devcontainer errors (state), attach routing (protocol), revocation on policy change (capability-broker), pluggable backends (capability-broker) |
| Migration from V1 | state (V1 coexistence contract, destroy+create on-ramp, failure modes) |
| "Tailnet identity lifecycle" (added 2026-04-15) | tailnet-identity (admin-token storage, destroy-time device delete, `PruneStaleTailnetDevices`, audit events extending state §1a) |

## Current-state summary (repo, 2026-04-14)

**Branches (both merged to main):**
- `ephemeral-container-lifecycle` → 3 commits (`d942a6b`, `d1b1020`, `82680a8`). Thin-runtime / thick-volumes semantics.
- `ws-secret-hardening` → 1 commit (`ee4b5f8`). Atomic writes + TOCTOU-safe rm.

**Open from the 2026-04-14 session log (pre-V2 gate):**
- **Volume-preservation regression test is still scope-reduced.** `tests/cli/test_lifecycle.py:95` (`test_force_rebuild_preserves_checkout`) covers **checkout** preservation, not **named-volume** preservation via `extra_mounts`. The session log explicitly flagged this as "Fix before merging the branch" — the branch merged but the test didn't.
  - Not blocking V2 *design*.
  - **Is** blocking V2 *implementation*: the daemon will inherit the thin-runtime / thick-volumes contract. If V1 has no regression test here and V2 introduces a teardown bug, we can't catch it.
  - See `v2-design-state.md` §6 and the verification checklist below.

**Other open items from session log, lower priority:**
- `WsError.to_dict()` JSON is bimodal when `code` is set. No consumers yet. Tolerable; stabilize before V2 adds new daemon-side error codes.
- Ephemeral-container lifecycle is tested only via mocks. No real-devcontainer integration test. Pre-existing v1 gap; not V2-specific.

## Reversibility digest

Items marked **HIGH** = get it right the first time; changing after V2 ships is breaking or a rewrite. **MEDIUM** = changeable with work. **LOW** = additive/painless.

**HIGH — hardest to reverse:**
- Capability-lease shape (`{lease_id, desk_id, type, scope, issued_at, expiry, issuer, revoked}`). V3/V4 extend; wrong field set = breaking change.
- `scope: dict` unversioned. V4 evolution of future type shapes has no formal compatibility path. V2 mitigation: append-only-per-type convention; first V4 type defines versioning model.
- Purity of `validate_spawn`. Test-enforced invariant.
- "No host-specific state in containers." V3 migration depends on it.

**MEDIUM — changeable but painful:**
- JSON-RPC 2.0 wire protocol.
- Bearer-token auth (vs. mTLS — additive, but removing bearers is breaking).
- Token subject model (`token → desk_id`). V3 multi-user extends additively but all consumers participate.
- `CreateDesk`/`SpawnChild` as distinct methods.
- `RequestCapability` subject derived from token (trust-model contract).
- `RequestCapability` as single-entry-point for leases.
- Secrets-as-capability-type (not separate API).
- Vocabulary (Project/Desk/Session).
- File-backed lease materialization at `~/.drydock/secrets/`.
- Audit event schema (event names + required `details` keys).
- Exact-string-only firewall domains (no wildcards).
- Domain canonicalization (IDNA/punycode) in the validator's pure module.
- Mount narrowness as a validator rule.

**LOW — additive/internal/painless:**
- SQLite as primary store.
- Task log persistence.
- Additive registry columns.
- Daemon opt-in (V1 coexistence).
- Reserved V4 enum names; YAML does not yet accept them.
- `ws attach` bypasses daemon.
- Structured `Reject` shape.
- Destroy+create as v1→v2 on-ramp (no `ws adopt`).
- Unix socket only (HTTP transport reserved for V3).

## Thin-slice implementation plan

Thinnest end-to-end first, then layer.

**Slice 1: `CreateDesk` over RPC, no policy.** Daemon accepts `CreateDesk(spec)`, executes exactly as V1 does today, returns `DeskRef`. Unix socket only. No policy validator, no leases, no nested spawn.
- **Proves:** daemon shape, wire protocol, state-ownership boundary, task-log-based crash recovery.

**Slice 2: `SpawnChild` + policy validator.** Desk-mode CLI gets a bearer token, authenticates, calls `SpawnChild`. Validator runs all 4 narrowness rules (firewall, secret, capability, mount). Parent-child cascade on destroy.
- **Proves:** trust boundary. Narrowness tests pass.

**Slice 3: Secrets broker (Phase 2 migration).** `RequestCapability(type=SECRET, ...)` returns leases with `expiry: None` (live until desk destroy or release). File-backed Phase 1 secrets migrate into daemon-managed store.
- **Proves:** capability-lease pattern end-to-end. Revocation-on-destroy. Finite-expiry + auto-renew machinery NOT exercised here; reserved for V4.

**Slice 4: Audit surface.** `GetAudit` streams structured events. Daemon writes to JSONL.
- **Proves:** observability boundary; closes V2's scope list.

**Not in V2:**
- V3 migration, suspend/resume, fleet-aware daemon.
- V4 cloud capability types (`STORAGE_MOUNT`, `COMPUTE_QUOTA`, `NETWORK_REACH`) — enum-reserved only, YAML rejects.
- Multi-user per-principal tokens (reserved in auth model, not implemented).
- Vault-bridge reconciliation desk (not a daemon feature).
- Vault-specific capability type or YAML — vault mounts go through generic `extra_mounts` with narrowness rule 4.

## Verification checklist — before writing first daemon line of code

- [ ] Volume-preservation regression test closed on main (v1 pre-req). See `v2-design-state.md` §6.
- [ ] This design reviewed by Codex (independent pass). Done 2026-04-14.
- [ ] This design reviewed by Steven.
- [ ] Vocabulary doc accepted — touches naming in RPC types, registry, audit.
- [ ] Capability-lease shape accepted — drives SQLite schema, RPC types.
- [ ] Policy validator contract accepted — drives every `SpawnChild` path's test suite.
- [ ] State-ownership split accepted — drives what goes in SQLite vs. daemon memory.
- [ ] V1 coexistence contract accepted — drives the destroy+create on-ramp and daemon-opt-in fallback.

## Decisions deliberately deferred

- **Resource-budget narrowness** (validator rule 4 in earlier drafts). V2 single-monorepo, few-children forcing function doesn't exercise it. Reinstate in V3 if fleet scale or shared-resource contention surfaces.
- **Live `ws reconcile` on parent policy change.** V2 flow is narrow → destroy → respawn. Live per-child cascade deferred to V3.
- **`ws adopt` / `ws disown`.** V1 → V2 on-ramp is destroy+create. Simpler, same outcome; revisit if an in-place upgrade becomes necessary.
- **24h auto-renew on SECRET leases.** V2 ships `expiry: None` (matches Phase-1 file-backed semantics). Finite-expiry + auto-renew machinery reserved for V4 cloud credentials where rotation is load-bearing.
- **HTTP/Tailscale transport.** V2 ships Unix socket only. HTTP reserved for V3 multi-host; same JSON-RPC envelope means the addition is additive.
- **`RotateDeskToken` method.** Reserved in the design; no V2 caller. Ship in V2.5 if operational need surfaces.
- **Audit storage format** (JSONL vs. SQLite table). Default JSONL (v1 convention); revisit if query shape demands.
- **Per-lease file-mount vs. single `/run/secrets/` dir materialization.** V1 uses single dir; keep. Revisit if dynamic lease adds/removes surface friction.
- **`wsd.toml` config reload mechanism.** Default: restart daemon.
- **`scope_version` field** on `CapabilityLease`. First real V4 type defines the versioning model.
- **Admin impersonation method** (`AdminRequestCapability`). No V2 callers; defer until needed.
- **Vault-bridge reconciliation desk.** Hit wall, then build.

## Codex review — findings and resolutions (2026-04-14)

Independent pass by Codex focused on reversibility, V4 forward-compat of the capability broker, and narrowness invariants. Findings below; design simplified post-review.

### Correctness (trust-boundary) fixes — addressed in-doc

| Finding | Resolution |
|---|---|
| Validator rule 5 referenced scoped entitlements that weren't in `DeskPolicy`. Biggest trust-boundary hole | Simplified: rule 5 is now generic mount narrowness (`child.extra_mounts ⊆ parent.extra_mounts`). No scoped-capability-scopes machinery in V2 — only one implemented capability type (SECRET) whose narrowness is already covered by rule 2 |
| Narrowness only ran on `SpawnChild`; a desk could request broader leases via `RequestCapability` | Post-spawn narrowness is now a trivial lookup: `RequestCapability` checks `requested_secret ∈ desk.secret_entitlements`, already pinned by `validate_spawn`. No parallel validator function needed in V2 |
| Domain canonicalization missed IDNA/punycode — visually-equivalent Unicode could bypass subset checks | Canonicalization mandates ASCII-only + IDNA/punycode; non-ASCII rejected with `invalid_domain_format`. See capability-broker §4.1 |
| Mount-path subset using raw string prefix would let `foo` vs. `foo-bar` slip through | Mount strings canonicalize to `(source_abs_path, target_abs_path, mode)` tuples via `os.path.normpath`; subset compares tuples |
| `RequestCapability(desk_id, ...)` accepted caller-supplied subject — confused-deputy risk | Removed `desk_id` arg; subject derived from bearer token. Admin impersonation is a separate future method |

### V4 forward-compat adjustments

| Finding | Resolution |
|---|---|
| Reserving `COMPUTE_QUOTA`/`NETWORK_REACH`/`STORAGE_MOUNT` YAML field names risked freezing wrong abstractions | V4 enum names reserved internally only; V2 YAML parser rejects those field names with `unknown_yaml_field`. Enum commitment is internal, shape not committed |
| "Always-valid" leases didn't exercise finite-lease machinery (renewal, revocation races) | V2 `SECRET` leases default to 24h finite expiry + auto-renew, exercising the path V4 cloud credentials need |
| `VAULT_MOUNT` as a distinct type created API baggage vs. future `STORAGE_MOUNT` | Cut entirely. Vault mounts ride on the generic `extra_mounts` narrowness rule. When V4 lands, a single `MOUNT` type with scheme discriminator is still an option |

### Reversibility reclassifications after simplification

| Item | Before | After | Note |
|---|---|---|---|
| `CreateDesk`/`SpawnChild` split | Low | Medium | Two authority models; expensive to reshape after clients depend |
| Unversioned `scope: dict` | (missing) | **HIGH** | Append-only-per-type convention in V2 |
| Token subject model | (missing) | Medium | V3 multi-user anchor |
| `RequestCapability` subject-from-token | (missing) | Medium | Trust-model contract |
| Domain canonicalization rules in validator's pure module | (missing) | Medium | Validator-bug class |
| Mount narrowness rule | (missing) | Medium | Pins daemon-enforced mount subset for nested spawn |

### What Codex confirmed was correctly classified

- HIGH: lease shape, validator purity, no-host-state-in-containers.
- Correct shape: wildcard/regex/CIDR ban in delegatable firewall domains.
- Audit event schema at Medium.

### What simplified the review's other concerns

Codex flagged `COMPUTE_QUOTA` and `NETWORK_REACH` scope shapes as likely-wrong. Resolved by not committing those shapes at all — V2 reserves enum names, rejects YAML, declines `RequestCapability` with `capability_unsupported`. Tests exercise the declination, not the shape.

## Cross-references

- `v2-scope.md` — the shipping spec V2 implements.
- `vision.md` — fabric framing; introduces `agent-desk` vocabulary.
- `secrets-roadmap.md` Phase 2+ — aligns with the capability-broker doc.
- `secrets-design.md` — Phase 1 file-backed convention that Phase 2 broker inherits.
- `CLAUDE.md` §Tests must justify their existence — binding test discipline for the policy validator.
- Slip-box: `Drydock vocabulary - project desk session.md`, `Nested agent spawn pressure - when autonomous decomposition justifies a daemon.md`, `Vault bridge - when two substrates earn their complexity.md`.
