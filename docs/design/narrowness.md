# Drydock — Narrowness

**Purpose.** Narrowness is the single policy invariant Drydock enforces: **children are strictly narrower than parents, and workers cannot exceed the drydock they run in.** Every grant the daemon issues — a spawned child drydock, a capability lease — passes through the same validator with the same rule set.

See [vocabulary.md](vocabulary.md) for Harbor / DryDock / Worker and [capability-broker.md](capability-broker.md) for the lease model that narrowness gates.

---

## 1. The invariant

A parent drydock declares a **delegatable set**: the firewall domains, secrets, capabilities, mounts, and storage scopes it is willing to hand off. A child request may only draw from this set. Once pinned at spawn time, a child's entitlements are fixed — later narrowing of the parent's delegatable set does not propagate to running children (destroy + respawn is the admin flow).

The validator is a **pure function**. No I/O. No database. Deterministic. All state arrives via arguments. This keeps the trust boundary trivially testable and keeps the validator immune to reviewer fatigue about mock setup.

Code: `validate_spawn()` in `src/drydock/core/policy.py`.

## 2. Uniform across spawn and lease

The validator runs at two distinct moments:

- **At `SpawnChild` time** — full shape: the child's requested firewall domains, secret entitlements, capabilities, and extra mounts must all subset the parent's delegatable set. On reject, `SpawnChild` returns `narrowness_violated` with the structured `Reject` shape and emits a `desk.spawn_rejected` audit event.
- **At `RequestCapability` time** — post-spawn narrowness is a trivial lookup: does the requested scope fall inside what was pinned on the caller at spawn time? For `SECRET`, the subset check is `scope.secret_name ∈ caller.delegatable_secrets`. For `STORAGE_MOUNT`, `matches_storage_scope()` in `policy.py` handles the parsed-scope matcher.

Both paths reject with the same error code (`narrowness_violated`, -32006), the same `data.rule` identifier, and the same audit pattern. The worker-action scoping case (a drydock narrowing its worker's authority for one operation) reuses the validator by treating the worker's request as the `requested_child`.

Separate per-caller narrowness code paths were rejected because divergent edge-case behavior and a muddier audit story were the predictable outcomes.

## 3. The rule set

`validate_spawn(parent: DeskPolicy, requested_child: DeskSpec) -> Allow | Reject`

Every rule must hold. First failure short-circuits.

| Rule | Check | Reject `rule` |
|---|---|---|
| 1 | `child.firewall_extra_domains ⊆ parent.delegatable_firewall_domains` | `firewall_narrowness` |
| 2 | `child.secret_entitlements ⊆ parent.delegatable_secrets` | `secret_narrowness` |
| 3 | `child.capabilities ⊆ parent.capabilities` | `capability_narrowness` |
| 4 | `child.extra_mounts ⊆ parent.extra_mounts` | `mount_narrowness` |

Rule 3 uses the `CapabilityKind` enum: `SPAWN_CHILDREN`, `REQUEST_SECRET_LEASES`, `REQUEST_STORAGE_LEASES`. A parent that cannot spawn grandchildren cannot grant that authority. A parent that cannot request storage leases cannot pass that capability down.

Rule 4 is the forcing function that stops a child-scraper from reaching a vault mount the parent carries but did not declare for the child — no vault-specific capability type is needed; the generic mount subset check covers it.

**Storage scopes** are a separate at-request-time check performed by `matches_storage_scope()` inside the capability handler. They don't run through `validate_spawn` because a `STORAGE_MOUNT` lease targets a specific `(bucket, prefix, mode)` triple, not a bare on/off capability.

**Resource budgets (CPU, memory, child count) are not enforced.** Container-level limits are the mechanism today. Reinstate when a drydock genuinely burns shared Harbor resources.

## 4. Input canonicalization

Before subset checks, all string-valued fields are canonicalized. A bug in canonicalization is a validator bug.

### Firewall domains

`canonicalize_domain(raw)` in `policy.py`:

- ASCII only; non-ASCII raises `InvalidDomainFormat`.
- No wildcards (`*`), no ports (`:`), no empty labels.
- Trailing dot stripped, lowercased.
- IDNA/punycode encoded via `.encode("idna")`.

Subset comparison is exact-string after canonicalization. Wildcards, regex, and CIDR are deliberately excluded — wildcard subset semantics is subtle and security-sensitive. Projects that want wildcards enumerate the subdomains they actually need.

### Mounts

`canonicalize_mount(raw)` parses `"source=...,target=...,type=..."` into a `(source, target, mode)` tuple. Both paths are `os.path.normpath`'d. `..` segments are rejected outright. `target` must be absolute. `mode` is one of `{bind, volume}`.

Subset compares tuples, never raw strings. `"source=/a,target=/x,type=bind"` and `"type=bind,source=/a/,target=/x"` canonicalize to the same tuple.

### Secret names

Already restricted to `[A-Za-z0-9_.\-]{1,64}` by `ws secret set`. Validator treats them as opaque strings.

### Storage scopes

Parent-side format in project YAML: `"s3://bucket/prefix/*"` (read-only) or `"rw:s3://bucket/prefix/*"` (read-write implies read). Trailing `/*` is sugar. `parse_storage_scope()` returns `{bucket, prefix, mode_max}`.

Request-side shape: `{bucket, prefix, mode}`. Matching rules (`matches_storage_scope()`):

- `bucket` must equal exactly.
- Requested prefix equals granted prefix, or is under it (`granted + "/"` prefix match). Scope `"data"` matches `"data"` and `"data/foo"` but not `"data2"`. Empty granted prefix matches any.
- Requested mode must be `<=` granted `mode_max`. `"ro"` always matches; `"rw"` requires the `rw:` prefix on the granted scope.

Malformed scopes in a parent's declared list are silently skipped, not fatal — a typo must not open the whole set.

## 5. Default-permissive-when-empty for storage

`DeskPolicy.delegatable_storage_scopes` defaults to `()`. An empty tuple means **"no narrowness declared yet; the capability gate alone governs."** A drydock with `REQUEST_STORAGE_LEASES` granted and no scopes declared can request any `(bucket, prefix, mode)` the backend will mint. Once any scope is declared, every request must match one.

This is back-compat for drydocks that were granted the capability before per-bucket narrowness landed. Switching to deny-all-when-empty would break them instantly. A drydock opts into narrowness by declaring its first scope; there is no mixed mode.

Same-class pattern is not used for `delegatable_secrets` or `delegatable_firewall_domains` — those have been empty-means-empty from the start, and adding entries is the only grant mechanism.

## 6. Result shape

```python
@dataclass(frozen=True)
class Allow: ...

@dataclass(frozen=True)
class Reject:
    rule: str                # "firewall_narrowness" | ...
    parent_value: object     # canonical form of the delegatable set
    requested_value: object  # canonical form of what was asked
    offending_item: object   # the specific item not in the parent's set
    fix_hint: str            # stable contract; tests pin the wording
```

`fix_hint` is treated as a stable contract per the test discipline — suggested recovery strings are user-facing surface.

## 7. Audit event on violation

At spawn time, a rejection emits:

```json
{
  "event": "desk.spawn_rejected",
  "principal": "ws_parent",
  "method": "SpawnChild",
  "result": "error",
  "details": {
    "parent_desk_id": "ws_parent",
    "reject": {
      "rule": "firewall_narrowness",
      "offending_item": "evil.example.com"
    }
  }
}
```

At lease-request time, the capability handler raises `narrowness_violated` directly; the generic RPC-error audit path covers it. The `data.rule` field on the error matches the `Reject.rule` string so downstream log consumers can classify violations uniformly across spawn and lease.

On allow, a positive event (`desk.spawned` with `narrowness_check: "allow"`, `lease.issued` with the full scope) lands instead. Every grant is audited; every rejection is audited.
