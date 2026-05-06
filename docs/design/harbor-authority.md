# Authority surface

**Status:** sketch · **Depends on:** [vocabulary.md](vocabulary.md), [capability-broker.md](capability-broker.md), [resource-ceilings.md](resource-ceilings.md), [in-desk-rpc.md](in-desk-rpc.md)

> Note on naming: per the V3 split (see [vocabulary.md](vocabulary.md)), the **Authority** is the deterministic rule-enforcement role and the **Auditor** is the LLM observation/judgment role. This doc historically used "Harbormaster" as the umbrella term for both; the deferred Harbormaster role is a future cross-coordination layer. Throughout this doc, "Harbormaster" should be read as **Authority** (the enforcement surface) — that's what this doc actually specifies.

This is the doc that unblocks resource-ceilings Phase C and any other "Authority does something to a worker" feature. It names the authority the Authority role has on a Harbor — what RPC methods it can call, how its identity differs from a worker's, what's structurally prevented even with that identity, and how its actions get audited.

---

## 1. The tension

The Authority is supposed to be *powerful enough* to: stop a runaway Dockworker, throttle egress bandwidth, revoke a stale lease, restart a dead agent process inside a healthy container. And *bounded enough* that a compromised Authority doesn't equal full Harbor takeover.

That tension shows up as four sub-tensions, each with a real design decision:

| Sub-tension | Cheap end | Strict end |
|---|---|---|
| **Identity model** | Authority is just another worker with extra capabilities | Authority has a structurally distinct auth grade |
| **RPC surface** | Reuse existing CLI handlers, gated by capability | New first-class RPC methods with their own audit shape |
| **Self-targeting prevention** | Code-guard: handler checks `target != caller_desk_id` | Structural: Authority can't reach the RPCs that would let it self-target |
| **Policy mutation** | Static — Authority reloads policy on file change, principal edits files | Dynamic — Authority can propose policy changes via Telegram, principal acks via reply, change applies live |

The right answer to each isn't obvious in advance. This doc lays out the choices, names the trade-offs, and proposes a default — but the proposal is meant to be redirected.

---

## 2. Identity model — bearer-token grade vs separate-identity

**Cheap (bearer-token grade):** Authority gets a normal `drydock-token` like every other desk. The token's row in the `tokens` table carries an extra `scope: "authority"` field (or analogous capability list). RPC dispatcher checks the scope before routing to Authority-restricted methods.

- *Pro:* Reuses everything that already works. One auth path, one revocation surface, one place to think about. Audit identifies caller by `drydock_id` uniformly.
- *Con:* If the Authority's container is compromised, the attacker has both worker-level access (it's in a container) AND Authority-level access (the token is right there in `/run/secrets/drydock-token`). One breach, full power.
- *Mitigation:* Periodic token rotation; storing the master token outside the container in the host filesystem; `drydock daemon` checks token-presentation source IP / socket peer-uid. Real but bandaid-y.

**Strict (separate-identity):** The Authority authenticates with a separate credential (e.g., a TLS client cert, or a signed ephemeral token issued by `drydock daemon` for each session) presented over a *different* socket (`~/.drydock/run/daemon-authority.sock`, root:0600). Worker tokens cannot reach this socket; Authority credential cannot be used on the worker socket.

- *Pro:* Compromise of the Authority *container* doesn't immediately give worker-style access (different socket); compromise of a worker's token doesn't immediately give Authority access (worker token can't authenticate to authority socket).
- *Con:* Two auth paths to maintain. Two test surfaces. The Authority needs to bootstrap its credential from somewhere — likely a one-time principal action (`drydock authority designate <desk> --grant-credential`).

**Recommended default: bearer-token-grade with a hardening pass.** The worker-vs-Authority attack-path distinction is real but the Authority *is* still a drydock, running in a container, that the principal trusts. Adding a separate auth path is a meaningful increase in moving parts for a marginal compromise-isolation benefit. Take the simpler path; if Authority compromise becomes a real concern, upgrade to separate-identity later — the RPC surface stays the same, only the auth check changes.

The trade-off you're sensing into: **one breach = full power vs. two-auth-paths-forever**. I lean cheap because the Authority's compromise scenario is "an agent on a Hetzner box you control got prompt-injected" — at which point you have bigger problems than the auth-grade distinction. Your mileage may vary.

---

## 3. RPC surface — reuse vs new first-class methods

The Authority wants to do things like stop a drydock, throttle a drydock's egress, restart a drydock's agent, revoke a drydock's lease. Two ways:

**Cheap (reuse + gate):** Treat the existing CLI handlers as the implementation. `drydock stop` already stops a drydock — wrap its core function in a daemon RPC `StopDesk(name)`, gate with the authority-scope check, audit the gated call. Same for `RevokeLease` (wraps `release_capability`) and so on. Restart-agent and throttle would be new because they don't currently exist as CLI commands.

- *Pro:* Smallest new surface. Existing tested code paths. The principal, the operator (you at the CLI), and the Authority all share one implementation.
- *Con:* The audit shape inherits whatever the existing handler emits, which may not capture "Authority did this *because* of policy rule X" — only "this got stopped, and the caller was the Authority." Reasoning gets lost.

**Strict (first-class RPC methods):** New daemon methods (`AuthorityStopDesk`, `AuthorityThrottle`, `AuthorityRevokeLease`, `AuthorityRestartAgent`) that take an additional `reason: { policy_rule, evidence }` field and emit a richer audit shape (`authority.action` events with full reasoning).

- *Pro:* Audit becomes self-explanatory — every Authority action carries the *why*. Reading `drydock audit --event authority.action` tells you the full story without joining against policy + metric history.
- *Con:* New code to maintain. The existing `drydock stop` and the new `AuthorityStopDesk` could drift if not careful.

**Recommended default: hybrid.** New first-class methods, but each one is a thin wrapper around the existing CLI handler's core function. The wrapper's job is *only* to (a) check Authority-scope, (b) capture reasoning, (c) emit the rich audit event, then (d) delegate to the existing function. Cheap implementation, strict audit.

Concrete RPC list for V1:

| Method | Wraps | Adds |
|---|---|---|
| `StopDesk(name, reason)` | `drydock stop` core | reasoning + authority.action audit |
| `RestartDeskAgent(name, agent, reason)` | NEW (NEW for Authority surface) | reads drydock's project-YAML `agents:` block, signals named PIDs |
| `ThrottleEgress(name, bandwidth_max, reason)` | NEW | tc/htb on the drydock's veth |
| `RevokeLease(lease_id, reason)` | `release_capability` | reasoning + audit. (Bypasses caller-desk-id ownership check that release_capability normally enforces — Authority can revoke leases it doesn't hold.) |
| `RegisterWorkload(drydock_id, workload_spec)` | (write side of resource-ceilings.md §3) | issues WorkloadLease |
| `ReadHarborMetrics()` | host_audit + harbor-monitor | unified read for the Authority's decision loop |

Notably absent from V1 — destructive operations the Authority should *escalate*, not auto-execute:

- `DestroyDesk` (data loss; principal-only)
- `RotatePolicy` (the Authority reads policy, doesn't author it)
- `UpgradeDesk` / image bumps (project-author concern)
- Anything touching `daemon-secrets/` (master credentials)

---

## 4. Self-targeting prevention

Three places a self-targeting bug could land if we don't think about it:

- Authority calls `StopDesk(self_desk_name)` and stops itself. Recoverable (systemd restarts the desk) but creates a brief loss of the only thing watching the archipelago.
- Authority calls `RevokeLease(lease_held_by_self)` and revokes its own credentials.
- Authority calls `ThrottleEgress(self_name, 0)` and bricks its own connectivity.

**Cheap (code-guard):** Each Authority RPC handler checks `if target_desk_id == caller_desk_id: raise self_target_rejected`. One-liner per method. Easy to forget on a new method.

**Strict (structural):** Maintain a registry table `authority_desks` listing which desks have the authority scope. RPC dispatcher refuses any authority-scoped RPC where `target ∈ authority_desks`. Single check, all methods covered, can't be forgotten.

**Recommended: structural.** The cost is one query in the dispatcher; the benefit is "you cannot accidentally write a self-targeting RPC." Code-guard works but assumes every future RPC author remembers to add the check. Structural assumes nothing.

There's a corollary: if you ever run *multiple* Authorities on the same Harbor (you probably won't — one per archipelago by design), the structural check naturally generalizes to "no authority-scoped action against any drydock in `authority_desks`." Authorities can't act on each other either. This is a property worth keeping even at one-Authority scale because it removes a future foot-gun.

---

## 5. Audit asymmetry

The Authority reads the audit log to make decisions ("has this drydock had repeated NETWORK_REACH denials in the last hour?"). It does NOT modify the audit log directly. Audit writes happen via `drydock daemon`'s `emit_audit` channel which is the only writer; the Authority's actions emit *new* audit events (via the wrapped RPCs) but cannot edit or delete prior ones.

This is already the existing audit model — calling it out so it doesn't get "improved" later. The audit log is append-only and `drydock daemon`-owned. That property is what makes a compromised Authority *recoverable*: even after the breach, the trail of what it did exists.

A specific thing to NOT add: a `Authority.MarkAuditReviewed` or "snooze" mechanism. If the principal needs to silence noisy audit events, that filter lives in the *display* layer (Telegram alert debouncing, `drydock audit` filtering), never in the audit log itself.

---

## 6. Policy mutation — static vs dynamic

The Authority reads its policy from `~/.drydock/policy/`. Two models for how policy changes propagate:

**Cheap (static):** Files are owned by the principal, read-only to the Authority. Authority watches mtimes (or polls every N seconds) and reloads. Principal edits files via SSH or a notebook drydock. Telegram is one-way escalation only.

**Dynamic (Telegram-driven proposals):** Authority can *propose* policy changes via Telegram ("I keep escalating `huggingface.co` for ml-sandbox; promote to standing entitlement?"). Principal replies with one word; Authority generates the YAML diff, applies it (via a separate `drydock daemon` write surface for policy files), and the change takes effect on next reload.

- *Pro of dynamic:* Closes the operational loop without the principal having to SSH anywhere. Policy refinement becomes a chat thread.
- *Con of dynamic:* Now the Authority has *write* access to the policy files it reads. Compromise impact widens — a compromised Authority could rewrite its own constraints. Even if a principal-confirmation step gates each write, a clever attacker could craft proposals that look reasonable.

**Recommended: static for V1, dynamic explicitly deferred.** The dynamic-policy upgrade is a significant trust-boundary change and shouldn't be bundled with the "give the Authority authority to act" V1. Ship static; live with it for a quarter; revisit dynamic when the volume of "I want to ack this from my phone" interactions justifies the security cost.

The trade-off you're sensing into: **operational ease vs. constraint-rewriting-via-compromise**. I lean static because the Authority's read-side already lets it propose-via-Telegram without needing write-side ("here's a YAML snippet you should add"); the principal pasting it manually is mild friction, not a blocker.

---

## 7. The full picture, summarized

Recommended V1 shape:

- **Identity:** bearer-token grade with a `scope: "authority"` field on the token. Same auth path as workers, distinguished at the dispatcher.
- **RPC surface:** 6 new first-class methods (StopDesk, RestartDeskAgent, ThrottleEgress, RevokeLease, RegisterWorkload, ReadHarborMetrics), each thin-wrapping existing or new core functions, each emitting `authority.action` audit events that capture the *why*.
- **Self-targeting:** structural — registry table of authority-scoped desks, dispatcher rejects any authority-scoped RPC where the target is in that table.
- **Audit:** read-only access via existing `drydock audit` machinery; writes happen only as the side-effect of the new RPC methods; no edit/delete surface.
- **Policy mutation:** static — Authority reads `~/.drydock/policy/`, reloads on mtime change. Dynamic-via-Telegram is V2.

Each of these is the *cheap* end of its respective tension except for **self-targeting** (where I picked structural because the cost is one query and the alternative bets on perfect future authorship). That's a deliberate mix — defaults that get the V1 shipped without too many moving parts, with one strict choice in the place where structural enforcement is genuinely cheaper than discipline.

---

## 8. Resolved decisions and open questions

**Resolved (promoted from earlier open questions):**

- **Throttle persistence.** Re-apply on next observation cycle rather than persist in overlay; keeps throttle state out of project config.
- **`RegisterWorkload` write-side.** Authority delegates to a `drydock daemon` internal method; lease minting stays in one place.
- **`ReadHarborMetrics` shape.** Composes the existing `drydock host audit` + `drydock harbors status` surfaces; no new third one.
- **Bootstrapping.** Principal runs `drydock authority designate <desk>`, a one-shot CLI that writes the `authority_desks` registry row and issues an Authority-scoped token.

**Still open:**

1. **Multi-tenancy of the Authority RPC surface.** If two desks both hold the authority scope (against the design recommendation but possible), they have equal authority; structural self-targeting prevents them from acting on each other; the ordering of conflicting actions is whoever-wins-the-race. Document explicitly as undefined-and-not-designed-around.
