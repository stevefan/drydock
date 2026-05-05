# Network reach (dynamic firewall opens)

**Status:** sketch · **Depends on:** capability-broker, in-desk-rpc

## Problem

drydock-base runs default-deny iptables/ipset firewall inside each container. The effective allowlist is computed at container startup from `BASE_DOMAINS + FIREWALL_EXTRA_DOMAINS` and frozen for the container's lifetime. `refresh-firewall-allowlist.sh` re-resolves the *same* domains periodically (CDN IP rotation) but cannot **add new domains**.

Today, when an in-desk agent needs a domain that isn't in the allowlist:

1. Connection is REJECTed.
2. Human edits `FIREWALL_EXTRA_DOMAINS` (project YAML or `.env.devcontainer`).
3. `ws stop && ws create` — full container recreate.
4. Agent retries.

This is friction at exactly the wrong moment (an agent has just discovered it needs a new dependency) and dynamics-killing for any worker pattern that does open-ended research.

## Goal

A drydock can request "open egress to `<domain>:<port>`" via `wsd` RPC. If the desk is entitled, the daemon adds the domain to the live allowlist without restart. If not, the request is denied (and audited).

## Design

### Capability type

Reuse the reserved `NETWORK_REACH` capability slot in the existing broker (`src/drydock/core/capability.py` + `wsd/capability_handlers.py`).

Request shape:
```
RequestCapability {
  type: NETWORK_REACH
  scope: {
    domain: "github.com"           # required; lowercased, validated
    port:   443                    # optional; default 443
    ttl_seconds: 3600              # optional hint; see TTL below
  }
}
```

Subject desk is derived from the bearer token (caller_desk_id) per existing broker convention.

### Entitlement model (the policy decision)

Per-desk column `delegatable_network_reach` in the registry, populated from project YAML:

```yaml
narrowness:
  network_reach:
    - "*.github.com"
    - "registry.npmjs.org"
    - "*.crates.io"
    - "huggingface.co"
```

Match semantics:
- Exact-match domains accepted as-is.
- Glob `*.foo.com` matches any single-level subdomain (`api.foo.com`, not `a.b.foo.com`).
- Wildcard `*` (alone) means **unconstrained** — only for desks explicitly trusted (research desks, `ws-shell`-style desks). Audit logs the unconstrained grant on every call.
- No entry / empty list = no dynamic opens permitted (current behavior).

Port allowlist: optional `network_reach_ports` list per desk (default `[80, 443]`). Anything else requires explicit per-port entry.

**Defaults to commit to:**
- New desks default to **empty** `network_reach` (no dynamic opens). Surfaces the question explicitly per project.
- Recommend `*.github.com`, `registry.npmjs.org`, `*.crates.io`, `huggingface.co`, `pypi.org`, `files.pythonhosted.org` as starter entries for code-writing desks.
- Wildcard `*` is opt-in only, never default.

### Materialization

`wsd` handler invokes the container-side helper synchronously:

```
docker exec -u root <container_id> /usr/local/bin/add-allowed-domain.sh <domain> <port>
```

The helper:
1. Validates domain shape (no spaces, no `;`, no `..`).
2. Appends to `/tmp/firewall-domains.txt` if absent (so the periodic refresher picks it up too).
3. Resolves A records, adds each IP to ipset `allowed-domains` with `ipset add -exist`.
4. If port ≠ 443, adds an iptables rule allowing OUTPUT tcp/<port> for the resolved set (today's rule chain only opens 443/80).
5. Exits 0 on success, non-zero on resolution failure.

Daemon returns the resolved IPs in the lease so the caller can sanity-check.

### TTL & revocation

V1 ships **additive-only**, no TTL enforcement. Matches `refresh-firewall-allowlist.sh`'s philosophy (stale CDN IPs in the set are harmless because the CDN no longer routes to them). Container restart wipes the additions; project YAML carries the durable allowlist.

A consequence to know about: calling `ReleaseCapability` on a NETWORK_REACH lease today is **bookkeeping-only**. The lease row is marked revoked and an audit event fires, but the ipset entry stays put. This matches the additive-only model — the worker has no way to "close" a domain it opened — but it's worth being explicit about because SECRET and STORAGE_MOUNT releases *do* clean up materialization.

TTL + per-IP revocation lands as part of [resource-ceilings.md](resource-ceilings.md) Phase C, when `lease_hold_max` and `idle_lease_revoke_after` become general broker primitives. At that point a NETWORK_REACH lease's IPs become reaper-tracked alongside other capability types — no per-feature reaper.

### Audit

Every grant + every denial is audited via the existing `emit_audit` channel:
```
{event: "capability.network_reach", desk_id, domain, port, decision: "granted"|"denied", reason}
```
Hot allowlist becomes a forensic asset: "what domains did `auction-crawl` open last week?"

### CLI surface

| Command | Purpose |
|---|---|
| `ws reach list <desk>` | Show entitled `network_reach` patterns + currently-opened (live) entries. |
| `ws reach add <desk> <domain> [--port N]` | Manually open from the Harbor (debug / one-off). Same path as the RPC. |
| `ws reach grants <desk>` | Audit query: recent capability.network_reach events. |

Worker-side: `drydock-rpc RequestCapability type=NETWORK_REACH scope.domain=foo.com scope.port=443` (already works once the handler dispatches NETWORK_REACH).

### Failure modes

- **Resolution fails:** lease denied with `dns_resolution_failed`; agent can retry or fall back.
- **Domain matches no entitlement pattern:** denied with `not_entitled`; surfaces a config issue (the human should add to project YAML if legitimate).
- **Container not running:** denied with `desk_not_running`.
- **`add-allowed-domain.sh` exits non-zero:** lease denied; audit captures stderr tail.

## Open questions

1. Should the wildcard `*` entitlement still constrain *ports* (so even a wildcard desk can't open arbitrary :22)? Lean yes — wildcard domains, default port allowlist.
2. Project YAML schema: nest under `narrowness:` (matches existing capability vocabulary) or add top-level `firewall:`? Lean `narrowness:` for consistency.
3. IPv6: current firewall already has `FIREWALL_IPV6_HOSTS` env. Helper should resolve AAAA and add to a v6 ipset symmetrically. Defer if no immediate use case.
4. Egress audit at packet level (vs. capability grants): out of scope for V1 — that's the exit-node design's job.
