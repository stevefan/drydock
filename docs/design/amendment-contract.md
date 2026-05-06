# Amendment contract — multi-author IaC

**Status:** sketch · **Pulls from:** [vocabulary.md](vocabulary.md) §"Infrastructure-as-code", [harbor-authority.md](harbor-authority.md), [port-auditor.md](port-auditor.md)

## Premise

The Project YAML is the **infrastructure-as-code** declaration for one DryDock. Traditional IaC has a single class of author (a human or CI). The V3 model has *three* classes:

1. **Principal direct.** The principal edits the YAML and the change applies. No approval gate — the principal is the principal.
2. **Dockworker within standing policy.** The agent inside the DryDock proposes a change that falls inside what the principal already authorized. The Authority auto-applies.
3. **Dockworker novel.** The agent proposes a change outside standing policy. The Auditor escalates to the principal via Telegram; principal approves/denies/redirects.

The amendment contract is the structured surface that supports all three with a single audit trail. This generalizes Steven's pre-V3 `EGRESS_GRANTS.yaml` prototype into a per-capability-type amendment system.

## The amendment shape

Every amendment, regardless of class, has the same envelope:

```yaml
amendment:
  id: am_<uuid>
  proposed_by:
    type: principal | dockworker
    identity: <desk_id or principal_id>
    timestamp: <iso8601>
  scope:
    yard: <yard_name or null>
    drydock: <dock_name or null>
  kind: network_reach | secret_grant | resource_lift | workload_register | ...
  request: { ...kind-specific... }
  reason: |
    Free-form prose. Required for Dockworker proposals; principal can omit.
  tos_notes: |
    Optional. Third-party ToS or compliance considerations.
  status: pending | auto_approved | escalated | approved | denied | applied | expired
  reviewed_by: <principal_id or "authority-auto" or null>
  reviewed_at: <iso8601 or null>
  review_note: |
    Reviewer's comment (required for denial; optional for approval).
  applied_at: <iso8601 or null>
  expires_at: <iso8601 or null>     # for time-bounded grants (workload registrations)
```

The `kind` field determines the schema of `request`. Each kind is its own micro-protocol that the broker knows how to validate and apply.

## Amendment kinds (V3 starter set)

| Kind | What it requests | Auto-approve gate |
|---|---|---|
| `network_reach` | Open egress to (domain, port) | Domain matches `delegatable_network_reach` glob AND port in allowlist |
| `secret_grant` | Materialize a secret into the calling Drydock | Secret in `delegatable_secrets` (or yard's `shared_secrets`) |
| `storage_grant` | Mount an S3 prefix with mode | Bucket+prefix matches `delegatable_storage_scopes` |
| `workload_register` | Declare upcoming heavy workload (lifts caps) | All requested resources within `workload_max` bounds |
| `resource_lift_persistent` | Permanently raise a soft ceiling | Never auto-approves; always escalates |
| `narrowness_widen` | Add to delegatable_* | Never auto-approves; always escalates |
| `fleet_change` | Add/remove Ship from Fleet | Never auto-approves; always escalates |

The auto-approval gate is implemented in the Authority (deterministic, in-process). Anything that fails the auto-gate gets `status: escalated` and lands in the Auditor's queue for principal review.

## Flow

### Class 2 — Dockworker within standing policy (auto-approved)

1. Dockworker calls `RequestCapability(kind=NETWORK_REACH, scope={domain: api.github.com, port: 443}, reason: "fetching dependencies")` via in-desk RPC
2. Authority validates: in `delegatable_network_reach` glob? port in allowlist? capability gate? → all pass
3. Authority creates amendment record with `status: auto_approved` immediately, `reviewed_by: "authority-auto"`
4. Authority applies (calls `add-allowed-domain.sh` in the container)
5. Audit: `amendment.proposed`, `amendment.auto_approved`, `amendment.applied`
6. Returns lease to Dockworker

Latency: <100ms. No LLM in path. No human in path.

### Class 3 — Dockworker novel (escalates)

1. Dockworker calls `RequestCapability(kind=NETWORK_REACH, scope={domain: evil.com, port: 443}, reason: "third-party API for X")` 
2. Authority validates: `evil.com` does NOT match any `delegatable_network_reach` glob → fails auto-gate
3. Authority creates amendment record with `status: escalated`
4. Authority returns `{lease: null, amendment_id: am_xyz, status: escalated}` to Dockworker (Dockworker's request is queued, not yet granted)
5. Auditor LLM (running in port-auditor Drydock) sees the new escalation, processes it:
   - Pulls context (recent audit, Dockworker's history, principal's standing values)
   - Decides whether to add Form A informational note ("auction-crawl is asking for evil.com, has not requested external domains in 6 months — unusual")
   - Sends Telegram to principal:
     ```
     🟡 amendment escalation
     dock: auction-crawl @ hetzner
     kind: network_reach
     request: open evil.com:443
     reason: "third-party API for X"
     auditor note: unusual — first external domain request in 6 months
     reply: 'approve' / 'deny' / 'investigate' / 'amend (...)'
     ```
6. Principal replies:
   - `approve` → Auditor calls `ApplyAmendment(am_xyz)` → Authority applies, status → `applied`
   - `deny` → status → `denied`, audit logged, Dockworker can retry with different proposal
   - `investigate` → status stays `escalated`, no action; principal does some checking
   - `amend (...)` → principal modifies the request (e.g., narrows the domain), then approves the modified version

Latency: minutes-to-hours (principal-paced). LLM in escalation path. Human in approval path.

### Class 1 — Principal direct

1. Principal edits YAML (or runs `ws project edit`, or replies "promote evil.com to standing for auction-crawl" in Telegram)
2. Authority creates amendment record with `proposed_by.type: principal`, `status: approved` immediately
3. Applies. Audit logged.

No LLM. No escalation. The principal is the principal.

## How this composes with existing things

- **EGRESS_GRANTS.yaml prototype**: this contract is the generalization. The pending-grants file shape becomes the `amendment` envelope; the per-grant fields map directly.
- **Capability broker**: the Authority's existing capability handlers (SECRET, STORAGE_MOUNT, NETWORK_REACH, INFRA_PROVISION) become the auto-approval gates for their respective amendment kinds. Existing code mostly unchanged; we add the amendment record-keeping.
- **Workload registration** (resource-ceilings.md §3): becomes one kind of amendment (`workload_register`). Auto-approves within `workload_max`; otherwise escalates. The bundled-NETWORK_REACH-leases pattern stays.
- **Audit log**: amendments are first-class audit events. `amendment.proposed`, `amendment.auto_approved`, `amendment.escalated`, `amendment.applied`, `amendment.denied`, `amendment.expired`.
- **Friction (principal-friction.md)**: applies to amendments where the principal is the actor. Form A notes can attach to escalation messages. Form B/C apply when the amendment's effect is high-stakes (e.g., a `narrowness_widen` for capabilities-with-master-credentials).

## What this is not

- **Not a workflow engine.** Amendments are simple state machines (proposed → auto/escalated → applied/denied). No multi-step approvals, no parallel reviewers, no SLAs. One principal, one decision per amendment.
- **Not a replacement for the YAML being canonical.** Approved amendments are *applied* to the running state, but the principal is encouraged to fold them into the YAML for durability. An amendment that opens evil.com expires when the Dock recreates; folding it into delegatable_network_reach makes it durable.
- **Not auto-applicable for irreversible changes.** Auto-approval is gated on "can the auto-gate evaluate it deterministically." Anything irreversible (fleet changes, narrowness widening, persistent ceiling lifts) explicitly does NOT auto-approve, regardless of declared scope.

## Storage

```sql
CREATE TABLE amendments (
    id              TEXT PRIMARY KEY,
    proposed_by_type TEXT NOT NULL CHECK (proposed_by_type IN ('principal', 'dockworker')),
    proposed_by_id  TEXT NOT NULL,
    proposed_at     TEXT NOT NULL,
    yard_id         TEXT NULL,
    drydock_id      TEXT NULL,
    kind            TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    reason          TEXT NULL,
    tos_notes       TEXT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    reviewed_by     TEXT NULL,
    reviewed_at     TEXT NULL,
    review_note     TEXT NULL,
    applied_at      TEXT NULL,
    expires_at      TEXT NULL
);

CREATE INDEX idx_amendments_status ON amendments (status, proposed_at DESC);
CREATE INDEX idx_amendments_drydock ON amendments (drydock_id, proposed_at DESC);
```

## CLI surface (proposed)

| Command | Purpose |
|---|---|
| `ws amendment list [--status pending] [--dock <name>]` | List amendments |
| `ws amendment show <id>` | Show full amendment record |
| `ws amendment approve <id> [--note <text>]` | Principal approves (Class 1 explicit) |
| `ws amendment deny <id> --note <text>` | Principal denies |
| `ws amendment apply-pending [--auto-only]` | Force-process pending amendments (the auto-gate) |
| `ws amendment expire <id>` | Mark a pending amendment expired |

Telegram has direct surface for principal approve/deny without CLI; CLI is for review/inspection.

## Phasing

**Phase A0 — schema + envelope:** `amendments` table, the envelope, basic CRUD. NO auto-approval logic yet — every amendment goes to `pending` and requires manual `ws amendment approve`. Proves the schema and audit shape.

**Phase A1 — auto-approval for existing kinds:** wire `network_reach`, `secret_grant`, `storage_grant` to use the amendment envelope; the existing capability-handler validation becomes the auto-gate. Successful validation → `status: auto_approved`. Failures → `status: escalated`.

**Phase A2 — escalation queue + Auditor consumption:** the Auditor (when implemented) reads escalated amendments, adds Form A notes, sends Telegram. Principal reply routes back to amendment status.

**Phase A3 — novel amendment kinds:** workload_register (with bounded auto-lift), resource_lift_persistent (escalate-only), narrowness_widen (escalate-only), fleet_change (escalate-only).

## Open questions

1. **Amendment expiration semantics.** Class-2 auto-approved amendments — when do they expire? For NETWORK_REACH today the answer is "container restart." For storage leases it's "TTL on the STS credential." Should there be a generic amendment expiry that drives lease lifecycle, or per-kind? Lean per-kind for V3; generic later if patterns converge.
2. **Where do reasons get stored vs displayed?** The `reason` field is principal-readable but does it inform anything else? Maybe the Auditor uses prior reasons to calibrate "is this novel-domain request consistent with what this Dockworker has done before?" — possible Phase A2+.
3. **Multi-Dockworker amendment proposals?** If a Yard has shared concerns and multiple Dockworkers in the Yard might propose related amendments — do they coordinate? Probably not for V3; each amendment is one-Dockworker-one-proposal.
4. **Amendments on principal's own behalf.** When the principal directly edits YAML, does the daemon auto-create amendments to record what changed? Probably yes — even principal edits should be auditable as amendments. Otherwise the audit log has gaps.
