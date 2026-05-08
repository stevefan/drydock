# Plan: smokescreen proxy as the only egress path + Auditor maintains it

**Status:** plan · 2026-05-08 · **Pulls together:** [make-the-harness-live.md §2a.1](make-the-harness-live.md#2a1-proxy-egress), [port-auditor.md](port-auditor.md), [memory: auditor isolation principles 2026-05-07]

This is the plan for the Auditor end-state Steven authorized on 2026-05-08: the Port Auditor, with `auditor` token scope, can mutate per-desk egress policy via the smokescreen proxy. By the end of this plan:

- **Smokescreen is the only egress path.** `iptables/ipset` allowlist machinery is deleted from drydock-base. ~300 lines of code go away.
- **Allowlist mutation is sub-minute.** Daemon writes a desk's smokescreen YAML and SIGHUPs the container's smokescreen. No restart.
- **Auditor has structural authority** to call the mutation RPC. Bucket-2 (defensive + reversible). Today's stub `throttle_egress` action becomes real.

## Current state (audit, 2026-05-08)

| Phase | Status | What's done |
|---|---|---|
| **E0** | ✅ | `base/Dockerfile` ships smokescreen v0.0.5 (multi-arch); `start-egress-proxy.sh` no-ops unless `EGRESS_PROXY_ENABLED=1` |
| **E1 partial** | ⚠️ | `core/proxy.py`/`write_smokescreen_acl` exists; daemon writes per-desk allowlist file at create-time when `egress_proxy: enabled`; container bind-mounts it. **Not validated end-to-end.** No live mutation; no `UpdateProxyAllowlist` RPC. |
| **E2** | ❌ | Still opt-in per project YAML. None of the 6 desks on hetzner use it. |
| **E3** | ❌ | NETWORK_REACH lease grants don't affect proxy. No SIGHUP wiring. |
| **PA4 (Auditor authority)** | ❌ | `throttle_egress` action kind exists in `AuditorAction` RPC; raises `-32021 auditor_action_unsupported`. |

## Plan

Five phases, sequential. Each phase is independently shippable + reversible. Total effort ~3-4 working days.

### Phase 1 — Validate E1 (gate, ~half day)

**Goal:** prove the existing E1 wiring actually works end-to-end on a real container before building on top of it.

**Tasks:**
1. Create a test desk with `egress_proxy: enabled` in YAML; minimal `delegatable_network_reach: ["api.github.com"]`.
2. Verify daemon writes `~/.drydock/proxy/<dock_id>.yaml`.
3. Verify container's `start-egress-proxy.sh` launches smokescreen on `:4750`.
4. Verify HTTP_PROXY/HTTPS_PROXY env points at the proxy inside container.
5. **From inside the container:** `curl https://api.github.com` succeeds; `curl https://example.com` fails with proxy refusal (not iptables drop).
6. Capture smokescreen logs to `/var/log/drydock/egress-proxy.log` for the Auditor's read-only view.

**Acceptance:** test desk's egress is gated by smokescreen, not by ipset. Smokescreen log shows allow/deny decisions. Existing iptables rules preserved as defense-in-depth (loopback proxy is the only OUTPUT ACCEPT).

**Risk:** smokescreen might not handle some edge case (raw IP destinations, non-HTTP TCP, etc.). Surface during validation, fix in this phase or fork to phase 1b.

### Phase 2 — E1 completion: live mutation RPC (~1 day)

**Goal:** `UpdateProxyAllowlist(drydock_id)` RPC writes the YAML file + signals smokescreen. New domains reachable within ~5 seconds of mutation, no restart.

**Tasks:**
1. New RPC method `UpdateProxyAllowlist` in `daemon/handlers.py`. Auth: `requires_auth=True`. Caller scopes:
   - `dock` scope: can update *its own* desk's allowlist (within `delegatable_network_reach` bounds — narrowness validator gates this)
   - `auditor` scope: can update *any* desk's allowlist (Bucket-2 mutation; full audit)
2. Daemon-side: regenerate the YAML from the desk's current `delegatable_network_reach` + any active NETWORK_REACH leases.
3. Daemon-side: signal smokescreen to reload. Two patterns:
   - **Preferred**: `docker kill --signal=HUP <container>` from the Harbor — daemon already has docker access
   - **Alternative**: in-desk RPC fires `pkill -HUP smokescreen` inside the container
4. Audit event `egress.allowlist_updated` with caller, target, before/after diff.
5. CLI: `drydock project reload <name>` calls `UpdateProxyAllowlist` after re-pinning YAML.

**Acceptance:** edit YAML → `drydock project reload` → new domain reachable in <10s without container restart. Same for lease addition.

### Phase 3 — E2: smokescreen as the default (~half day)

**Goal:** new desks default to proxy. Existing desks migrate at their own pace via `drydock project reload`.

**Tasks:**
1. Flip `ProjectConfig.egress_proxy` default from `"disabled"` to `"enabled"`. New `drydock create` desks pick proxy automatically.
2. Update `init-firewall.sh` to detect proxy mode and **only** allow `127.0.0.1:4750` in OUTPUT chain (no ipset population).
3. Migration doc: how to flip an existing desk (`drydock project reload` on YAML with `egress_proxy: enabled`).
4. Update `drydock new` worker template to include `egress_proxy: enabled` in the scaffolded YAML.
5. Update CLAUDE.md to document the change of defaults.

**Acceptance:** fresh `drydock create` produces a desk whose only egress is via smokescreen. Existing desks still work on iptables path.

### Phase 4 — E3 + cleanup: lease-driven mutation (~1 day)

**Goal:** sub-minute "Dockworker proposes a domain → daemon adjudicates → next request succeeds" loop. Delete the iptables fallback.

**Tasks:**
1. `RequestCapability(NETWORK_REACH, scope.domain=...)` validates against `delegatable_network_reach`, calls `UpdateProxyAllowlist` with the new domain + lease TTL.
2. Lease release / expiry sweeper: removes the entry, calls `UpdateProxyAllowlist` again.
3. Migrate any desks still on iptables to proxy (`drydock project reload` each). Communicate to Steven; he flips them at convenient times.
4. **The deletes:**
   - `base/init-firewall.sh` — keep only the proxy-mode branch; delete the ipset-rotation logic
   - `base/refresh-firewall-allowlist.sh` — delete entirely
   - `base/add-allowed-domain.sh` — delete entirely
   - `firewall_aws_ip_ranges` field handling — delete (smokescreen does DNS-time allow checks; AWS regions handled via domain globs)
   - `delegatable_firewall_domains` semantics — collapse into `delegatable_network_reach`
5. ~300 lines deleted. Single source of truth for egress policy.

**Acceptance:** all desks on smokescreen. iptables is loopback-proxy-only across the fleet. `git grep ipset` returns zero hits in `base/`.

### Phase 5 — PA4: Auditor maintains the proxy (~half day)

**Goal:** `AuditorAction(kind="throttle_egress", target_drydock_id=X, action="freeze")` actually fires. Reversible.

**Tasks:**
1. New `auditor_handlers.py` dispatch for `throttle_egress`:
   - `action: "freeze"` — write empty allowlist (deny-all). Drydock can still reach daemon socket (Unix-domain, not HTTP).
   - `action: "unfreeze"` — restore from registry's policy (revert to what `delegatable_network_reach` says).
2. Auditor scope check (already in place): refuse if caller's token isn't `auditor`-scoped.
3. Auditor's deep-analysis prompt updated to know `throttle_egress` is now live (not stub). Guidance: only freeze when there's strong evidence (e.g. exfiltration pattern); unfreeze is always allowed.
4. Audit events: `auditor.egress_throttled` / `auditor.egress_unthrottled` (live mode), already-existing `auditor.action_dry_run` for dry-run.
5. **Default remains dry-run.** `AUDITOR_LIVE_ACTIONS=1` in daemon env to enable. Steven flips after observation period.

**Acceptance:** principal can call `drydock-rpc AuditorAction '{"kind":"throttle_egress","target_drydock_id":"X","action":"freeze","reason":"..."}'` from inside the auditor desk. Target desk loses egress within 10s. Unfreeze restores it.

## Cross-cutting concerns

**Reversibility**: every Auditor mutation is undoable by another `AuditorAction` call OR by a principal re-running `drydock project reload`. The registry remains the source of truth for "what is this desk *supposed* to be allowed to reach"; the proxy's live allowlist is the *current state* and may drift downward (Auditor freeze) or upward (active leases).

**Audit trail**: every YAML file write generates an audit event. The Auditor can read these via the audit-log bind-mount and detect "did the principal undo my freeze?" — distinguishing a thoughtful override from an in-progress incident.

**Failure modes**:
- Smokescreen down → no egress → Auditor (which depends on Anthropic API for itself) goes silent → deadman fires. *Acceptable*: a broken proxy is a structural alert, not a mystery.
- Daemon down during proxy mutation → file written but signal not delivered → smokescreen runs stale config until container restart. *Mitigation*: daemon writes file atomically (temp + rename); `start-egress-proxy.sh` re-reads on its own SIGHUP whenever the container restarts.
- Auditor mistakenly freezes a critical desk → principal sees the audit event in their daily summary; can manually `drydock-rpc UpdateProxyAllowlist` to override. Bucket-2's "reversible" property holds.

**What's NOT in this plan**:
- DNS resolver hardening (DoH/DoT) — phase 2 concern per make-the-harness-live.md
- TLS MITM for content inspection — explicitly out of scope; smokescreen is SNI-aware, not MITM
- Per-domain rate limits — possible future work; the proxy emits enough metric data for the Auditor to recommend rate-limit policy without the proxy enforcing it

## What lands first if we execute today

Phase 1 (E1 validation) is the gate. ~4 hours. Verifies the existing wiring works on a real desk before we commit to building on top. If it works, Phase 2 can start the same day.

If Phase 1 surfaces unexpected smokescreen behavior, fork to Phase 1b for the fix before continuing. Don't skip the validation just because the code "looks right."

## Estimated total: 3-4 working days

| Phase | Effort | Cumulative |
|---|---|---|
| 1 — Validate E1 | 0.5 day | 0.5 |
| 2 — Live mutation RPC | 1.0 day | 1.5 |
| 3 — E2 default-on | 0.5 day | 2.0 |
| 4 — E3 + iptables delete | 1.0 day | 3.0 |
| 5 — PA4 Auditor authority | 0.5 day | 3.5 |

Each phase ships independently. After Phase 5, the Auditor structurally maintains the proxy — Steven's authorized end-state.
