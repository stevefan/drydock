# Port Auditor

**Status:** sketch · **Pulls from:** [vocabulary.md](vocabulary.md) §"Harbor Authority + Port Auditor split", [principal-friction.md](principal-friction.md), [amendment-contract.md](amendment-contract.md), [harbor-authority.md](harbor-authority.md)

## What the Port Auditor is

The observation + judgment + bounded-defensive-action role on a Harbor. The Watchful Pier-Walker with a fire-axe.

**Reads** (full read access on its Harbor):
- Audit log
- Cgroup metrics for every Drydock
- Capability lease ledger
- Workload registrations
- Amendment queue
- Cross-Harbor metrics via peer-RPC (when implemented)

**Acts** (Bucket 2 — defensive + reversible — see vocabulary.md §"Harbor Authority + Port Auditor split"):
- Throttle egress on a Drydock
- Stop a Drydock (state preserved; resumable)
- Freeze storage
- Revoke a lease
- Shut down cloud resources (when cloud backends exist)

**Escalates** (Bucket 3 — destructive / expensive / irreversible):
- Always to the principal via Telegram
- Includes context: what was observed, why it matters, recommended action

**Cannot do** (structurally — bearer-token scope rejects):
- Destroy a Drydock (data loss)
- Delete a volume
- Provision new compute / spend money
- Modify policy files
- Rotate master credentials
- Anything in Bucket 3

## Personality

The Watchful Pier-Walker with a fire-axe. Observant, patient, narrative-aware, knows the harbor's history (audit log is its memory). Talks to the principal in plain language. Has views about what's normal and what's worth noting. Knows the difference between "unusual but not urgent" (escalate, wait) and "actively damaging" (act + escalate). Comfortable saying "I don't know — flagging for you."

Never says "I checked, looks good" — affirming the principal's intent makes the principal less attentive, which is the opposite of the goal. The Auditor's job is to draw attention to what might be missed, not to certify what's clearly fine.

## What the Auditor is made of

Three layers, separated for good engineering reasons:

**Layer 1 — deterministic measurement (Python service, long-running).** Polls cgroup stats, reads broker lease accounting, scans audit log, computes declared-vs-actual diffs. Writes structured facts to a local SQLite. Exposes facts via a local read API. Pure code; no LLM. This is the foundation — even without the LLM layers, the principal can query measurements directly.

**Layer 2 — Watch loop (cheap LLM, scheduled).** Every minute (or whatever cadence), a small inference call (Haiku-class model) reads recent facts and asks "anything anomalous?" Tool calls limited to `read_more_context` + `classify`. Output is one of: `routine` (silent log), `anomaly_suspected` (escalate to Layer 3), `unsure` (escalate to Layer 3 with low confidence). Cost: cents per day at steady state.

**Layer 3 — Deep analysis (bigger LLM, event-triggered).** Invoked when Layer 2 escalates OR when a novel amendment needs framing OR when the principal asks a question. Sonnet/Opus-class. Full toolset (`read_metrics`, `read_audit`, `read_workloads`, `compare_declared_actual`, plus the bucket-2 action calls and `escalate_telegram`). Decides: act / Form-A note / escalate to principal / silent log. This is where the real judgment happens. Cost: dollars per day on a busy fleet, dominated by genuine anomalies.

The architecture is **NOT a long-running Claude Code remote-control session.** It's a Python service + scheduled-and-event-triggered LLM calls via the Anthropic API. Each LLM call is mostly stateless (state lives in the audit log + measurement DB); the LLM gets the relevant context per-call. This is much cheaper and more operationally predictable than a persistent agent session.

**Why tiered models matter:** the watch loop runs constantly (1×/minute = 1440×/day). Doing that with Sonnet would cost too much for the value (most checks are "everything is routine"). Haiku as the always-on classifier + Sonnet/Opus as the always-available-on-escalation gives you good coverage at low cost. This is a well-trodden production pattern (small classifier → large reasoner) and it composes naturally with the Auditor's role.

## Where it lives

A dedicated DryDock on each Harbor — `port-auditor` Drydock. Its Fleet contains the Auditor's processes (probably one container running both the measurement service and the LLM-call orchestrator; potentially split into two Ships if it grows). Its container has:

- The measurement-layer Python service running as a long-lived process
- The watch-loop scheduler (cron-like; calls Haiku-class model every minute)
- The deep-analysis dispatcher (called on demand by watch-loop or amendment queue)
- A bind-mount of the audit log (read-only)
- A bind-mount of `~/.drydock/run/wsd.sock` (broker access for action calls)
- A bearer token with scope `auditor:read + auditor:defensive_action`
- Telegram bot credentials (via secret) for principal channel

**Anthropic API access — own key, with OAuth fallback.** The Auditor needs LLM access *independent of the principal's OAuth*. Reasons:
- The Auditor must keep functioning when the principal is asleep / unavailable.
- The Auditor must stay operable precisely when principal's OAuth has issues — that's exactly the scenario we need it most.
- The Auditor's spend should be its own line item (auditable, capped) not pooled with principal's.
- Compromise of principal's OAuth shouldn't compromise the Auditor.

So: the Auditor has its own Anthropic API key, stored as `anthropic_api_key` secret in its Drydock. OAuth login (using `claude /login` flow) is the backup if the API key is revoked or rate-limited.

Drydock-level resource ceilings on the Auditor's spend (e.g., `anthropic_tokens_per_day: 100_000` for Haiku, `200_000` for Sonnet on top) are themselves bounds the principal sets. The Auditor running away on its own API spend is one of the things the Auditor can't catch (it'd be auditing itself), so the principal pre-commits to bounded spend at the Drydock-create level.

It's a Drydock like any other, with one difference: its bearer-token scope is *unusual*. Normal Drydocks get scoped capabilities for their own resources. The Auditor gets read-everything (across all Drydocks on this Harbor) plus the bucket-2 action set.

## Tools available to the Auditor's LLM

```
read_metrics(drydock=<id>, window=<duration>)
  → returns cgroup CPU/memory, egress bytes, broker lease activity for the window

read_audit(filter=<query>)
  → returns audit events matching filter

read_workloads(drydock=<id>, status=<active|completed|all>)
  → returns workload registrations + their declared profiles

read_amendments(status=<pending|escalated|all>)
  → returns amendment queue

compare_declared_actual(workload_id=<id>)
  → returns structured diff: declared resources vs measured

# Bucket-2 actions (call Authority's RPCs with reasoning attached):
throttle_egress(drydock_id, max_bps, reason)
stop_drydock(drydock_id, reason)
freeze_storage(volume_id, reason)
revoke_lease(lease_id, reason)
shutdown_cloud_resource(resource_id, reason)   # future, when cloud backends exist

# Friction + escalation:
add_form_a_note(amendment_id, note)
escalate_telegram(severity, message, context_dict)
propose_policy_amendment(amendment_envelope)    # the principal must approve
```

What's deliberately absent:
- Anything from Bucket 3 (destroy, delete, provision, spend)
- Direct file-system access (the Auditor can't read or write `~/.drydock/policy/`; it can only *propose* policy via the amendment contract)
- Direct broker-state mutation (it goes through Authority's RPCs which audit the call)

## When the Auditor acts vs escalates

The decision tree (the LLM follows this; it's simple enough to be deterministic but LLM-rendered for adaptive context):

1. **Is the action in Bucket 1?** → not the Auditor's job. Authority handles deterministically.
2. **Is the action in Bucket 3?** → escalate. Cannot act regardless of context.
3. **Is the situation actively damaging?** (e.g., runaway resource consumption, suspected compromise, leaked credential being abused)
   - Yes → take the relevant Bucket-2 action immediately AND escalate (synchronous Telegram). Reasoning attached.
   - No → continue to step 4.
4. **Is the situation unusual but not damaging?** (e.g., declared-vs-actual diverging modestly; novel amendment proposal)
   - Add Form A note + escalate (async Telegram for principal review).
5. **Is the situation routine?** → silent log, no action.

Steps 3-5 use LLM judgment; the LLM has tools to gather context before deciding.

## Output contract — what the Auditor's Telegram messages look like

Every Telegram message from the Auditor follows a structured shape:

```
<emoji> <event-class>: <subject>
<one-line context>
<key facts, bullet-style if multiple>
<recommended action OR amendment proposal>
→ reply '<word>' to <effect>
```

Examples:

```
🔴 dock auction-crawl @ hetzner: actively damaging
egress 8× declared (40Gi vs 5Gi) and growing
auditor action: throttled egress to 0 (revertible)
context: pattern is mass-export to govdeals.com mirror IPs
→ reply 'investigate' for details, 'release' to lift throttle, 'kill' to stop dock
```

```
🟡 amendment escalation: auction-crawl @ hetzner
kind: network_reach
request: open evil.com:443
reason from Dockworker: "third-party API for X"
auditor note: unusual — first external-domain request in 6 months for this dock
→ reply 'approve' / 'deny' / 'investigate'
```

```
🔎 daily summary: archipelago 2026-05-05
2 harbors, 8 docks, all healthy
budgets: 60% of microfoundry yard token budget used; auction-crawl peaked at 80% during deep-crawl 03:00-05:00 UTC
amendments: 24 routine (auto-approved), 1 escalated → approved (huggingface.co for ml-sandbox)
no anomalies worth flagging
```

The third example is the steady-state rhythm: a single brief daily summary. Quiet most days. Loud only when there's reason to be.

## Implementation phasing

**Phase PA0 — measurement layer only (no LLM):** the deterministic Python service that polls cgroup stats + reads audit + computes diffs. Writes to local SQLite; exposes read API. Useful on its own — the principal can query it manually via CLI. No LLM yet.

**Phase PA1 — Watch loop with cheap LLM (Haiku):** the scheduler fires a Haiku-class call every minute against recent measurements. Output is structured: `routine | anomaly_suspected | unsure`. On `routine`, log silently. On `anomaly_suspected | unsure`, surface to the principal as a daily summary (no autonomous action yet). Cost: cents per day. Useful as a calibration period.

**Phase PA2 — Deep analysis with bigger LLM (Sonnet/Opus), no action yet:** when watch-loop escalates or amendment queue gets a novel proposal, invoke deep analysis. The bigger model does framing + recommends action via Telegram to principal. Principal acts via direct CLI. Auditor still has NO action authority — it just makes recommendations.

**Phase PA3 — Bucket-2 action authority:** Auditor's bearer token gets the `auditor:defensive_action` scope; its tools include the action-RPCs. Throttle, stop, freeze, revoke become available to the deep-analysis layer's judgment. The watch-loop never acts directly; only deep-analysis does.

**Phase PA4 — friction enforcement:** the Auditor reads the friction policy + handles Form A note generation, Form B/C challenge presentation in Telegram. Form A notes can come from either the watch loop or deep analysis (deep-analysis-generated notes carry more context).

This phasing is conservative on purpose. PA0 is pure code (no LLM, no judgment). PA1-2 add LLM judgment with no action authority (calibration). PA3 raises the action authority (with structural defense — bearer-scope rejection of bucket-3). PA4 layers in the friction model. Each phase has time to observe before escalating to the next.

## Bootstrapping

The first Auditor is principal-created via `ws auditor designate <drydock-name>`. This:
- Creates the `port-auditor` Drydock with the right base image + secrets
- Issues an `auditor:read + auditor:defensive_action`-scoped bearer token
- Records in `harbormaster_desks` (or a more-correctly-named-now `auditor_desks`) registry table that this Drydock holds the Auditor scope
- Refuses if there's already an Auditor on this Harbor (one per Harbor by design)

The principal can later destroy / re-create the Auditor; on destroy, audits all pending amendments/escalations move to a no-Auditor state where they queue but can't be processed (visible warning in `ws host audit`).

## Open questions

1. **One Auditor per Harbor or one per archipelago?** Probably per-Harbor (each Harbor's Authority is local; cross-Harbor visibility comes via peer-RPC). A meta-Auditor that sees all Harbors could exist later — that's closer to the deferred Harbormaster role.
2. **What happens during Auditor restart / down?** Authority continues enforcing Bucket-1 deterministically; Bucket-2 actions just don't happen until Auditor returns; escalations queue; daily summaries miss. Acceptable degraded state.
3. **Does the Auditor's LLM have memory across decisions?** Probably yes (Claude Code has session continuity). But the *measurements* are queried fresh each time — no caching of stale facts.
4. **Should the Auditor itself be subject to friction?** If the Auditor proposes a large bucket-2 action (e.g., throttle a whole Yard), should that be confirmed or auto? Lean auto for genuinely-defensive actions; the friction is on the principal's overrides.
5. **Cross-Auditor coordination (multi-Harbor).** If Hetzner's Auditor sees a token-leak signal that affects a Mac Dock, how does it coordinate? Probably via the peer-RPC channel to Mac's Auditor; both are notified, both can act on their own jurisdiction.
