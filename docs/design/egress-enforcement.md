# Egress enforcement — belt + suspenders

**Status:** accepted 2026-04-15. Belt (in-container iptables) is current. Suspenders (egress proxy baked into `drydock-base`) is a committed direction for `drydock-base:v2`. Not yet implemented.

This doc captures the egress-control architecture separately because its failure modes are different from the capability broker's and it ships on its own cadence. See [capability-broker.md](capability-broker.md) and [narrowness.md](narrowness.md) for the firewall-domain narrowness rule at lease time.

## The problem

Drydocks need to restrict outbound network access to a per-project allowlist (the `firewall_extra_domains` + base allowlist plumbing that already exists). Today's implementation is `init-firewall.sh` inside each drydock: iptables with a default-DROP OUTPUT policy and an ipset-based allowlist, baked into the drydock's devcontainer.

Two things motivated capturing a decision here:

1. **The 2026-04-15 blanket-443 bug** — a blanket `iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT` rule (originally added because AWS VPN uses 443) was silently defeating the ipset allowlist for ALL HTTPS destinations. Diagnostic logs showed 227+ packets hitting the blanket rule; the DROP terminator saw zero. Fix (restricting 443 to AWS VPN CIDRs only) was applied to the affected asi project. But the class of bug — "one overly-permissive rule quietly negates the rest" — is inherent to iptables-only enforcement and hard to catch without careful review.

2. **A conntrack hypothesis during diagnosis was wrong.** Initial guess: Docker-for-Mac's networking layer breaks conntrack state tracking inside containers. Direct testing inside a running asi drydock refuted this: `nf_conntrack_count` increments correctly, INPUT ESTABLISHED rule has real hit counters. Conntrack works on Mac. What DOES have known issues on Docker-for-Mac: Intel-emulated containers on Apple Silicon (iptables can fail entirely), VPNKit userland proxy interactions, and some OUTPUT rule patterns that can destabilize the LinuxKit VM. These are real but are not the primary driver for belt+suspenders.

The primary driver: **layered enforcement is stronger than any single layer, especially when policy mistakes in one layer (like blanket-443) are easy to introduce and hard to detect from inside that layer.**

## Decision: belt + suspenders

Two enforcement layers, both active simultaneously:

**Belt (kernel-layer, IP-based):** `init-firewall.sh` with iptables + ipset, as today. Enforces at the packet-filter layer. Strict: blocks raw TCP, UDP, any protocol regardless of application behavior. Limitations: can't distinguish tenants at shared IPs (CDN fronting), no visibility into HTTP-layer intent, policy bugs (blanket rules) hard to audit.

**Suspenders (HTTP-layer, SNI-based):** a small egress proxy baked into `drydock-base`, listening on a well-known local port. Containers use `HTTP_PROXY` / `HTTPS_PROXY` env vars pointing at the proxy; non-proxy-aware tools still hit the iptables layer. Proxy enforces an allowlist by TLS SNI inspection (no MITM in v1 — no CA cert gymnastics). Logs every accept/reject to a structured file that feeds into drydock's audit story.

Both layers reference the SAME allowlist source (project YAML's `firewall_extra_domains` + base allowlist). Drydock's overlay generates proxy config alongside the existing iptables env vars.

## Rejected alternatives

**A. iptables-only (current).** Rejected because the blanket-443 class of bug is structural.

**B. egress-proxy-only.** Rejected because raw-TCP or TLS-without-SNI tools would bypass it entirely. Belt stays for coverage that the proxy can't enforce.

**C. Tailscale exit node + internal docker network.** Interesting; considered. Rejected for now because it adds latency on ALL egress (not just policy-enforced destinations), couples every drydock to Tailscale ACL management, and pushes the enforcement point off the Harbor (harder local reasoning). Revisit if drydock density on a single Harbor makes per-drydock proxies expensive.

## Architecture (when built)

```
Drydock container
├── iptables OUTPUT chain         ← belt: drops packets to non-allowlist IPs
├── egress-proxy (localhost:8118) ← suspenders: returns 403 for non-allowlist SNIs
│     ├── HTTP_PROXY env set globally (shells, language runtimes pick up)
│     └── access log → /var/log/drydock/egress.log
└── (drydock-base:v2 provides both)
```

Proxy implementation candidates (pick during implementation):
- **tinyproxy** — minimal, C, HTTP CONNECT-based, supports domain allowlists via `AllowDomain`. No SNI inspection out of the box; would need wrapping.
- **Squid** — feature-rich, supports SNI-based ACLs, stronger audit logging. Heavier footprint (~30MB).
- **Custom Go/Rust proxy** — smallest attack surface, precisely the features we want, more maintenance cost.

Leaning toward Squid with a narrow ACL config. "Boring is good" for security-adjacent components.

## Known gaps that remain even with belt+suspenders

- **Raw TCP to allowlisted IPs.** Belt allows it. Suspenders doesn't see it. If a project's allowlist contains an IP, any port / any protocol to that IP is allowed. Future work: narrow iptables rules to specific destination ports per IP when the allowlist source declares them.

- **Outbound UDP.** Currently DNS (port 53) + tailscale (41641). Nothing else. Belt blocks the rest. Suspenders is HTTP-only. If UDP allowlisting becomes needed (QUIC? HTTP/3?), belt has to handle it; suspenders can't.

- **DNS tunneling.** An attacker with code-execution inside a drydock could exfiltrate data via DNS queries (UDP 53 is always open). This is a fundamental limit; realistic mitigation is monitoring (suspenders logs), not blocking.

- **TLS without SNI.** Rare in modern TLS 1.3, but possible. Suspenders can't inspect; belt catches at IP layer.

- **Proxy crash = blast radius.** If the egress proxy dies, legitimate HTTPS traffic stops working inside the drydock. Restart semantics and healthcheck needed. A stuck proxy is worse than no proxy (silent hang vs. explicit fail); watchdog + fail-loud on proxy exit.

## When to implement

Implement as part of `drydock-base:v2` when one of:

1. Another "blanket rule" class of bug is caught in review or production — signals iptables-only auditing isn't keeping up.
2. A real need for HTTP-layer audit surfaces (e.g., "show me every external URL this drydock reached this week" — unanswerable with iptables-only).
3. Shared-hosting allowlist precision becomes load-bearing (e.g., allowing access to one specific repo on github.com but not others, impossible at IP layer).

Until then, the 2026-04-15 blanket-443 fix closes the most obvious immediate hole. `init-firewall.sh`'s verification step (the `curl https://example.com` check) stays as-is — it's a real guardrail against regressing the fix. It now passes.

## References

- [capability-broker.md](capability-broker.md) — the lease primitive; a future `NETWORK_REACH` capability type would route here
- [persistence.md](persistence.md) — audit event schema; egress-proxy accept/reject events would extend it
- 2026-04-15 diagnostic confirming conntrack works on Docker-for-Mac, blanket-443 was the root cause of apparent enforcement failure (see session log)
- AWS IP ranges reference: https://ip-ranges.amazonaws.com/ip-ranges.json

## Provenance

Decision made 2026-04-15 in response to the asi drydock firewall investigation. Two Explore subagents ran diagnostics in parallel: one ruled out the conntrack hypothesis, the other surveyed egress-control patterns on Docker-for-Mac. Steven signed off on belt+suspenders as a directional commitment without scheduling the implementation.
