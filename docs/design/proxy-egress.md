# Proxy egress — replacing iptables/ipset domain enforcement

**Status:** sketch · prompted by the 2026-05-06 hetzner deploy where the iptables/ipset firewall hit its structural limits · **Depends on:** [amendment-contract.md](amendment-contract.md), [network-reach.md](network-reach.md), [capability-broker.md](capability-broker.md), [narrowness.md](narrowness.md)

## Problem

The current default-deny iptables + `allowed-domains` ipset works as a *blast-radius cap* but fails as a *policy primitive*. Three structural issues, all hit live during the litestream provisioning + rename deploy:

1. **It's IP-based, but the policy author thinks in domains.** Every DNS-→ipset round-trip is a stale snapshot. S3 (and any CDN-fronted service) returns different IPs per query. The 15-minute `refresh-firewall-allowlist.sh` loop is asymptotically chasing a moving target. Connections randomly fail with "no route to host" because the just-resolved IP isn't in the set yet.
2. **It's not dynamic.** `FIREWALL_EXTRA_DOMAINS` is an env var read at container start. YAML edits don't propagate to a running container. The amendment-contract pattern (Dockworker proposes a new domain → Authority decides → policy updates) collapses to "stop, edit YAML, recreate container," which is the antipattern V3 exists to avoid.
3. **CIDR-range whitelisting (`FIREWALL_AWS_IP_RANGES`) helps but isn't enough.** Virtual-host bucket names sometimes resolve to subnets outside the published ranges. The published ranges churn weekly. AWS publishes 16 us-west-2 S3 prefixes today; that's manageable for one service but doesn't scale across services or regions.

Tonight's session-long firewall debugging — five separate "add this domain → reload → restart container → retry" cycles per desk — is exactly the loop the V3 amendment contract is supposed to *eliminate*, not iterate.

## Why a proxy is the right shape

Replace per-domain IP-set tracking with a forward proxy that enforces domain-based policy at request time. The proxy resolves DNS *each request*, so IP rotation is a non-issue. The proxy's allowlist is a file the daemon owns, reloadable on SIGHUP — sub-minute end-to-end.

**Default-deny iptables stays.** Even if the proxy is compromised, the container can't escape to arbitrary IPs. That's the security floor. The proxy adds the policy layer on top — defense in depth, not redundancy.

The shape:

```
┌─────────────────────────────────┐
│  Drydock container              │
│                                 │
│   worker process                │
│       │                         │
│       │ HTTP_PROXY/HTTPS_PROXY  │
│       ▼                         │
│   smokescreen (sidecar)  ───────┼──► outside (only this path egresses)
│       │                         │
│       │ ←── allowlist file       │
│       │     (SIGHUP on reload)   │
│       ▼                         │
│   iptables: default-DROP except │
│   to smokescreen's listen port  │
└─────────────────────────────────┘
```

**Why SNI-based, not full TLS interception:** the TLS ClientHello includes the target hostname in plaintext (SNI extension). The proxy reads it, decides allow/deny, then either tunnels the connection (CONNECT-style) or refuses. **No CA injection, no MITM, no end-to-end encryption broken.** URL paths, headers, request bodies stay encrypted between the worker and the destination. We get domain-level policy without becoming a man-in-the-middle.

The trade: SNI-only inspection can't filter by URL path or HTTP method. "Allow github.com" is the granularity, not "allow github.com but only `/api/v3/`." For drydock's threat model — preventing exfiltration to unexpected domains — this is the right level.

## Concrete tool: smokescreen

[Stripe's `smokescreen`](https://github.com/stripe/smokescreen) is purpose-built for this exact use case: containers reach allowlisted external domains via an SNI-aware HTTP CONNECT proxy. Production-tested at scale. Single Go binary, easy to bake into `drydock-base`.

Properties that matter:

- **YAML-driven allowlist**, reload on SIGHUP. Maps trivially onto drydock's `delegatable_network_reach` field.
- **Built-in SSRF protection**: blocks RFC1918 (private) addresses, link-local, AWS metadata service (169.254.169.254), GCP/Azure equivalents — exactly the things a confused worker might accidentally hit.
- **Per-request audit log**: which client, which destination, allowed or denied, how long. Feeds straight into the audit pipeline.
- **Glob support in allowlist**: `*.github.com` is one entry, not 12.
- **Health endpoint** for the daemon to probe — Auditor watch-loop has a clear "is the egress layer alive" signal.

Drop-in alternative if smokescreen ever stops fitting: Squid in `https_port` mode + `ssl_bump=peek` (heavier, more configurable, classic). Custom Python proxy is tempting but TLS handshake parsing is fiddly and the security surface widens for no real win.

## YAML contract

The existing `delegatable_network_reach` glob list becomes the proxy's allowlist verbatim:

```yaml
narrowness:
  network_reach:
    - "*.github.com"
    - "api.anthropic.com"
    - "*.s3.us-west-2.amazonaws.com"   # works! SNI matches the cert SAN
    - "huggingface.co"
  network_reach_ports: [443, 22]      # default 443; explicit if other
```

The daemon writes this to `/etc/smokescreen/<drydock>.yaml` inside the container at create + on every `project reload`, then sends SIGHUP to smokescreen.

`firewall_extra_domains`, `firewall_aws_ip_ranges`, `firewall_ipv6_hosts` become obsolete — superseded by the single `network_reach` list. Migrate existing project YAMLs by folding their contents into `network_reach`. Keep `firewall_extra_domains` as a deprecated alias for one release.

## Daemon surface

Two new pieces:

1. **`UpdateProxyAllowlist(drydock_id, allowlist)`** RPC. Internal-only, called by `project reload` after re-pinning. Writes the proxy config + signals reload.
2. **NETWORK_REACH capability becomes proxy-allowlist mutation.** Today it adds a domain to the in-container ipset via `add-allowed-domain.sh`. After this lands: `RequestCapability(type=NETWORK_REACH, scope.domain=...)` validates against `delegatable_network_reach`, appends to the proxy allowlist (with optional TTL = lease expiry), SIGHUPs. Lease release removes the entry. **Same RPC contract — only the enforcement layer changes.**

This is the amendment-contract loop made operational: Dockworker hits a 403 → recognizes the failure → submits an amendment → Authority auto-applies (if matches glob) or Auditor escalates → allowlist updated → next request succeeds. Sub-minute response loop, no container restarts.

## Failure modes

- **Proxy down** → all egress dies. Mitigation: smokescreen runs as a supervised process (systemd inside container, or pid-1 watcher); Auditor watch-loop probes the proxy's `/health` endpoint and escalates if it's been down >2 minutes.
- **Proxy compromised** → attacker can rewrite the allowlist? No — the allowlist file is owned by root, smokescreen runs as a non-root user, and the daemon is the only writer. Compromise of smokescreen process gets attacker connections out via *currently-allowed* domains, but no widening.
- **Misconfigured glob lets too much through** → standard policy-author risk. Same risk model as today's `firewall_extra_domains`. Auditor can flag suspiciously broad globs.
- **DNS poisoning/hijack** → proxy resolves DNS server-side; if the resolver is compromised, allowlist is meaningless. Mitigation: DoH/DoT to a trusted resolver, plus optional pinning. Phase 2 concern.

## Migration path

**Phase E0 — drydock-base ships smokescreen.** Add smokescreen binary to the base image build. Init script writes a no-op default config. Existing desks: nothing changes (env vars not set, smokescreen not started).

**Phase E1 — opt-in per drydock.** Project YAML field `egress_proxy: enabled`. When set:
- Init-firewall installs an iptables rule allowing only `127.0.0.1:4750` (smokescreen's listen port) for OUTPUT, drops everything else.
- Smokescreen launches with the desk's `network_reach` as allowlist.
- Container env sets `HTTP_PROXY=http://127.0.0.1:4750` and `HTTPS_PROXY=http://127.0.0.1:4750`.
- `firewall_extra_domains` continues to work (legacy code path, ipset additions still applied) — there's no flag day; existing desks keep working unchanged.

**Phase E2 — make it the default.** New desks created after a flag flip use proxy. Old desks are migrated via `drydock project reload` + recreate at convenient times. Eventually retire `firewall_extra_domains`, `firewall_aws_ip_ranges`, `firewall_ipv6_hosts`, `add-allowed-domain.sh`, `refresh-firewall-allowlist.sh` — this is a substantial code+doc cleanup, ~300 lines disappear.

**Phase E3 — wire the amendment-contract.** `RequestCapability(type=NETWORK_REACH)` goes through proxy mutation instead of ipset. The Authority+Auditor split (per [vocabulary.md](vocabulary.md)) decides amendments. This is where the productivity win actually shows up — the agent stops needing the principal in the loop for routine domain additions.

E0 + E1 are independently shippable; E2 + E3 build on them.

## Resolved decisions and open questions

**Resolved:**
- **SNI inspection, not TLS MITM.** No CA injection, no end-to-end-encryption break. Domain-level policy is the right granularity for drydock's threat model.
- **Default-deny iptables stays.** Floor-of-last-resort even if proxy is compromised. Defense in depth.
- **Smokescreen as the V1 implementation.** Production-tested, single binary, fits the contract.
- **Per-container sidecar, not Harbor-level shared proxy.** Per-drydock isolation: an exploit in one drydock can't see other drydocks' allowlists or rewrite shared config. Cost is one extra process per drydock (~10 MB RSS), trivially worth it.

**Still open:**
1. **Where does smokescreen run inside the container?** As pid 0 wrapper that exec's the worker? Sidecar process supervised by systemd-in-container? Backgrounded by `start-smokescreen.sh` analogous to `start-tailscale.sh` today? Lean third option for V1 — matches existing pattern.
2. **TTL on NETWORK_REACH proxy entries.** Today's NETWORK_REACH is "additive forever (until container restart)." With proxy reload, lease expiry can actually mean "domain disappears from allowlist." Worth it? Probably yes — gives the Auditor a meaningful "stale grant" signal. But surfaces edge cases (long-running connection through an expired lease).
3. **Logging cardinality.** Smokescreen logs every request. At 100 req/s × 5 desks × 90 days that's a lot. Likely solution: ring-buffer recent decisions, ship denials + summarized allows to audit. Tunable.
4. **DNS strategy.** Smokescreen does its own DNS by default. Pinning + DoH is a hardening play, not V1.
5. **Compatibility with non-HTTP egress.** smokescreen handles HTTP CONNECT (which covers HTTPS) and plain HTTP. SSH, raw TCP to specific ports — won't go through proxy. Today the firewall handles tcp:22 to GitHub via a separate iptables rule. That stays as a special case, no regression.
