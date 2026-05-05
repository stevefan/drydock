# Resource ceilings + workload registration

**Status:** sketch · not yet implemented · **Depends on:** [capability-broker.md](capability-broker.md), [narrowness.md](narrowness.md), [principal-harbormaster-governance.md](principal-harbormaster-governance.md), [deskwatch.md](deskwatch.md), [harbor-monitor.md](harbor-monitor.md)

This is the doc that fills out §5(a) and §5(c) of the principal–Harbormaster–Dockworker governance paper: the schema for *resource ceilings* a drydock operates under, and the *workload registration* primitive that lets desks legitimately burst above their standing caps without being mistaken for a runaway.

---

## 1. The two-track design

Two separate mechanisms, intentionally:

**Hard ceilings** are enforced by the substrate (cgroups, docker flags, filesystem quotas). They cannot be exceeded — the kernel kills the offending process. Set at container creation, recorded in the registry, immutable for the container's lifetime. Right tool for things where "exceeded" means "OS-level damage": OOM that takes the host down, fork bomb, runaway disk writes that fill the volume.

**Soft ceilings** are observed by the Harbormaster. Exceeding one triggers a Harbormaster *action* (warn / throttle / revoke / stop / escalate per policy) but the substrate doesn't enforce them. Right tool for things where "exceeded" means "spending too much money or quota": Anthropic API tokens, AWS API calls, network egress bytes, lease hold time. The kernel doesn't know what an API token costs.

The split matters because they have different failure modes (substrate kill is abrupt and unsignalable; Harbormaster action is slower but graceful) and different authoring semantics (cgroup limits are "what the substrate can enforce"; soft caps are "what the principal cares about budget-wise").

---

## 2. Schema

In project YAML, under the existing `narrowness:` block:

```yaml
narrowness:
  delegatable_secrets: [claude_credentials]
  network_reach: ["*.github.com", "pypi.org"]

  resources:
    # Hard — substrate-enforced at container creation
    hard:
      cpu_max:        2.0          # docker --cpus
      memory_max:     4Gi          # docker --memory
      pids_max:       512          # docker --pids-limit
      workspace_disk_max: 20Gi     # filesystem quota on the worktree volume

    # Soft — Harbormaster-observed in software
    soft:
      egress_bytes_per_day:        5Gi
      egress_bandwidth_max:        10Mbps
      anthropic_tokens_per_day:    1_000_000
      aws_calls_per_hour:          1000
      lease_hold_max:              8h    # any single capability lease
      idle_lease_revoke_after:     2h    # revoke if unused this long

    # Per-violation policy. Keys are soft-ceiling names; value is the
    # Harbormaster action when that ceiling is breached.
    on_violation:
      anthropic_tokens_per_day:  throttle    # block further API spend
      egress_bytes_per_day:      escalate    # ask the principal
      aws_calls_per_hour:        throttle
      lease_hold_max:            revoke      # revoke the offending lease
      default:                   warn        # anything not listed
```

Globals at `~/.drydock/policy/global.yaml` use the same shape. Per-drydock *narrows*, never widens — schema validation rejects per-drydock soft ceilings higher than global.

### Fields, briefly

**Hard** (everything maps to a real substrate primitive):
- `cpu_max` — fractional cores; `docker --cpus`. Default unset = unlimited.
- `memory_max` — `docker --memory`. Default unset = host RAM.
- `pids_max` — `docker --pids-limit`. Default 1024.
- `workspace_disk_max` — quota on the `/workspace` volume. Today this needs a quota-aware fs (xfs/btrfs); ext4 falls back to a periodic du-check by the Harbormaster. Document the fallback as soft-on-ext4.

**Soft** (everything maps to something the Harbormaster can count):
- `egress_bytes_per_day` / `egress_bandwidth_max` — bytes counted at the container's veth via `iptables -L -v` or `nftables` counters; the Harbormaster reads on its poll cycle and computes deltas. Bandwidth ceiling is enforced via `tc/htb`.
- `anthropic_tokens_per_day` — counted by reading the worker's Claude Code session metadata (claude writes per-session token counts to `~/.claude/projects/<dir>/sessions/`); the in-desk probe surfaces deltas.
- `aws_calls_per_hour` — counted from the AWS STS lease's CloudTrail-ish call log when issued via `RequestCapability(STORAGE_MOUNT)`; the broker already mints these and can attribute calls back to drydocks.
- `lease_hold_max` / `idle_lease_revoke_after` — broker-tracked since the broker mints leases; sweeper runs on the Harbormaster's cadence.

**Action vocabulary** (`on_violation` values):
- `warn` — emit a deskwatch violation event; no enforcement.
- `throttle` — block further consumption of this resource until lifted (specific to the resource: `anthropic_tokens` blocks API spend; `egress_bandwidth` caps tc/htb to 0).
- `revoke` — revoke the offending lease (only meaningful for `lease_hold_max`).
- `stop` — `ws stop` the drydock (preserves volumes).
- `escalate` — push to principal via Telegram, take no enforcement until response.

### Defaults the principal will inherit

A `defaults` section in `global.yaml` applies to any drydock that doesn't override:
```yaml
defaults:
  hard:
    cpu_max: 2.0
    memory_max: 4Gi
    pids_max: 1024
  soft:
    anthropic_tokens_per_day: 500_000
    egress_bytes_per_day: 5Gi
    lease_hold_max: 8h
    idle_lease_revoke_after: 2h
  on_violation:
    default: warn
    anthropic_tokens_per_day: escalate
    egress_bytes_per_day: escalate
```

Empty defaults = no ceilings (current behavior). Recommend the principal start with the above and tighten per-drydock.

---

## 3. Workload registration

A worker about to burn meaningfully more than its standing soft caps **declares the workload first**. The Harbormaster responds with a `WorkloadLease` that lifts the relevant caps for the lease window; without a registration, the same burn trips enforcement.

### RPC

```
RegisterWorkload {
  kind:         "training" | "crawl" | "batch" | "experiment" | "interactive"
  description:  "fine-tune llama-3-8b on internal corpus"
  expected: {
    cpu_hours:           4.0
    memory_peak:         12Gi          # may exceed hard.memory_max → escalates
    disk_writes:         50Gi
    egress_bytes:        2Gi
    anthropic_tokens:    50_000
    duration_max:        2h
  }
  domains_needed:        ["huggingface.co", "*.s3.amazonaws.com"]   # optional
}
→ WorkloadLease {
  id:           "wl_2026050501"
  granted_at:   "2026-05-05T16:20:00Z"
  expires_at:   "2026-05-05T18:20:00Z"
  granted: {                             # may differ from expected
    egress_bytes:        2Gi
    anthropic_tokens:    50_000
    ...
  }
  granted_domains:       ["huggingface.co", "*.s3.amazonaws.com"]
  bundled_capability_leases: [           # auto-issued NETWORK_REACH leases
    "lease_abc...", "lease_def..."
  ]
}
```

Subject identity from bearer token, per existing broker convention.

### Harbormaster decision logic

1. Look up effective policy for caller_desk (per-drydock ∩ global).
2. For each requested resource bump:
   - Within standing soft cap: free.
   - Above standing soft cap, within `workload_max`: lift the cap to the requested level for the lease window.
   - Above `workload_max` or above any hard ceiling: escalate to principal (or reject if `auto_reject_hard_breach: true`).
3. For each `domains_needed`: same logic against `network_reach`. Auto-issued bundled NETWORK_REACH leases have `expires_at == workload.expires_at`.
4. After expiry: caps snap back; bundled leases revoke; deviation between declared and actual is recorded in audit.

### What "above standing, within workload_max" means

```yaml
narrowness:
  resources:
    soft:
      anthropic_tokens_per_day: 500_000     # standing cap
    workload_max:
      anthropic_tokens_per_day: 5_000_000   # what RegisterWorkload can lift to
      egress_bytes:             50Gi
      memory_peak:              16Gi        # above hard.memory_max → always escalates
```

`workload_max` is the principal's pre-authorization for "this drydock can burst this high if it asks." Anything above it requires synchronous principal approval. Combined with tight standing caps, this means *typical* drydock operation is bounded tightly, *expected* heavy work is bounded loosely-but-declared, and *unexpected* heavy work trips immediately.

### Why this is the leverage point

The current single-cap model has a tuning paradox: tight enough to catch runaways → false-positive on legitimate work; loose enough to allow legitimate work → misses real runaways. Adding workload registration sidesteps it by making "is this expected?" a *declared* property rather than something the Harbormaster has to infer. The policy author's job becomes setting two numbers (standing cap, workload_max) plus `on_violation` actions, and the Harbormaster's judgment is reduced to "did they register or not?"

---

## 4. Lifecycle interactions

### Container creation
- Hard ceilings → docker run flags. Soft ceilings → registry columns + policy table.
- Failure mode: hard ceiling unset where global default exists → use global default; explicit `null` opts out (audited).

### Container running
- Soft ceilings polled every N seconds (configurable; default 60s) by Harbormaster via harbor-monitor + in-desk probe.
- Workload leases tracked in broker; expiry sweeper runs on Harbormaster cadence.

### Container stop
- All workload leases for that drydock auto-revoke.
- Final deviation-from-declared recorded in audit.

### Container restart (`ws stop && ws create`)
- Hard ceilings re-applied from current registry state (catches any policy update since last create).
- Soft ceilings carry over (they live in policy, not container state).
- In-flight workload leases survive only if the container restart was Harbormaster-initiated within the lease window (e.g., `RestartDeskAgent` for credential refresh); otherwise revoked.

### Project YAML reload (`ws project reload`)
- Hard ceiling diffs trigger a "container restart needed to apply" warning (since cgroup limits can't be raised on a running container without recreate; lowering is technically possible via `docker update` but we keep the model uniform).
- Soft ceiling diffs apply immediately on the Harbormaster's next poll.

---

## 5. Audit shape

Every meaningful event lands in audit:

```
{event: "resource.violation",  desk_id, ceiling, value, limit, action_taken}
{event: "workload.registered", desk_id, workload_id, kind, expected, granted}
{event: "workload.deviation",  desk_id, workload_id, resource, declared, actual}
{event: "workload.expired",    desk_id, workload_id, summary}
{event: "ceiling.lifted",      desk_id, ceiling, from, to, by_workload, by_principal}
```

`workload.deviation` is the interesting one: the principal can run a "where do my drydocks consistently under-declare or over-declare?" report and tighten/loosen the standing caps accordingly. This is the feedback loop that keeps the model honest over time.

---

## 6. CLI surface

| Command | Purpose |
|---|---|
| `ws resources show <desk>` | Effective ceilings (per-drydock ∩ global) and current usage. |
| `ws resources history <desk> [--days N]` | Time series of usage vs ceilings; flags excursions. |
| `ws workload list [<desk>]` | Active and recent workload leases. |
| `ws workload register <desk> --kind K --description D ...` | Manually register from the Harbor (debug / one-off). |
| `ws workload revoke <workload_id>` | Cut short a workload lease. |
| `ws ceiling raise <desk> <ceiling> <value> --duration D` | Principal-initiated ceiling lift outside the workload mechanism. Audited as principal action. |

Worker side: `drydock-rpc RegisterWorkload kind=training description=... expected.cpu_hours=4 ...` once the wsd handler dispatches it.

---

## 7. Implementation order

Three phases, each independently shippable:

**Phase A — schema + hard ceilings.**
- `ResourceCeilings` dataclass in `core/`, parsed from project YAML's `narrowness.resources` block.
- Registry columns: `hard_ceilings_json`, `soft_ceilings_json`, `on_violation_json`.
- `ws create` translates hard ceilings to docker flags (`--cpus`, `--memory`, `--pids-limit`).
- `ws resources show <desk>` (read-only).
- No Harbormaster action yet; no workload registration.

This alone is useful: the principal can set hard caps, the substrate enforces them, OOMs are bounded.

**Phase B — soft observation.**
- In-desk probe (`desk-probe`, per principal-harbormaster-governance.md §5d) ships in drydock-base with a snapshot RPC.
- Harbormaster poll cycle: read snapshots, compute deltas, write to `resource_usage` table.
- `on_violation: warn` actions emit deskwatch violations.
- `ws resources history` works.

This makes consumption visible without enforcement. Calibration phase — the principal watches what their drydocks actually use before tightening.

**Phase C — soft enforcement + workload registration.**
- `RegisterWorkload` / `WorkloadLease` in broker.
- `on_violation: throttle | revoke | stop | escalate` actions wired up.
- Bundled NETWORK_REACH leases issued + revoked alongside workloads.
- Bidirectional Telegram for escalations (per principal-harbormaster-governance §5).

Phase C is where the model becomes self-regulating; A and B are the substrate for it.

**Mapping to the principal-Harbormaster roadmap.** Per [principal-harbormaster-governance.md §6](principal-harbormaster-governance.md), this work spans roadmap items #1 (resource ceilings + workload registration in `narrowness:`) and #2 (lease TTL + idle revocation, which is Phase C's lease-hold-max enforcement); Phase B's in-desk probe is roadmap item #4. Phase A is the only piece that can ship without the Harbormaster authority surface (item #5) being designed.

**Where cgroup metrics come from.** Phase B's soft observation reads from the in-container `desk-probe` (per [principal-harbormaster-governance.md §5d](principal-harbormaster-governance.md)) for process/fd/disk-growth visibility, and from the Harbor's cgroup interface for CPU/memory totals. [harbor-monitor.md](harbor-monitor.md) is *not* extended to gather cgroup stats — it stays focused on liveness/health/CC-token probes; the Harbormaster joins harbor-monitor's signal with desk-probe's snapshot to reach a verdict.

---

## 8. Open questions

1. **Per-resource enforcement plane.** Egress bandwidth via `tc/htb` is straightforward but adds a Harbor-side dependency (`tc` in the right package). Anthropic-token throttle requires the worker to cooperate (the broker can revoke the `claude_credentials` lease, but in-memory tokens persist until process restart — so throttle = revoke + `RestartDeskAgent`). Document the cooperative-vs-coercive distinction per resource.
2. **Workload kind taxonomy.** Free-form strings or a closed enum? Lean: closed enum for V1 (training/crawl/batch/experiment/interactive), with the principal able to add custom kinds in `global.yaml`. Closed enum makes per-kind policy possible ("crawls auto-get up to 10Gi egress; experiments require approval").
3. **Honest-declaration incentive.** Nothing prevents a worker from declaring small workloads to evade enforcement. Mitigations: deviation tracking (consistent under-declaration triggers escalation), per-drydock reputation (desks that frequently deviate get lower auto-grant ceilings). Probably not a V1 problem since the failure mode is "Steven notices his agents lying," not "adversary." Revisit if multi-user lands.
4. **Workload-against-Harbormaster.** Can the Harbormaster itself register workloads? Probably no — the Harbormaster's resource budget is principal-set and not lifted by self-declaration; the principal-out-of-band-kill protocol is what bounds Harbormaster resource use. Document explicitly so the model stays clean.
5. **Cost mapping.** `anthropic_tokens_per_day` is a proxy for spend, not spend itself. Future: a `daily_dollar_cap` that aggregates Anthropic token cost + AWS STS-lease-attributed cost + tailnet bandwidth cost into one number. Out of scope for Phase A–C.
