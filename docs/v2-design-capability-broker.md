# V2 Design — Capability Broker + Policy Validator

**Purpose.** Define the daemon's trust boundary: the lease interface, and the pure function that validates every `SpawnChild` against narrowness invariants.

Extends `v2-scope.md` §Policy validation, `secrets-design.md` §End state, `secrets-roadmap.md` Phase 2+. Addresses OQ#4 (capability revocation on parent policy change) and OQ#5 (pluggable backends).

---

## 1. Why generalize from day one

V2's immediate need is secrets. V4's need (per `project_v4_cloud_fabric.md`) is storage mounts, compute quotas, network reachability tokens — all the same shape: daemon issues a time-bounded lease; desk presents it to use the capability; lease expires or is revoked.

The cost of a secrets-only API in V2 is a mandatory breaking change in V4. The cost of generalizing is one extra abstraction — `Capability` as a sum type, `SECRET` as one variant. V2 ships only `SECRET`; later types plug in additively.

## 2. Capability lease — data model

```python
@dataclass(frozen=True)
class CapabilityLease:
    lease_id: str                      # UUID; new id each renewal
    desk_id: str                       # grant subject (derived from bearer token at request time)
    type: CapabilityType               # SECRET (V2) | STORAGE_MOUNT/COMPUTE_QUOTA/NETWORK_REACH (V4 reserved)
    scope: dict                        # type-specific JSON
    issued_at: datetime                # absolute UTC
    expiry: datetime | None            # absolute UTC
    issuer: str                        # "wsd" in V2
    revoked: bool = False
    revocation_reason: str | None = None
```

**Notes.**

- `expiry` for V2 `SECRET` leases: **None by default** — leases live until desk destroy or explicit `ReleaseCapability`. Matches Phase-1 file-backed semantics (files are readable for the life of the desk). Finite-expiry + auto-renew machinery is reserved for V4 cloud credentials, which need rotation by design; V2 secrets don't. Clients may pass an explicit `ttl` for test scenarios, but no V2 caller relies on it.
- `issuer` field preserved for forward-compat (e.g., `wsd@hostname` if a thin multi-host case surfaces). V2 always emits `"wsd"`.
- **No `parent_lease_id` field** in V2. Earlier drafts reserved it for a "parent sub-lets to child" flow, but V2 enforces narrowness at `SpawnChild` time via the validator — children get independent leases against their own entitlements. If sub-letting ever becomes needed, add the field then.
- **No `scope_version` field** in V2. Codex review flagged the unversioned `dict` as a HIGH-reversibility risk. Mitigation in V2: treat `scope` as **append-only per type** (never rename keys, never narrow value types). When V4 ships its first new type, that iteration defines the versioning model.

## 3. Capability types

V2 implements one; the rest are enum-reserved.

| Type | V2 status | Notes |
|---|---|---|
| `SECRET` | **Implemented** | `scope = {secret_name: str}`. Phase 2 broker; exposed to desk as file at `/run/secrets/<secret_name>`. |
| `STORAGE_MOUNT` | Enum-reserved | V4 cloud storage. Daemon rejects with `capability_unsupported`. Scope shape not committed. |
| `COMPUTE_QUOTA` | Enum-reserved | V4. Scope shape not committed — real quotas need metering, not just numeric subset. |
| `NETWORK_REACH` | Enum-reserved | V4. Scope shape not committed — needs external services, ports, egress classes. |

**V2 YAML does not accept V4 type names.** `storage_mounts:` / `compute_quotas:` / `network_reach:` in project YAML get rejected with `unknown_yaml_field`. Enum reservation is an internal code commitment only; user-facing YAML stays minimal until V4 locks the shapes.

**Vault mounts are not a capability type.** Earlier drafts had `VAULT_MOUNT`. Removed per review — vault-access narrowness is covered by the generic mount-narrowness rule in the validator (§4, rule 4). Vault-bridge concept is documented in slip-box (`Vault bridge - when two substrates earn their complexity.md`); Drydock doesn't ship anything vault-specific in V2.

## 4. Policy validator — the pure function

```python
def validate_spawn(
    parent: DeskPolicy,
    requested_child: DeskSpec,
) -> Result[Allow, Reject]:
    """Return Allow() or Reject(reason: RejectReason).

    INVARIANT: Pure function. No I/O. Deterministic. No LLM.
    INVARIANT: Never takes a DB dependency. All state via arguments.
    """
```

**Inputs (`DeskPolicy`):**
- `parent.delegatable_firewall_domains: set[str]` — exact-string domains the parent may delegate
- `parent.delegatable_secrets: set[str]` — secret names the parent may delegate
- `parent.capabilities: set[CapabilityKind]` — bare on/off grants (`spawn_children`, `request_secret_leases`, …)
- `parent.extra_mounts: set[str]` — the parent's own mounts, as canonical strings; child can't receive a mount the parent itself doesn't have

**Child request (`DeskSpec`):**
- `child.firewall_extra_domains: set[str]`
- `child.secret_entitlements: set[str]`
- `child.capabilities: set[CapabilityKind]`
- `child.extra_mounts: set[str]`

**Rules (every one must hold):**

1. **Firewall narrowness:** `child.firewall_extra_domains ⊆ parent.delegatable_firewall_domains`.
2. **Secret narrowness:** `child.secret_entitlements ⊆ parent.delegatable_secrets`.
3. **Capability narrowness:** `child.capabilities ⊆ parent.capabilities`.
4. **Mount narrowness:** `child.extra_mounts ⊆ parent.extra_mounts`. The parent can only pass on mounts it already has. This covers the forcing-function case (parent has vault mount; child-scraper doesn't declare it → child can't reach vault).

**Resource budgets deferred.** `v2-scope.md` mentioned a rule 4 "resource limits" (parent CPU/memory/child-count budget debited on spawn). Dropped from V2: the V2 forcing function is a single monorepo with a handful of children; budget caps are premature. Reinstate when a desk genuinely burns shared host resources. Until then, container-level resource limits are the mechanism.

**Return shape:**
```python
@dataclass
class Reject:
    rule: str              # "firewall_narrowness" | "secret_narrowness" | ...
    parent_value: object
    requested_value: object
    offending_item: object
    fix_hint: str
```

**Post-spawn narrowness is a trivial lookup.** `RequestCapability` doesn't invoke the validator — it checks `requested_secret ∈ desk.secret_entitlements` against what `validate_spawn` already pinned at spawn time. `RenewCapability` does the same check (a narrowed-since-spawn `secret_entitlements` blocks renewal). No parallel validator function needed in V2; if V4 adds scoped capabilities that need subset-checking at request time, that logic lands with the V4 type.

### 4.1 Input canonicalization

Before subset checks, all string-valued fields are canonicalized. Validator rejects unnormalized input with `invalid_input_format`.

| Field | Canonical form |
|---|---|
| Domain strings | ASCII-only, IDNA/punycode encoded, lowercased, trailing dot stripped, no port, no wildcards. Reject non-ASCII or wildcard with `invalid_domain_format` |
| Mount strings (`extra_mounts`) | Parsed to `(source_abs_path, target_abs_path, mode)` tuple; source/target normalized via `os.path.normpath`; `..` rejected. Subset compares tuples, never raw strings |
| Secret names | Already restricted to `[a-z0-9_]{1,64}` by `ws secret` Phase-1 hardening |

Canonicalization lives in the same pure module as `validate_spawn`. A bug there is a validator bug. Fuzz tests generate inputs differing only in encoding/whitespace/case and assert canonical form is identical.

**No wildcards, regex, or CIDR** in `delegatable_firewall_domains`. Wildcard subset semantics is subtle and security-sensitive; V2 keeps the trust boundary trivially-correct. Projects that want wildcards enumerate the subdomains they actually need.

## 5. Test discipline

Per CLAUDE.md §Tests must justify their existence:

- **Contract tests** per rule: positive subset → Allow; negative superset → Reject with `rule=<name>`, `offending_item=<expected>`.
- **Regression tests** for every bug that surfaces.
- **Invariant tests** that assert purity: identical inputs → identical outputs; I/O mocked to fail still produces same result (proves no I/O attempted).
- **Canonicalization fuzz**: inputs differing only in encoding / whitespace / case produce identical canonical form.

Not worth writing: default-value assertions on dataclass fields, mock verification that `validate_spawn` was called, exhaustive combinatorics over orthogonal rule failures.

## 6. Capability revocation and policy change (addresses OQ#4)

### 6a. Parent destroyed → cascade
- Child desks destroyed first (existing `v2-scope.md` cascade).
- Outstanding child leases invalidated in memory + persisted `revoked=true`.
- Post-destroy capability requests from children return `parent_destroyed`.

### 6b. Parent's delegatable policy narrowed (admin op)
Operator edits project YAML. V2 does **not** do live cascade reconciliation:
- Existing children keep running under their existing entitlements (validator already pinned them at spawn time).
- To enforce a narrowed policy on running children: destroy + respawn. The admin flow is `ws destroy <child>` then `ws create` (or `SpawnChild` from the parent) with the now-narrower scope.
- Live `ws reconcile` with per-child state transitions is deferred; reinstate when destroy+respawn cost becomes painful.
- Narrowing a grant with no children affected = zero-cost; just update YAML.

### 6c. Lease expiry
- Expiries indexed in SQLite; background sweeper every 30s marks expired leases revoked and emits audit event.
- Desk's next request for an expired lease sees `lease_expired` and can renew.

**Why not re-validate on parent grant.** Grants only widen the parent's delegatable set; children with narrower entitlements remain valid.

## 7. Plugin interface for secrets backends (addresses OQ#5)

V2 ships one backend: file-backed (Phase 2 store = daemon-in-memory + persisted encrypted-at-rest). Phase-3 backends (1Password, Vault, cloud SMs) reserved but not built.

```python
class SecretsBackend(Protocol):
    name: str

    def fetch(self, secret_name: str, desk_id: str) -> bytes | None:
        """Return secret bytes, or None if not found.

        Sync-first: V2 ships only the file-backed backend, where fetch is
        a stat + read with no I/O wait. Network-sourced backends (1Password
        via `op` CLI, Vault, cloud SMs) should add `async def fetch_async`
        as an additive method when they ship; the daemon will prefer
        `fetch_async` when present and fall back to `fetch` otherwise.
        Wrapping a sync `fetch` in `run_in_executor` works as a transition
        but adds latency and noisier error semantics — not a long-term answer.
        """

    def supports_rotation(self) -> bool: ...
    def rotate(self, secret_name: str) -> bytes | None: ...
```

Selection via per-project YAML:
```yaml
secrets_backend: 1password
secrets_source: "op://Private/myapp"
```

Default `file`. Unknown backend: `ws create` rejects with `unknown_secrets_backend`.

Lease semantics are backend-independent: `RequestCapability(type=SECRET, ...)` returns the same `CapabilityLease` shape regardless of backend.

**Threat-model note for file-backed.** The "personal-fleet scale" framing of the V2.0 secrets-backend decision (see status block in `v2-design-overview.md`, 2026-04-16 entry) is right for backend choice but undersells secret density per host. The drydock-employee pattern (see `project_drydock_employee_pattern.md`) means N permissioned long-running agents per host, each holding its own entitlement set — `~/.drydock/secrets/` grows in agents × secrets, not in hosts. 0400 perms + OS-level disk encryption (FileVault on Mac, LUKS on Linux) are the load-bearing controls; daemon-level encryption-at-rest is defense-in-depth against root processes only. Worth restating in any future explicit threat-model doc.

## 8. Reversibility audit

| Decision | Cost | Notes |
|---|---|---|
| Capability-lease shape | **HIGH** | V4 extends. Minimalist on purpose |
| `scope: dict` unversioned | **HIGH** | Append-only-per-type convention in V2; first V4 type defines formal versioning |
| Purity of `validate_spawn` | **HIGH** | Hard invariant; test-enforced |
| Secrets-as-capability-type (not separate API) | Medium | Generalization is the design's payoff |
| Domain canonicalization (IDNA/punycode, ASCII-only) | Medium | Validator-trust-boundary rules; bugs here are validator bugs |
| Mount narrowness as a validator rule | Medium | Rule 4 pins daemon-enforced mount subset. Removing it later weakens nested-spawn isolation |
| Backend Protocol shape | Medium | Narrow on purpose; widening is additive |
| V4 enum names reserved, YAML not yet accepting them | Low | Internal enum commitment only; YAML opens in V4 |
| Structured `Reject` shape | Low | Additive fields OK |
| Policy-change → `policy_violation` state (not kill-container) | Low | Behavior reversible by admin flag |
