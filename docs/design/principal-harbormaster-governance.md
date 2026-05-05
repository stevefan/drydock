# The principal–Harbormaster–Dockworker triangle

**Status:** position paper · **Pulls together:** [vision.md](../vision.md), [capability-broker.md](capability-broker.md), [narrowness.md](narrowness.md), [employee-worker.md](employee-worker.md), [auth-broker.md](auth-broker.md), [harbor-monitor.md](harbor-monitor.md), [network-reach.md](network-reach.md), [deskwatch.md](deskwatch.md), [tailnet-identity.md](tailnet-identity.md)

This document is the umbrella over the recent run of design work (auth-broker, harbor-monitor, network-reach). Those each solve a concrete problem; this one names what they're collectively *building toward* — and what's missing before the picture closes.

---

## 1. The shape of the problem

A personal computing fabric increasingly contains a population of **agents** (Claude instances doing real work in bounded environments) running across multiple machines. Three pressures emerge that V1 didn't have to answer:

1. **Living credentials decay.** Claude Code OAuth tokens, AWS STS leases, Tailscale auth keys — none of these are static. Some refresh in-memory but never write back; some need periodic re-minting from a master credential; some require interactive re-login. The archipelago drifts to expired auth overnight if no living thing tends it.

2. **Standing entitlements are too coarse.** A drydock's project YAML declares everything it might ever need at create time. In practice agents discover dependencies dynamically — a research drydock needs `huggingface.co` it didn't list; a coding drydock needs to fetch from a new package mirror. The current resolution is "edit YAML, recreate container," which kills any pretense of dynamic operation. Either you over-grant up front (broad attack surface, nothing learned about actual reach) or you constantly interrupt the agent to recreate.

3. **Compromise has no resource ceiling.** A Dockworker that goes wrong — confused agent, prompt-injected agent, genuinely compromised agent — can today: spin CPU indefinitely, fill disk, exfiltrate at line rate over the (admittedly bounded) egress allowlist, burn Anthropic API quota, and hold any granted lease forever. The capability-broker prevents the worker from *acquiring new permissions* it shouldn't have, but does nothing about *abuse of permissions it does have*.

These three pressures share a structure: each demands a **continuously-acting agent that the principal trusts more than any individual worker, but less than the principal themselves.** That's the Harbormaster pattern.

---

## 2. What drydock has built so far

V1 (complete, per `project_v1_complete.md`):

- **Container-as-bounded-environment.** Every drydock is a devcontainer with default-deny iptables, tailnet-isolated network, per-drydock secret directory bind-mounted at `/run/secrets/`, and a per-drydock identity recorded in the SQLite registry on the Harbor. Nothing crosses the boundary except via mediated channels.
- **Capability broker** (`docs/design/capability-broker.md`, implemented in `src/drydock/wsd/capability_handlers.py`). Workers request capabilities (`SECRET`, `STORAGE_MOUNT`, soon `NETWORK_REACH`); the daemon validates against per-drydock entitlement (`narrowness`), materializes the lease into the worker's `/run/secrets/`, and audits. Subject identity is derived from the bearer token, not RPC arguments — confused-deputy-resistant by construction.
- **Narrowness model** (`narrowness.md`). Per-drydock policy declarations that the broker reads to gate every grant. Today: `delegatable_secrets`, `delegatable_storage_scopes`, `request_*_leases` capability flags. Tomorrow: `network_reach`, resource ceilings, escalation thresholds.
- **Employee-worker pattern** (`employee-worker.md`). The `infra` drydock on Hetzner already plays this role for credentials — it holds master OAuth state, runs `claude remote-control` to keep tokens fresh in-memory, and delegates leases to peer drydocks via the broker.
- **Deskwatch** (`deskwatch.md`). Per-drydock health observation: scheduled-job outcomes, output freshness, probe results. Observes; does not act.
- **Audit log.** Every capability grant, every secret access, every administrative action recorded. The forensic backbone.

In flight (designs landed, implementation partial or pending):

- **Auth-broker** (`auth-broker.md`). Designated Harbor holds master refresh token, mints fresh access tokens on a 4h cadence, distributes to peer Harbors. Closes the "laptop closed → archipelago drifts to expired" gap.
- **Harbor-monitor** (`harbor-monitor.md`). Central observer that probes every peer Harbor (daemon liveness, deskwatch, **CC liveness** — the headline distinguishing "container up" from "container up but agent dead"). V1 ships SSH-shell channel + `ws harbors status/probe`; storage and Telegram alerts deferred.
- **Network-reach** (`network-reach.md`). `NETWORK_REACH` capability type. Workers request `RequestCapability type=NETWORK_REACH scope.domain=foo.com`; daemon validates against per-drydock `narrowness.network_reach` glob list; materializes via `add-allowed-domain.sh` inside the container (resolves A records, syncs to ipset, opens iptables rule for non-default ports). No restart. Additive-only V1.

The pattern under all of these: **anything the worker wants from the outside flows through a broker the principal controls.** Recent work just extends "anything" — first secrets, then storage, then living credentials, then network reach, eventually compute quotas.

---

## 3. The landscape — how others solve adjacent problems

Worth being honest about prior art so the design choices read as choices, not invention.

| Approach | What it does well | Why it doesn't fit |
|---|---|---|
| **DevPod / GitHub Codespaces / Devcontainers** | Reproducible bounded environments per project. | No agent-mediated dynamic capability grants. Config is static; new entitlements require rebuild. No notion of a Harbormaster that polices runtime behavior. |
| **Kubernetes RBAC + Pod Security Standards** | Mature, well-audited capability gates. ServiceAccounts as agent identity. | Pod spec is mostly static. Network policy is namespace-shaped, not per-call dynamic. Designed for service operators, not for agents that *discover* what they need. The control plane is the principal, not the agent. |
| **gVisor / Firecracker / microVM sandboxing** | Strong isolation primitives — kernel attack surface drastically reduced. | Orthogonal to the policy question. They make "what's inside the box" safer; they don't speak to "what should the box be allowed to ask for over time." |
| **Capability OS (seL4, Genode)** | The principled foundation for what we're approximating. | Doesn't compose with the Linux ecosystem agents actually need. Decades-long bet, not a year-long bet. |
| **AWS IAM + STS short-lived credentials** | Closest production analogue to what drydock's broker does. Identity-bound, time-bounded, narrowly-scoped. | Cloud-only, vendor-specific, and the policy authoring surface is famously hostile. Drydock's STS-lease pattern (Phase B `INFRA_PROVISION`, per `project_phase_b_infra_provision.md`) already mirrors this — IAM is the model, not the substrate. |
| **OpenAI / Anthropic agent sandboxes** (browser-tool, code-interpreter) | Per-task egress firewalls, ephemeral envs, principal-of-last-resort is the platform. | Stateless model. No notion of a *persistent* drydock with policy carried over time. No multi-Harbor archipelago shape. Principal is the platform vendor, not you. |
| **Cloudflare Workers / Deno Deploy** | Capability-per-isolate, deny-by-default network. | Compute model is request/response, not "an agent that lives in a drydock and works on a project." |
| **Tailscale ACLs** | Identity-based network policy across an overlay. | Tailnet-wide configuration, not per-container per-domain dynamic. The right tool for *peer* policy (Harbor↔Harbor), not *worker* policy. Drydock already uses Tailscale this way. |
| **macOS Seatbelt / SELinux / AppArmor** | Process-level mandatory access control. | Policy is the developer's, not a separable principal's. No notion of an agent requesting a wider grant over time. |
| **Smallweb / personal-cloud projects (e.g. Coolify, Caprover)** | Self-hosted multi-app fabrics with deploy automation. | Operator-shaped (humans deploy services); not agent-shaped (agents request capabilities). |

The synthesis drydock is converging on isn't novel in any one dimension — it's the **combination of bounded environments + capability broker + persistent Harbormaster + per-drydock narrowness policy** at *personal* scale (one principal, ~10s of drydocks, no platform team). The closest analogue in spirit is what good cloud-engineering teams build internally on top of IAM + cgroups + an internal control plane — drydock just collapses that stack to a single human's home archipelago.

---

## 4. The principal–Harbormaster–Dockworker triangle

Three roles. The whole point of the architecture is keeping their action spaces separate.

### Principal (you)

- Holds the **master OAuth identity** (Mac keychain remains the seed-of-record for Claude Code).
- Authors **policies at two levels**:
  - **Global** (`~/.drydock/policy/global.yaml`) — archipelago-wide defaults, escalation thresholds, Harbormaster authority caps, default resource ceilings, the principal's standing answers ("any new domain request from a `*-research` drydock auto-approves; from anything else escalates").
  - **Per-drydock** (project YAML's `narrowness:` block, plus `~/.drydock/policy/<desk>.yaml` for principal-only overrides the project author shouldn't touch) — drydock-specific entitlements, ceilings, and exception flags.
  - Per-drydock policy *narrows* global policy, never widens it. The Harbormaster's effective policy for a drydock is the conjunction.
- Holds the **out-of-band kill switch** for the Harbormaster (described below — this matters).
- Reviews **escalations** the Harbormaster raises (Telegram, eventually a richer review channel).
- Reads **audit** to learn what's happening when they were asleep.

The principal does *not* want to be in the loop for every grant, every refresh, every routine resource decision. That's why the Harbormaster exists.

### Harbormaster (Harbor agent)

A persistent employee-worker (`employee-worker.md`) running on a designated Harbor — concretely, the `infra` drydock on Hetzner, eventually upgraded with the auth-broker + harbor-monitor + governance roles described here. **Has master keys for archipelago operation but is constrained against itself.**

What the Harbormaster *can* do (within standing principal policy):

- Mint and distribute fresh access tokens to peer drydocks (auth-broker role).
- Grant capability leases that fall within standing per-drydock narrowness — secrets, storage, network reach.
- Observe archipelago health (harbor-monitor role) including **from inside containers** via a thin in-desk probe (see §5d), and trigger remediation that the principal has pre-authorized (e.g., "auto-refresh credentials when CC liveness fails").
- **Accept workload registrations** from workers that are about to do heavy lifting (see §5c) — granting temporary ceiling lifts within policy, refusing them otherwise.
- **Stop, throttle, or restart-the-agent-inside** Dockworkers that exceed resource ceilings or whose agents are dead-but-container-healthy (the new layer this doc proposes).
- **Talk to the principal over Telegram** in both directions: emit audit summaries and probe results on a schedule; ask for quick confirmation on novel requests; propose policy updates ("I keep escalating `huggingface.co` for this drydock — promote to standing entitlement?") that the principal accepts/rejects with one reply.
- Escalate to the principal anything outside its authority — wildcard requests, novel domains, repeated violations, suspected compromise.

What the Harbormaster *cannot* do — these are structural, enforced by the substrate, not by the Harbormaster's good behavior:

- **Modify its own policy.** The narrowness file the Harbormaster reads to make decisions is owned by the principal, in a path the Harbormaster has read-only access to (`~/.drydock/policy/`, root:0400 with the Harbormaster's container UID having no write privilege). Changing what the Harbormaster is allowed to do requires the principal's hand on the host filesystem.
- **Rotate or read its own master credentials.** The master refresh token (`~/.drydock/daemon-secrets/claude_master_refresh_token`) is owned by `wsd`, not the Harbormaster drydock. The Harbormaster can request a token-refresh *operation* via wsd RPC, but never reads the refresh token directly. wsd is the single trusted reader.
- **Modify the audit log.** Audit is append-only at the wsd layer; the Harbormaster can `query` audit but has no write surface.
- **Manage its own container.** The Harbormaster has no `ws` CLI access on the Harbor. It cannot stop, restart, upgrade, or destroy itself or any other drydock *directly*. It calls wsd RPC methods that are scoped to specific operations (e.g., `StopDesk`, gated by capability) — and those methods have policy guards that reject self-targeting.
- **Change its own resource limits.** The cgroup ceilings the Harbormaster operates under are set at container creation by the principal, recorded in the registry, and unchanged for the container's lifetime.
- **Reach the principal's machine** beyond the audit-emit + escalation-notify channels. The Harbormaster has no shell on the principal's Mac, no write access to the principal's notebooks, no ability to exfiltrate.

The Harbormaster's powers are **broad but shallow**: it can act across the whole archipelago, but every action it takes is one of a small set of pre-defined operations, each gated by principal policy, each audited. This is the inverse shape of a normal Dockworker, which has **narrow but deep** powers (one project's worth of code, but free to run arbitrary computation within its bounds).

### Worker

A regular project drydock. Bounded environment, narrow capabilities, can run arbitrary code within its bounds, requests broader capabilities from the broker on demand. Subject to resource enforcement by the Harbormaster. Audited.

Dockworker compromise is the threat model the Harbormaster exists to mitigate.

### The shape that falls out

```
                principal
                /       \
        policies         escalations
              \         /
               Harbormaster
              /  |  \
        observe/grant/throttle
            /    |    \
         worker worker worker  ...
```

The principal talks to the Harbormaster *through standing policy* and *through escalation review*. The Harbormaster talks to workers *through capability grants* and *through enforcement actions*. The principal talks to workers *only through the Harbormaster* in steady state — direct intervention is reserved for cases the Harbormaster escalated or for explicit ad-hoc work the principal initiates.

This is roughly the same pattern as a senior engineer running an on-call rotation: the senior writes runbooks (policy), the on-call executes them (Harbormaster), services keep running (workers), the senior is paged for novel situations (escalation). Drydock's contribution is making this work for a single human's archipelago of agent Docks rather than a team of humans operating services.

---

## 5. Resource governance against compromised Dockworkers

The capability broker prevents a worker from acquiring permissions it shouldn't have. It says nothing about a worker abusing permissions it *does* have. That's the gap this section names and proposes filling.

### What "compromised Dockworker consuming too many resources" actually looks like

| Vector | Example | Currently bounded by | Currently unbounded |
|---|---|---|---|
| CPU | Mining loop, runaway recursion | `docker --cpus` if set at create time | Default is unlimited; nothing dynamic |
| Memory | Loading a 50GB model into RAM, leak | `docker --memory` if set | Same — static, not enforced after |
| Disk (volume) | Filling the workspace volume with logs / downloads | Filesystem quota if set; usually not | No observation, no alerting |
| Network egress bandwidth | Exfil within the allowlist (`huggingface.co` legitimately allows large downloads) | iptables allows or denies; doesn't rate-limit | No bandwidth ceiling; no anomaly detection |
| Anthropic API quota | Worker burns through daily token budget on a loop | Anthropic enforces account-level rate limits | No per-drydock allocation; no early warning |
| AWS API calls | Worker enumerates S3 buckets, runs costly queries | STS lease scope limits *what* it can do, not *how much* | No call-rate ceiling; cost surprise |
| Fork / process count | Fork bomb, runaway subprocess tree | `ulimit -u` if set; cgroup pids if set | Often default-unlimited |
| Lease lifetime | Holding `claude_credentials` lease forever | Today: leases are "until revoked"; nothing revokes | No TTL enforcement, no idle-revoke |

The pattern: **detection** is partly there (deskwatch can notice job failures, output staleness; harbor-monitor can notice silent peers; cgroup metrics are readable). **Judgment** is missing (when does "high CPU" cross from legitimate to abuse?). **Action** is missing (revoke leases, throttle, stop the drydock, alert the principal).

The Harbormaster is the natural home for judgment + action. Detection feeds it; standing principal policy bounds what it does without escalation.

### The proposed enforcement layer

Three pieces, each layering on existing primitives:

**(a) Standing resource ceilings, set by the principal in project YAML.**

```yaml
narrowness:
  ...
  resources:
    cpu_max:           2.0       # docker --cpus
    memory_max:        4Gi
    workspace_disk_max: 20Gi
    egress_bandwidth_max: 10Mbps
    api_call_budget:
      anthropic_tokens_per_day: 1_000_000
      aws_calls_per_hour:       1000
    process_max:       512
    lease_ttl_default: 8h        # leases auto-expire after this
```

The hard ones (cpu, memory, pids, disk-via-volume-quota) are cgroup/docker-flag enforced at container creation — the substrate enforces them, the Harbormaster doesn't have to. The soft ones (egress bandwidth, API budgets, lease TTL) need the Harbormaster to observe + enforce in software.

**(b) Harbormaster as the judgment + action layer.**

The Harbormaster reads aggregated metrics from harbor-monitor (extended to capture cgroup stats and broker-tracked API spend) and applies per-drydock policy. Roughly:

```
foreach drydock:
  metrics = fleet_monitor.query(desk)
  policy  = principal.policy(desk)
  for ceiling in policy.resources:
    if metrics.exceeds(ceiling):
      action = policy.violation_action(ceiling)   # warn | throttle | revoke | stop | escalate
      Harbormaster.act(desk, action)
      audit.emit(...)
```

Standing actions the Harbormaster can take without escalating:
- Emit a deskwatch violation (visible in `ws deskwatch`).
- Revoke an in-flight capability lease (lease_id known from broker state).
- Throttle egress via tc/htb on the container's veth (requires Harbor-side sudo on iptables/tc — already granted to wsd).
- Stop the drydock (`StopDesk` RPC — equivalent to `ws stop`, preserves volumes/worktree).
- Notify the principal (Telegram, with full context).

Standing actions the Harbormaster *cannot* take without escalation:
- Destroy a drydock (data loss).
- Modify the drydock's project YAML or narrowness policy.
- Override a ceiling.
- Take any action on a drydock marked `principal_review_only` in policy.

**(c) Workload registration — declared intent before bursting.**

A worker about to do something heavy *registers* the workload with the Harbormaster before starting. Not "ask permission" exactly — more "declare intent, get a lease, get observed against the declaration." The Harbormaster then distinguishes "expected burn" from "anomalous burn."

Shape:
```
RegisterWorkload {
  kind:        "training" | "crawl" | "batch" | "interactive" | "experiment"
  description: "fine-tune llama-3-8b on internal corpus"
  expected_resources: {
    cpu_hours:           4.0
    memory_peak:         12Gi
    disk_writes:         50Gi
    egress_bytes:        2Gi
    anthropic_tokens:    50_000
    duration_max:        2h
  }
  domains_needed: ["huggingface.co", "*.s3.amazonaws.com"]
}
→ WorkloadLease { id, expires_at, granted_resources, granted_domains }
```

The Harbormaster's response is policy-driven. Within standing ceilings: auto-grant, lift the per-drydock soft caps for the workload's duration, fold `domains_needed` into a temporary network-reach lease. Above standing ceilings: escalate to principal with the registration as the structured ask. After the workload completes (or `WorkloadLease` expires): caps snap back; deviation between declared and actual goes into audit.

The trick this enables: the *normal* per-drydock ceilings can be tighter (because legitimate heavy work is expected to register and lift them), which means runaway-without-registration is detected almost instantly — anything that breaks ceilings without a corresponding `WorkloadLease` is per-definitionem unexpected.

**(d) In-desk probe — observation from inside the container.**

Aggregate cgroup metrics from outside the container are coarse (total memory, total CPU). What's actually happening — which process is hot, what file descriptors are open, what's writing to disk — needs an observer inside. A thin probe shipped in `drydock-base` (call it `desk-probe`, ~one binary, no Claude inside it) runs as an unprivileged service in the container, exposes a read-only RPC to wsd over the existing in-desk-RPC channel:

```
desk-probe.snapshot → {
  processes:    [{pid, cmd, rss, cpu_pct, age}, ...]
  fds:          {open_files, sockets}
  disk_growth:  {workspace_bytes_delta_5min, log_bytes_delta_5min}
  agents:       [{name, pid, status, last_request_ts}, ...]
}
```

The probe **observes only**; it has no authority to act. The Harbormaster reads its snapshots via wsd, joins them with cgroup-side metrics from the Harbor, and judges. Keeping the probe inert is what keeps the in-container attack surface negligible — a compromised Dockworker that escalates to the probe gains read-only inspection of itself, nothing more.

**(e) Lease TTL and idle revocation.**

The current capability broker treats leases as "until revoked." Combined with a `lease_ttl_default` policy, the Harbormaster can sweep idle leases — if a drydock holds a `claude_credentials` lease but hasn't made an outbound call in N hours, revoke it. Forces the drydock to re-request, which re-runs the entitlement check (potentially in a stricter policy if the principal has tightened things since).

This also closes the network-reach TTL gap left open in V1 of `network-reach.md` — the Harbormaster + lease registry is the right home for it, rather than building a per-feature reaper.

**(f) Agent restart inside a healthy container.**

A common failure mode in live operation: the *container* is healthy (network up, volumes mounted, all init scripts succeeded) but the *agent process* inside it is dead — Claude Code's remote-control crashed, OAuth went stale past in-memory recovery, or the process simply needs to re-read freshly-pushed credentials from disk. Today's only recourse is `ws stop && ws create`, which churns a perfectly good container.

A new primitive — call it `RestartDeskAgent` — sits between `ws exec` (shell in) and `ws stop` (recreate container). Implementation: each drydock declares its agent processes in project YAML by command-line label or PID-file convention; `wsd` invokes a small in-container script (`restart-agent.sh`, shipped in `drydock-base`) that signals the named agent(s) for clean restart and verifies they came back up. The Harbormaster uses this when CC-liveness probes fail but the desk-probe shows the container is otherwise fine. Audited like any other Harbormaster action.

This composes cleanly with the auth-broker: when fresh credentials are pushed to a drydock that was already running, the Harbormaster can immediately `RestartDeskAgent` to make the agent re-read them, instead of waiting for the next natural restart.

### What the principal sees — bidirectional Telegram

In normal operation: a quiet, scheduled summary (daily or on-demand). The Harbormaster handles routine grants, refreshes, throttles. Audit log is the durable record; Telegram is the read-the-room channel.

```
📊 Harbormaster daily — 2026-05-05
  archipelago: 2 harbors, 6 docks healthy, 1 throttled (auction-crawl @ hetzner, see below)
  grants: 24 capability leases (22 routine, 2 escalated → approved)
  burn:   ~340k anthropic tokens (within budget)
  drift:  none
  proposal: huggingface.co requested 4× by ml-sandbox this week.
            promote to standing entitlement? (reply 'yes' / 'no')
```

In abnormal operation: structured escalation, principal replies with one word to act.

```
🟡 Harbormaster escalation: dock auction-crawl @ hetzner
  reason: anthropic_tokens_per_day exceeded (1.4M / 1M)
  trajectory: linear since 03:00 UTC; likely crawl loop
  Harbormaster action: throttled api_call_budget to 0 until principal review
  context: last 5 prompts + last 20 capability grants
  → reply 'release auction-crawl' to lift; 'stop auction-crawl' to terminate
```

The channel is **bidirectional** — the principal can also push:
- `audit auction-crawl 24h` → Harbormaster returns a structured summary
- `policy raise ml-sandbox memory_max 16Gi` → Harbormaster proposes a YAML diff, principal confirms, Harbormaster writes it (audited as a principal-authored change, Harbormaster is just the typist)
- `restart desk-agent foo` / `stop foo` / `release foo` — direct Harbormaster actions

Bidirectionality matters because the principal often *wants* to know things at random times ("what's auction-crawl been up to today?") without sshing into Hetzner. The Harbormaster's job is to make the answer one message away.

The principal's job, in either direction, is to confirm/redirect, not to compute the response from scratch.

### What stops the Harbormaster itself from being the threat

Three things:

1. **Structural constraints (section 4).** The Harbormaster cannot rewrite its own policy, cannot read its own master credentials directly, cannot modify audit, cannot reach the principal's machine.
2. **Out-of-band kill switch.** The principal's Mac retains the master refresh token and the keychain-extract path (`scripts/mac/claude-refresh.sh`). If the Harbormaster is suspected compromised, the principal revokes the Harbormaster's `wsd` bearer token from the Mac, rotates the master refresh token, and re-seeds a clean Harbormaster. This is the path that doesn't depend on the Harbor or the Harbormaster's cooperation.
3. **Audit asymmetry.** The Harbormaster emits to audit, can read audit, but cannot delete or modify entries. If the Harbormaster starts behaving badly, the record exists. (Substrate-level enforcement: `wsd` is the only writer, audit file is `root:0600`, Harbormaster container has no privileged mount.)

A compromised Harbormaster is a serious incident, but it's a *recoverable* incident with a forensic trail. That's the property that makes the architecture worth running.

---

## 6. How the recent work fits, and what's still missing

Mapping the recent designs onto the triangle:

| Design | Role in the triangle |
|---|---|
| `capability-broker.md` | The **mediation channel** between worker and Harbormaster. The Harbormaster's grants flow through this; the workers' requests flow through this. |
| `narrowness.md` | The **policy-as-data** the Harbormaster reads. Authored by principal. |
| `employee-worker.md` | The **slot the Harbormaster lives in** — a persistent Harbor desk. Names the pattern. |
| `auth-broker.md` | A **Harbormaster responsibility**: keeping archipelago OAuth state alive without principal involvement. |
| `harbor-monitor.md` | The **Harbormaster's senses**: how it learns what's happening across the archipelago. CC-liveness probe is the canonical signal. |
| `network-reach.md` | The **per-domain capability** the Harbormaster grants on request. First dynamic capability that mid-task discovery actually needs. |
| `deskwatch.md` | The **per-drydock health observer**. Feeds signal into harbor-monitor → Harbormaster judgment loop. |
| `tailnet-identity.md` | The **substrate identity** that lets the Harbormaster reach peer Harbors safely. |

What's not yet designed (and should be, in roughly this order):

1. **Resource ceilings + workload registration in `narrowness:`** — schema, registry columns, project-config parser. Hard ceilings (cgroup) at create time; soft ceilings recorded for the Harbormaster; `RegisterWorkload` RPC spec.
2. **Lease TTL + idle revocation** — extend the broker with `expires_at` and an idle sweeper. Closes the V1 hole in network-reach and any future capability.
3. **`RestartDeskAgent` primitive** — new wsd RPC + in-container `restart-agent.sh` helper + project-YAML `agents:` declaration block. Sits between `ws exec` and `ws stop`.
4. **`desk-probe` in-container observer** — minimal binary in `drydock-base`, read-only RPC, exposes process/fd/disk/agent snapshots to wsd.
5. **Harbormaster authority surface** — what RPC methods does the Harbormaster have access to that workers don't? `StopDesk`, `ThrottleDesk`, `RestartDeskAgent`, `RevokeLease`, `ReadHarborMetrics`, `RegisterWorkload` (write-side). Define the bearer-token scope for "Harbormaster" as a distinct grade of identity, structurally distinct from worker scope.
6. **Principal policy file format** — global at `~/.drydock/policy/global.yaml`, per-drydock at `~/.drydock/policy/<desk>.yaml`. Read-only to Harbormaster; reload on file change. Eventually git-backed for history. Schema enforces "per-drydock narrows global, never widens."
7. **Bidirectional Telegram channel** — outbound (escalations, daily summaries, proposals); inbound (one-word confirms, audit queries, direct actions). Per `1439fce collab` this side-channel already exists; needs a Harbormaster-shaped front door.
8. **Out-of-band kill protocol** — document the Mac-side procedure for revoking and re-seeding a compromised Harbormaster. This is more runbook than design doc, but it should exist before the Harbormaster holds enough power to matter.

What's *deliberately* deferred:

- **Federated multi-Harbormaster.** Per `project_peer_harbors_decision.md`, the peer-Harbors model is sovereign-peers, not federated. One Harbormaster per archipelago. Revisit when islanding pain is real.
- **Harbormaster as judgment-LLM vs deterministic policy-engine.** Today most Harbormaster decisions are policy-table lookups (entitlement match, ceiling comparison). Some are genuinely judgmental ("is this novel domain request reasonable?"). V1 Harbormaster can be mostly deterministic with escalation-on-uncertainty; promote to LLM-in-the-loop only where the determinism breaks down. Avoid making the Harbormaster itself a black box.
- **Cross-principal delegation.** The model assumes one human principal. Multi-user (`project_multi_user_sketch.md`) is an orthogonal axis; the principal-Harbormaster-worker triangle composes inside each principal's slice but the federation across principals is its own design.

---

## 7. The bet

Drydock's bet is that the right boundary for personal agent infrastructure is **bounded environment + mediated capability + persistent Harbormaster + principal-as-policy-author**. None of those primitives is novel; the combination, at one-human scale, is.

The cost is real: every new "thing the worker wants" becomes a capability type. The narrowness vocabulary keeps growing. The Harbormaster's surface keeps widening. There's a version of this where it collapses under its own ontology weight.

The bet pays off if it makes the steady-state **boring**: agents work, credentials refresh, novel requests escalate when they should and not when they shouldn't, compromise is contained and recoverable, the principal sleeps. The infrastructure exists to make that the default outcome rather than the lucky one.

The recent work — auth-broker, harbor-monitor, network-reach — is three more pieces of "agents work, credentials refresh, novel requests escalate." The resource-governance layer described in §5 is the missing piece for "compromise is contained and recoverable." Once that lands, V2 of the fabric has the shape it needs; everything after is filling in capability types and tightening the policy vocabulary.
