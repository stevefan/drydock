# Harbormaster authority surface

**Status:** sketch · **Depends on:** [principal-deputy-governance.md](principal-deputy-governance.md), [capability-broker.md](capability-broker.md), [resource-ceilings.md](resource-ceilings.md), [in-desk-rpc.md](in-desk-rpc.md)

> Note on naming: this doc uses **Harbormaster** for what `principal-deputy-governance.md` and `employee-worker.md` currently call "the deputy" / "the Harbor agent." If the maritime-vocabulary consolidation lands, this is canonical. Until then, mentally substitute "deputy" wherever you see Harbormaster.

This is the doc that unblocks resource-ceilings Phase C and any other "Harbormaster does something to a worker" feature. It names the authority the Harbormaster has on a Harbor — what RPC methods it can call, how its identity differs from a worker's, what's structurally prevented even with that identity, and how its actions get audited.

---

## 1. The tension

The Harbormaster is supposed to be *powerful enough* to: stop a runaway worker, throttle egress bandwidth, revoke a stale lease, restart a dead agent process inside a healthy container. And *bounded enough* that a compromised Harbormaster doesn't equal full Harbor takeover.

That tension shows up as four sub-tensions, each with a real design decision:

| Sub-tension | Cheap end | Strict end |
|---|---|---|
| **Identity model** | Harbormaster is just another worker with extra capabilities | Harbormaster has a structurally distinct auth grade |
| **RPC surface** | Reuse existing CLI handlers, gated by capability | New first-class RPC methods with their own audit shape |
| **Self-targeting prevention** | Code-guard: handler checks `target != caller_desk_id` | Structural: Harbormaster can't reach the RPCs that would let it self-target |
| **Policy mutation** | Static — Harbormaster reloads policy on file change, principal edits files | Dynamic — Harbormaster can propose policy changes via Telegram, principal acks via reply, change applies live |

The right answer to each isn't obvious in advance. This doc lays out the choices, names the trade-offs, and proposes a default — but the proposal is meant to be redirected.

---

## 2. Identity model — bearer-token grade vs separate-identity

**Cheap (bearer-token grade):** Harbormaster gets a normal `drydock-token` like every other desk. The token's row in the `tokens` table carries an extra `scope: "harbormaster"` field (or analogous capability list). RPC dispatcher checks the scope before routing to Harbormaster-restricted methods.

- *Pro:* Reuses everything that already works. One auth path, one revocation surface, one place to think about. Audit identifies caller by `desk_id` uniformly.
- *Con:* If the Harbormaster's container is compromised, the attacker has both worker-level access (it's in a container) AND Harbormaster-level access (the token is right there in `/run/secrets/drydock-token`). One breach, full power.
- *Mitigation:* Periodic token rotation; storing the master token outside the container in the host filesystem; `wsd` checks token-presentation source IP / socket peer-uid. Real but bandaid-y.

**Strict (separate-identity):** The Harbormaster authenticates with a separate credential (e.g., a TLS client cert, or a signed ephemeral token issued by `wsd` for each session) presented over a *different* socket (`~/.drydock/run/wsd-harbormaster.sock`, root:0600). Worker tokens cannot reach this socket; Harbormaster credential cannot be used on the worker socket.

- *Pro:* Compromise of the Harbormaster *container* doesn't immediately give worker-style access (different socket); compromise of a worker's token doesn't immediately give Harbormaster access (worker token can't authenticate to harbormaster socket).
- *Con:* Two auth paths to maintain. Two test surfaces. The Harbormaster needs to bootstrap its credential from somewhere — likely a one-time principal action (`ws harbormaster designate <desk> --grant-credential`).

**Recommended default: bearer-token-grade with a hardening pass.** The worker-vs-Harbormaster attack-path distinction is real but the Harbormaster *is* still a desk, running in a container, that the principal trusts. Adding a separate auth path is a meaningful increase in moving parts for a marginal compromise-isolation benefit. Take the simpler path; if Harbormaster compromise becomes a real concern, upgrade to separate-identity later — the RPC surface stays the same, only the auth check changes.

The trade-off you're sensing into: **one breach = full power vs. two-auth-paths-forever**. I lean cheap because the Harbormaster's compromise scenario is "an agent on a Hetzner box you control got prompt-injected" — at which point you have bigger problems than the auth-grade distinction. Your mileage may vary.

---

## 3. RPC surface — reuse vs new first-class methods

The Harbormaster wants to do things like stop a desk, throttle a desk's egress, restart a desk's agent, revoke a desk's lease. Two ways:

**Cheap (reuse + gate):** Treat the existing CLI handlers as the implementation. `ws stop` already stops a desk — wrap its core function in a wsd RPC `StopDesk(name)`, gate with the harbormaster-scope check, audit the gated call. Same for `RevokeLease` (wraps `release_capability`) and so on. Restart-agent and throttle would be new because they don't currently exist as CLI commands.

- *Pro:* Smallest new surface. Existing tested code paths. The principal, the operator (you at the CLI), and the Harbormaster all share one implementation.
- *Con:* The audit shape inherits whatever the existing handler emits, which may not capture "Harbormaster did this *because* of policy rule X" — only "this got stopped, and the caller was the Harbormaster." Reasoning gets lost.

**Strict (first-class RPC methods):** New wsd methods (`HarbormasterStopDesk`, `HarbormasterThrottle`, `HarbormasterRevokeLease`, `HarbormasterRestartAgent`) that take an additional `reason: { policy_rule, evidence }` field and emit a richer audit shape (`harbormaster.action` events with full reasoning).

- *Pro:* Audit becomes self-explanatory — every Harbormaster action carries the *why*. Reading `ws audit --event harbormaster.action` tells you the full story without joining against policy + metric history.
- *Con:* New code to maintain. The existing `ws stop` and the new `HarbormasterStopDesk` could drift if not careful.

**Recommended default: hybrid.** New first-class methods, but each one is a thin wrapper around the existing CLI handler's core function. The wrapper's job is *only* to (a) check Harbormaster-scope, (b) capture reasoning, (c) emit the rich audit event, then (d) delegate to the existing function. Cheap implementation, strict audit.

Concrete RPC list for V1:

| Method | Wraps | Adds |
|---|---|---|
| `StopDesk(name, reason)` | `ws stop` core | reasoning + harbormaster.action audit |
| `RestartDeskAgent(name, agent, reason)` | NEW (per principal-deputy §6f) | reads desk's project-YAML `agents:` block, signals named PIDs |
| `ThrottleEgress(name, bandwidth_max, reason)` | NEW | tc/htb on the desk's veth |
| `RevokeLease(lease_id, reason)` | `release_capability` | reasoning + audit. (Bypasses caller-desk-id ownership check that release_capability normally enforces — Harbormaster can revoke leases it doesn't hold.) |
| `RegisterWorkload(desk_id, workload_spec)` | (write side of resource-ceilings.md §3) | issues WorkloadLease |
| `ReadFleetMetrics()` | host_audit + fleet-monitor | unified read for the Harbormaster's decision loop |

Notably absent from V1 — destructive operations the Harbormaster should *escalate*, not auto-execute:

- `DestroyDesk` (data loss; principal-only)
- `RotatePolicy` (the Harbormaster reads policy, doesn't author it)
- `UpgradeDesk` / image bumps (project-author concern)
- Anything touching `daemon-secrets/` (master credentials)

---

## 4. Self-targeting prevention

Three places a self-targeting bug could land if we don't think about it:

- Harbormaster calls `StopDesk(self_desk_name)` and stops itself. Recoverable (systemd restarts the desk) but creates a brief loss of the only thing watching the fleet.
- Harbormaster calls `RevokeLease(lease_held_by_self)` and revokes its own credentials.
- Harbormaster calls `ThrottleEgress(self_name, 0)` and bricks its own connectivity.

**Cheap (code-guard):** Each Harbormaster RPC handler checks `if target_desk_id == caller_desk_id: raise self_target_rejected`. One-liner per method. Easy to forget on a new method.

**Strict (structural):** Maintain a registry table `harbormaster_desks` listing which desks have the harbormaster scope. RPC dispatcher refuses any harbormaster-scoped RPC where `target ∈ harbormaster_desks`. Single check, all methods covered, can't be forgotten.

**Recommended: structural.** The cost is one query in the dispatcher; the benefit is "you cannot accidentally write a self-targeting RPC." Code-guard works but assumes every future RPC author remembers to add the check. Structural assumes nothing.

There's a corollary: if you ever run *multiple* Harbormasters on the same Harbor (you probably won't — one per fleet by design), the structural check naturally generalizes to "no harbormaster-scoped action against any desk in `harbormaster_desks`." Harbormasters can't act on each other either. This is a property worth keeping even at one-Harbormaster scale because it removes a future foot-gun.

---

## 5. Audit asymmetry

The Harbormaster reads the audit log to make decisions ("has this desk had repeated NETWORK_REACH denials in the last hour?"). It does NOT modify the audit log directly. Audit writes happen via `wsd`'s `emit_audit` channel which is the only writer; the Harbormaster's actions emit *new* audit events (via the wrapped RPCs) but cannot edit or delete prior ones.

This is already the existing audit model — calling it out so it doesn't get "improved" later. The audit log is append-only and `wsd`-owned. That property is what makes a compromised Harbormaster *recoverable*: even after the breach, the trail of what it did exists.

A specific thing to NOT add: a `Harbormaster.MarkAuditReviewed` or "snooze" mechanism. If the principal needs to silence noisy audit events, that filter lives in the *display* layer (Telegram alert debouncing, `ws audit` filtering), never in the audit log itself.

---

## 6. Policy mutation — static vs dynamic

The Harbormaster reads its policy from `~/.drydock/policy/` (per principal-deputy-governance §4). Two models for how policy changes propagate:

**Cheap (static):** Files are owned by the principal, read-only to the Harbormaster. Harbormaster watches mtimes (or polls every N seconds) and reloads. Principal edits files via SSH or a notebook desk. Telegram is one-way escalation only.

**Dynamic (Telegram-driven proposals):** Harbormaster can *propose* policy changes via Telegram ("I keep escalating `huggingface.co` for ml-sandbox; promote to standing entitlement?"). Principal replies with one word; Harbormaster generates the YAML diff, applies it (via a separate `wsd` write surface for policy files), and the change takes effect on next reload.

- *Pro of dynamic:* Closes the operational loop without the principal having to SSH anywhere. Policy refinement becomes a chat thread.
- *Con of dynamic:* Now the Harbormaster has *write* access to the policy files it reads. Compromise impact widens — a compromised Harbormaster could rewrite its own constraints. Even if a principal-confirmation step gates each write, a clever attacker could craft proposals that look reasonable.

**Recommended: static for V1, dynamic explicitly deferred.** The dynamic-policy upgrade is a significant trust-boundary change and shouldn't be bundled with the "give the Harbormaster authority to act" V1. Ship static; live with it for a quarter; revisit dynamic when the volume of "I want to ack this from my phone" interactions justifies the security cost.

The trade-off you're sensing into: **operational ease vs. constraint-rewriting-via-compromise**. I lean static because the Harbormaster's read-side already lets it propose-via-Telegram without needing write-side ("here's a YAML snippet you should add"); the principal pasting it manually is mild friction, not a blocker.

---

## 7. The full picture, summarized

Recommended V1 shape:

- **Identity:** bearer-token grade with a `scope: "harbormaster"` field on the token. Same auth path as workers, distinguished at the dispatcher.
- **RPC surface:** 6 new first-class methods (StopDesk, RestartDeskAgent, ThrottleEgress, RevokeLease, RegisterWorkload, ReadFleetMetrics), each thin-wrapping existing or new core functions, each emitting `harbormaster.action` audit events that capture the *why*.
- **Self-targeting:** structural — registry table of harbormaster-scoped desks, dispatcher rejects any harbormaster RPC where the target is in that table.
- **Audit:** read-only access via existing `ws audit` machinery; writes happen only as the side-effect of the new RPC methods; no edit/delete surface.
- **Policy mutation:** static — Harbormaster reads `~/.drydock/policy/`, reloads on mtime change. Dynamic-via-Telegram is V2.

Each of these is the *cheap* end of its respective tension except for **self-targeting** (where I picked structural because the cost is one query and the alternative bets on perfect future authorship). That's a deliberate mix — defaults that get the V1 shipped without too many moving parts, with one strict choice in the place where structural enforcement is genuinely cheaper than discipline.

---

## 8. Open questions

1. **Throttle persistence across desk restarts.** If the Harbormaster sets a tc/htb cap on a desk's veth and the desk's container restarts, the veth disappears and the throttle goes with it. Either the Harbormaster re-applies on the next observation cycle, or we persist the throttle as part of the desk's overlay so it re-installs at create. The latter is cleaner but mixes throttle state with project config. Lean re-apply.
2. **`RegisterWorkload` write-side ownership.** The Harbormaster issues WorkloadLeases. Does that mean the Harbormaster's RPC scope includes "create lease" (a thing today only `wsd`'s capability handlers do)? Or does the Harbormaster delegate by calling an internal `wsd` method that issues the lease on its behalf? Probably the latter — keeps lease minting in one place.
3. **Multi-tenancy of the Harbormaster RPC surface.** If two desks both hold the harbormaster scope (against the design recommendation but possible), do they have equal authority? Probably yes; structural self-targeting prevents them from acting on each other; the ordering of conflicting actions is whoever-wins-the-race. Worth documenting that this is undefined and not designed-around.
4. **What does `ReadFleetMetrics` actually return?** Right now `ws host audit` is one shape; `ws fleet status` is another. The Harbormaster wants a unified, structured-for-decision-making view. Either it composes the two existing surfaces, or there's a new third one. Lean compose.
5. **Bootstrapping the first Harbormaster.** Chicken-and-egg: the Harbormaster grants and revokes things, but who grants the Harbormaster scope to the first Harbormaster desk? The principal, manually, via `ws harbormaster designate <desk>` — a one-shot CLI command that writes to the `harbormaster_desks` registry table and issues a Harbormaster-scoped token. No Harbormaster involved in granting itself authority.
