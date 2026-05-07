# Make the harness live — V3's resource boundary as a living allocator

**Status:** design + plan · 2026-05-06 · **Pulls together:** [vocabulary.md](vocabulary.md) §V3, [resource-ceilings.md](resource-ceilings.md), [amendment-contract.md](amendment-contract.md), [proxy-egress.md](proxy-egress.md), [capability-broker.md](capability-broker.md), [narrowness.md](narrowness.md)

This doc is the unified treatment of Tier 2a in `roadmap-2026-05.md`. It defines the four primitives that, together, make V3's "DryDock = mutable resource boundary" claim operationally true. Each primitive is small individually; collectively they are the operational implementation of V3.

Sections:
- [Glossary](#glossary) — basic terms used throughout
- [The shared first principle](#the-shared-first-principle)
- [2a.1 Proxy egress](#2a1-proxy-egress)
- [2a.2 Cgroup live update](#2a2-cgroup-live-update)
- [2a.3 WorkloadLease end-to-end](#2a3-workloadlease-end-to-end)
- [2a.4 Migration primitive](#2a4-migration-primitive)
- [Cross-cutting concerns](#cross-cutting-concerns)
- [Implementation plan](#implementation-plan)
- [Open questions](#open-questions)

---

## Glossary

These terms are used throughout. Defined here so the body can move at speed.

- **cgroup** *(control group)* — Linux kernel mechanism for grouping processes and applying resource limits to the group as a unit. Memory cap, CPU shares, PID count, IO bandwidth — all enforced by the kernel via cgroup files like `/sys/fs/cgroup/memory/<group>/memory.max`. Containers run inside one cgroup per container; updating the file updates the limit immediately.
- **SNI** *(Server Name Indication)* — TLS extension. The very first packet of a TLS handshake (the *ClientHello*) carries the hostname the client wants to reach, in plaintext. Servers use it for name-based virtual hosting; intermediaries can use it for policy without breaking encryption.
- **CONNECT** — HTTP method used to ask a forward proxy to open a TCP tunnel to a destination. The proxy sees the destination hostname in the CONNECT request, decides allow/deny, then either tunnels bytes blindly or refuses. Standard mechanism for HTTPS-through-proxy.
- **MITM** *(Man-In-The-Middle)* — full TLS interception, where the proxy terminates the worker's TLS connection, inspects/modifies traffic, and opens a second TLS connection to the destination. Requires a *CA* (Certificate Authority) cert installed in the worker's trust store. Defeats end-to-end encryption.
- **iptables / netfilter** — Linux kernel packet filtering. Rules match packets (by IP, port, etc.) and apply actions (ACCEPT, DROP, REJECT). The "default-deny OUTPUT chain" pattern means *every* outbound packet is dropped unless an earlier rule explicitly accepts it.
- **ipset** — kernel data structure for fast set-membership queries on IPs/CIDRs/ports. Used by iptables: a rule like "ACCEPT if dst in `allowed-domains`" matches efficiently against thousands of entries.
- **CIDR** *(Classless Inter-Domain Routing)* — IP range notation. `10.0.0.0/8` means "any IP starting with 10." Used to express subnets compactly.
- **veth** *(virtual ethernet pair)* — pair of virtual network interfaces. Containers' network namespaces use one end of a veth; the host bridge sees the other end. The container's outbound traffic flows out the host-side veth.
- **tc / HTB** *(Linux Traffic Control / Hierarchical Token Bucket)* — kernel egress shaping. Attach a queueing discipline (qdisc) to a network interface; the kernel rate-limits or prioritizes packets. `tc qdisc add dev <veth> root tbf rate 10mbit` caps egress at 10 Mbps.
- **OOM kill** *(Out-Of-Memory)* — when a cgroup hits its memory limit and can't reclaim enough, the kernel kills a process inside the cgroup. Abrupt and unsignalable to userspace.
- **STS** *(AWS Security Token Service)* — AWS service that mints short-lived credentials with attached session policies (typically a few hours). Drydock's STORAGE_MOUNT capability uses STS to issue scoped S3 credentials.
- **WAL** *(Write-Ahead Log)* — SQLite mode where writes go to a separate log file before being applied to the main DB. Litestream replicates the WAL to S3, giving live-backed SQLite.
- **bind mount** — making a directory or file at one path appear at another path. The container sees the host's `~/.drydock/secrets/<id>/` at `/run/secrets/`.
- **systemd / launchd** — Linux's / macOS's service supervisor. Manages start, stop, restart, dependencies, logging.
- **devcontainer** — Microsoft's spec for "container as development environment." A `devcontainer.json` describes how to build/run a container; the `devcontainer up` CLI brings it up.
- **DDL / DML** *(Data Definition / Manipulation Language)* — SQL flavors. DDL is `CREATE TABLE`, `ALTER`, `DROP`. DML is `INSERT`, `UPDATE`, `DELETE`.
- **PRAGMA** — SQLite directive for setting/querying database options, e.g., `PRAGMA table_info(<table>)`.
- **drain** — graceful-stop pattern. Tell a process "you're stopping soon, finish in-flight work, persist state, then exit." Distinct from a SIGKILL.
- **snapshot** *(here)* — capture of a drydock's portable state (registry row, secrets, worktree pointer, volumes) into a single addressable artifact, restorable atomically.
- **lease** — drydock's term for a time-bounded grant of authority. The capability broker issues leases (SECRET, STORAGE_MOUNT, NETWORK_REACH, INFRA_PROVISION). WorkloadLease bundles multiple sub-leases.

---

## The shared first principle

V3's claim is structurally simple: **the runtime container and the resource boundary are different things, and conflating them costs us.**

The container is what runs the worker's code. It is the *security boundary* — compromise stays inside; recreating it is a fresh isolation slate. Container creation/destruction should be a serious operation, reserved for what it actually is — a security event (fresh image, code reset, suspected compromise).

The harness is everything *around* the container — what the container is allowed to reach, what it's allowed to consume, what it can ask the daemon for. The harness is what the principal *governs*. The harness is the *resource boundary*.

Pre-V3, drydock conflated them. Every parameter of the harness — firewall rules, cgroup ceilings, mounts, port forwards, allowed domains — was baked into `docker run` flags or container env, set at start, immutable until container recreate. This was operationally fine when the harness was a static recipe ("infra desk has these caps forever"). It collapses when the harness needs to *react*:

- A worker discovers it needs a domain it didn't list at start.
- A workload registration says "this drydock needs 8 GB for 2 hours."
- A principal wants to narrow a desk's reach in response to a suspicious signal.
- A daemon-version upgrade requires a structural state transition.
- An image bump is needed without losing accumulated state.

In each case, today's mechanism is *recreate the container*. The amendment contract — "Dockworker proposes → Authority decides → policy updates" — collapses into "stop, edit YAML, recreate, restart, verify."

The V3 thesis: **the harness should be a living allocator the daemon manipulates, not a frozen recipe baked into the container's birth.** Container recreation stays as a security operation. Resource-policy changes happen on the live container.

The four Tier 2a items are not independent features. They're the same shift — making the harness live — applied to four resource axes:

| Axis | Today | V3 |
|---|---|---|
| Network reach | iptables/ipset of resolved IPs, set at container start, recreate to update | Forward proxy with SNI allowlist, daemon-owned file, SIGHUP to reload |
| CPU/memory ceilings | docker run flags, immutable | `docker update` on lease grant/expire, kernel sees changes immediately |
| Workload-shaped allocation | Each capability (mount, secret, reach) requested separately, no atomic semantics | `RegisterWorkload` issues a bundled `WorkloadLease` covering all sub-resources atomically |
| Structural transitions | Bespoke shell pipes (manual stop, edit, recreate) | `drydock migrate` state machine with structured drain/snapshot/rollback |

Each is independently shippable. Together they make V3 an operational reality.

---

## 2a.1 Proxy egress

### Why now

Tonight's litestream provisioning hit the iptables/ipset firewall's structural limits five distinct times. S3 returned different IPs per query (CDN load balancing); the 15-minute `refresh-firewall-allowlist.sh` loop was constantly behind; AWS's published `us-west-2:S3` CIDRs covered most of the rotating IPs but not all. Each cycle ended with me hand-injecting IPs into the *ipset* on the running container — a workaround that re-exposes the same problem on the next CDN rotation.

The fix isn't "tune the refresh interval lower." The fix is "stop tracking IPs."

### First principles

The fundamental question: *what is a network policy boundary, and where does it live?*

Today the boundary lives at *layer 3* (IP packets). The container has a default-deny iptables OUTPUT chain; the only way out is via an `ipset` of allowed IPs that `init-firewall.sh` populated at container start by resolving `FIREWALL_EXTRA_DOMAINS`. This is wrong on two axes:

1. **The semantic level is wrong.** Policy is authored at *layer 7* (domains: "this drydock can reach github.com"). Enforcement is at layer 3 (IPs). The translation is fundamentally lossy:
   - One domain → many IPs (CDN: every query of `s3.us-west-2.amazonaws.com` returns 8 different IPs).
   - Many domains → one IP (virtual hosting: `*.s3.amazonaws.com` shares hosts).
   - IPs change without notice (CDN rotation, AWS subnet expansion).
   - The translation is *eventually consistent* with a 15-minute lag; policy intent is *immediately* consistent. The lag IS the bug.

2. **The locus is wrong.** The boundary lives in netfilter, owned by the container at start. The principal can't reach into it without a container recreate. The amendment contract requires a *control plane* the daemon can talk to; netfilter inside an isolated network namespace isn't that.

The first-principles correct shape: put the boundary at *layer 7* (where policy is authored), and put it in a process the daemon can talk to (the control plane). A forward proxy with SNI inspection does both.

### How it works

A *forward proxy* sits between the worker and the internet. The worker is configured to send all HTTP/HTTPS traffic through it (via the standard `HTTP_PROXY` and `HTTPS_PROXY` environment variables). The proxy:

1. Receives the worker's connection.
2. For HTTPS: reads the *SNI* in the TLS *ClientHello*. The hostname is in plaintext.
3. Compares the hostname against an in-memory allowlist.
4. If allowed: opens a TCP connection to the destination, then *tunnels bytes blindly* between worker and destination. The TLS handshake happens between worker and destination; the proxy never sees plaintext.
5. If denied: returns HTTP 403 (or TCP RST), connection terminates.

For policy: domain-based. For encryption: end-to-end preserved (no MITM, no CA injection). For dynamism: the allowlist is a file the daemon writes; SIGHUP reloads it without restarting the proxy or the container.

The trade-off: SNI peeking can decide "github.com yes" but not "github.com/api yes, github.com/users no" — granularity is *domain-level*, not URL-level. For drydock's threat model (preventing exfiltration to unexpected domains), domain-level is the right granularity.

### The tool: smokescreen

[Stripe's smokescreen](https://github.com/stripe/smokescreen) is purpose-built for this. Single Go binary. Production-tested at scale. Properties that matter:

- YAML-driven allowlist. Reload on SIGHUP.
- Built-in SSRF (Server-Side Request Forgery) protection: blocks RFC1918 (private IPs: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), link-local (169.254.0.0/16 — including AWS metadata service), GCP/Azure metadata equivalents.
- Per-request audit log: client identity, destination hostname, allowed/denied, duration. Feeds straight into the daemon's audit pipeline.
- Glob support: `*.github.com` is one entry, not 12.
- HTTP `/health` endpoint for the Auditor's watch loop.

Drop-in alternative if smokescreen ever stops fitting: Squid in `https_port` mode + `ssl_bump=peek` (heavier, more configurable, classic). Custom Python proxy is tempting but TLS handshake parsing is fiddly and the security surface widens.

### Architecture

```
┌─────────────────────────────────┐
│  Drydock container              │
│                                 │
│   worker process                │
│       │                         │
│       │ HTTP_PROXY=             │
│       │   http://127.0.0.1:4750 │
│       ▼                         │
│   smokescreen ─────────────────┼──► destination (HTTPS)
│       │                         │
│       │ ◄── allowlist file       │
│       │     (daemon writes;     │
│       │      SIGHUP to reload)  │
│       ▼                         │
│   iptables OUTPUT chain:        │
│   default DROP except           │
│   destination 127.0.0.1:4750    │
└─────────────────────────────────┘
```

Default-deny iptables stays. Even if smokescreen is compromised, the worker can't escape to arbitrary IPs — netfilter is the security floor. Smokescreen is the policy layer on top: defense in depth, not redundancy.

### YAML contract

The existing `delegatable_network_reach` glob list becomes the proxy's allowlist verbatim:

```yaml
narrowness:
  network_reach:
    - "*.github.com"
    - "api.anthropic.com"
    - "*.s3.us-west-2.amazonaws.com"   # SNI matches because cert SAN matches
    - "huggingface.co"
  network_reach_ports: [443, 22]      # default 443; explicit if other
```

`firewall_extra_domains`, `firewall_aws_ip_ranges`, `firewall_ipv6_hosts` become obsolete. Migrate existing project YAMLs by folding their contents into `network_reach`. Keep the old fields as deprecated aliases for one release cycle.

### Implementation phases

**E0 — drydock-base ships smokescreen** (~1 day)
- Add to `base/Dockerfile`: download smokescreen v0.0.x binary, install at `/usr/local/bin/smokescreen`.
- Add `base/start-egress-proxy.sh` — analogous to `start-tailscale.sh`. Reads allowlist from `/run/drydock/proxy/allowlist.yaml`, starts smokescreen on `127.0.0.1:4750`, daemonizes.
- Add `base/init-firewall.sh` mode `egress_proxy=enabled`: OUTPUT chain becomes "ACCEPT to 127.0.0.1:4750, DROP everything else." Skip ipset population entirely.
- No project YAML field yet — defaults to disabled. Existing desks unchanged.
- **Test:** new container with `egress_proxy: true` env can reach `github.com:443` via proxy; cannot reach arbitrary IPs.

**E1 — opt-in per drydock** (~1 day)
- Add `egress_proxy: enabled` to project YAML schema.
- `overlay.py` writes the allowlist file path into the bind-mounted config dir.
- `core/proxy.py` (new): generates the smokescreen YAML allowlist from `narrowness.network_reach`.
- Daemon RPC `UpdateProxyAllowlist(drydock_id)` writes the file + signals smokescreen via `pidof smokescreen | xargs kill -HUP` inside the container (via the existing in-container daemon-rpc).
- `drydock project reload` calls `UpdateProxyAllowlist` after re-pinning.
- **Test:** `drydock project reload` after editing `network_reach` → new domain reachable within 5 seconds, no container restart.

**E2 — make it the default** (~half day)
- New desks created after a config flag flip default to proxy.
- Old desks migrate via `drydock project reload` at convenient times.
- Document the migration. Communicate the deprecation of `firewall_extra_domains`.

**E3 — wire NETWORK_REACH RPC to proxy mutation** (~half day)
- `RequestCapability(type=NETWORK_REACH, scope.domain=...)`: validates against `delegatable_network_reach`, appends to allowlist with TTL = lease expiry, SIGHUPs.
- Lease release (or expiry sweeper) removes the entry, SIGHUPs.
- This is the contract the amendment loop has been waiting on. After E3, "Dockworker proposes a domain → daemon adjudicates → allowlist updates → next request succeeds" is sub-minute.
- **Test:** RequestCapability for new domain → smokescreen log shows allow within 1s; lease release → next request denied within 1s.

**E2 + E3 retire ~300 lines of code:** `add-allowed-domain.sh`, `refresh-firewall-allowlist.sh`, the FIREWALL_AWS_IP_RANGES handling in init-firewall, the entire ipset-rotation-tracking machinery. The delete is the satisfying part.

### Failure modes

- **Proxy down → all egress dies.** Mitigation: smokescreen runs as a supervised process (init script restarts on exit); Auditor probes `/health` and escalates if down >2 min.
- **Misconfigured glob lets too much through.** Same risk model as today's `firewall_extra_domains`. Auditor flags suspiciously broad globs.
- **DNS poisoning.** Proxy resolves DNS server-side; if resolver compromised, allowlist meaningless. Mitigation: DoH/DoT to trusted resolver. Phase 2 concern.
- **Non-HTTP egress.** SSH, raw TCP — not proxied. Today's special-case iptables rule for tcp:22 to GitHub stays as an explicit per-port allowlist. No regression.

### What lives in audit

- `proxy.allowlist_updated` — full diff: added/removed domains, source (project_reload | NETWORK_REACH lease | NETWORK_REACH revoke).
- `proxy.request_denied` — drydock_id, destination, timestamp. Auditor watches for runs of denials (signal of a confused worker or a misconfigured allowlist).
- `proxy.health_check_failed` — escalates after threshold.

---

## 2a.2 Cgroup live update

### Why now

The smallest item; biggest pull per line of code.

Today's `WorkloadLease` (per `resource-ceilings.md §3`) lifts the *soft* ceiling — the Auditor's observation threshold — but does nothing to the *hard* ceiling enforced by the kernel. A worker that legitimately needs 8 GB on a 4 GB-capped drydock still gets *OOM-killed* by the kernel before the Auditor can even observe the spike. The contract — "register workloads, get lifts, do the heavy thing safely" — is half-built.

### First principles

The fundamental question: *if the harness is the resource boundary and the daemon is the resource governor, why are resource limits frozen at the container's birth?*

There's no kernel constraint forcing this. Cgroup limits are mutable at runtime — write a new value to `/sys/fs/cgroup/<group>/memory.max` (cgroup v2) or the equivalent v1 file, and the kernel applies it immediately. Docker exposes this as `docker update --memory --cpus --pids-limit <container>`. The container doesn't restart, the worker process doesn't notice; from its perspective, the kernel "just" allowed it to allocate more. The reverse — lowering — is also live; if the worker is currently using more than the new ceiling, it'll start getting refused on next allocation.

What's missing is not the *capability* but the *contract*: when should the cgroup limit change? Without a contract, even live updates are noise.

The WorkloadLease design IS the contract:
- A worker about to do a heavy task **declares the workload** (kind, expected peak resources, duration).
- The daemon evaluates against the desk's `workload_max` — the principal-set ceiling for this kind of workload.
- If granted: lease issued, valid for the workload's expected duration.
- At lease grant: hard cgroup ceilings lift to the granted level.
- At lease expiry / explicit release: ceilings revert.

This makes resource ceilings *workload-shaped* rather than *worker-shaped*. The soft caps for a desk stay tight (small standing allocation, tight anomaly detection); when a heavy task is legitimately needed, the worker declares it, the daemon allocates, the principal sees it in audit.

### How it works

When `RegisterWorkload(spec)` succeeds:

```python
# Pseudocode in capability_handlers.py
lease = create_workload_lease(spec)
if "memory_peak" in spec.expected:
    subprocess.run(["docker", "update",
                    "--memory", str(spec.expected.memory_peak),
                    container_id], check=True)
if "cpu_max" in spec.expected:
    subprocess.run(["docker", "update",
                    "--cpus", str(spec.expected.cpu_max),
                    container_id], check=True)
record_audit("workload.cgroup_lifted", {
    "drydock_id": ws.id,
    "lease_id": lease.id,
    "memory_old_max": ws.original_memory_max,
    "memory_new_max": spec.expected.memory_peak,
    ...
})
```

When the lease expires (sweeper runs every minute) or is released:

```python
subprocess.run(["docker", "update",
                "--memory", str(ws.original_memory_max),
                "--cpus", str(ws.original_cpu_max),
                container_id], check=True)
record_audit("workload.cgroup_reverted", {...})
```

### Where the original ceilings live

The "ceiling to revert to" needs to be canonical. Today the values are in project YAML's `resources.hard` and computed at create time. We need to persist the *applied* values to the registry at `drydock create`/`upgrade`/`project reload` time, in a new column `original_resources_hard` on `drydocks`. That way the sweeper has an authoritative answer regardless of what the project YAML currently says (which might have been edited mid-lease).

### Implementation phases

**Single phase, ~half-day.**

- Schema migration: add `drydocks.original_resources_hard TEXT DEFAULT '{}'` column. Populate at create/upgrade/reload.
- `core/cgroup.py` (new): `apply_cgroup_limits(container_id, limits: HardCeilings) -> None`. Wrapper around `docker update`.
- `daemon/capability_handlers.py`: in the (existing) `RegisterWorkload` handler, call `apply_cgroup_limits` with the granted limits.
- `daemon/server.py` lease-expiry sweeper: on lease expiry, call `apply_cgroup_limits` with `original_resources_hard`.
- Audit events: `workload.cgroup_lifted`, `workload.cgroup_reverted`. Include before/after values.
- **Test:** integration test creates a desk with 1 GB cap, registers a workload with 4 GB peak, verifies `docker inspect` shows the new cap, allocates 2 GB inside the container, lease expires, verifies cap reverted and a 2-GB allocation now triggers OOM.

### Failure modes

- **`docker update` fails partway** (e.g., daemon crash). Mitigation: idempotent — sweeper will retry; lease state recorded before kernel update; on daemon restart, sweeper reconciles.
- **Worker has already allocated more than the lower-revert cap when lease expires.** Kernel doesn't reclaim; new allocations fail. Worker may crash. Acceptable (the lease said the workload would end; if it didn't, that's the worker's bug). Auditor flags overrun via the soft ceiling.
- **Container restarts mid-lease.** New container has the original (low) cap; lease's lift is lost. Mitigation: at container start, daemon reapplies any active workload leases for that drydock. `daemon.recovery` already has the resume path; we hook in here.

### What lives in audit

- `workload.registered` — already emitted; gets a new `cgroup_changes` field with before/after.
- `workload.cgroup_lifted` — separate granular event for the kernel-side change.
- `workload.cgroup_reverted` — same, on expiry.

---

## 2a.3 WorkloadLease end-to-end

### Why now

`resource-ceilings.md §3` is the most-developed sketch in the repo for this primitive but the implementation is partial. After 2a.2, hard cgroup lift works. This phase makes the full bundle work — atomically.

### First principles

The fundamental question: *what does a workload actually consume, and can the harness allocate all of it atomically?*

A heavy workload is rarely just one resource. A research-fine-tuning workload needs:
- Memory + CPU lift (hard cgroup).
- Disk write headroom (different ceiling, different mechanism).
- Anthropic token budget lift (broker-tracked, not kernel-enforced).
- Egress bandwidth lift (`tc/htb` shaping).
- Possibly new domains to reach (`huggingface.co`, S3 buckets) — NETWORK_REACH leases.
- Maybe an STS-credentialed S3 mount — STORAGE_MOUNT lease.
- A duration bound — `expires_at`.

Today these are six separate RPCs the worker would have to compose, each independently fallible, with no rollback if one fails. If the fifth one fails after the first four succeeded, the worker is in a half-allocated state with no clean way back. That's untenable for the workload-as-transaction-boundary model.

The first-principles correct shape: **workloads are atomic**. `RegisterWorkload(spec)` is a single RPC returning a single `WorkloadLease(id, granted_at, expires_at, granted_caps, bundled_leases)`. Either every resource lift succeeds or none of them do. At expiry, all lifts revert atomically.

This also aligns the workload with the unit of *expected behavior* — the granularity the Auditor wants. The Auditor's prompt now has a concrete object to compare against: "this drydock declared a 2-hour fine-tune with 50 GB egress and 1M tokens; observed actuals are 3.5 hours, 200 GB egress, 1.2M tokens." Anomaly detection becomes structural — the workload's declaration *is* the baseline.

### Per-resource enforcement primitive

Each sub-resource in a WorkloadLease has its own enforcement primitive. They differ by mechanism but share the lease-grant-and-revert structure:

| Resource | Primitive | At grant | At expire |
|---|---|---|---|
| Memory / CPU / PIDs | `docker update` (2a.2) | apply lifted values | apply original values |
| Egress bandwidth | `tc qdisc` on the drydock's veth | `tc qdisc add dev <veth> root tbf rate <granted>` | `tc qdisc del dev <veth> root` (back to default) |
| Network reach (additional domains) | smokescreen allowlist (2a.1) | append domains, SIGHUP | remove domains, SIGHUP |
| Storage mount | STS lease | mint creds, write to `/run/secrets/aws_*` | revoke STS session, remove files |
| Anthropic tokens | broker-tracked counter | record allocated quota | revoke `claude_credentials` lease, signal worker (`RestartDeskAgent`) |
| Disk write quota | filesystem quota (xfs/btrfs only) | `xfs_quota -x -c 'limit bhard=<size>M'` | restore original |

The Anthropic tokens case is the most cooperative: revocation requires the worker to actually stop using its in-memory token, which means killing the worker process or signaling it explicitly. We document this as the *cooperative-vs-coercive* distinction. Coercive: kernel-enforced (cgroup, tc, filesystem quota). Cooperative: requires worker action (token revocation, secret rotation).

### How it works

```python
# Pseudocode in capability_handlers.py
def register_workload(spec, ws):
    # 1. Validate against policy
    if not policy_allows_workload(spec, ws):
        raise narrowness_violated(spec)

    # 2. Prepare each sub-action (don't apply yet)
    actions = []
    if spec.expected.memory_peak > ws.original_resources_hard.memory_max:
        actions.append(CgroupLift(memory=spec.expected.memory_peak))
    if spec.expected.egress_bytes > ws.standing_egress_cap:
        actions.append(EgressShape(rate=spec.expected.egress_bandwidth))
    for domain in spec.domains_needed:
        actions.append(NetworkReachGrant(domain=domain))
    if spec.expected.storage_bucket:
        actions.append(StorageMountGrant(spec.expected.storage_bucket))

    # 3. Apply atomically
    applied = []
    try:
        for action in actions:
            action.apply()
            applied.append(action)
    except Exception:
        # Roll back any that succeeded
        for action in reversed(applied):
            try: action.revert()
            except: log_error_but_continue()
        raise

    # 4. Record lease
    lease = WorkloadLease(
        id=new_lease_id(),
        drydock_id=ws.id,
        spec=spec,
        applied_actions=[a.serialize() for a in applied],
        granted_at=now(),
        expires_at=now() + spec.duration_max,
    )
    persist(lease)
    record_audit("workload.lease_granted", lease.to_audit())
    return lease

def revoke_workload(lease_id):
    lease = lookup(lease_id)
    for action in reversed(lease.applied_actions):
        action.revert()  # idempotent
    record_audit("workload.lease_revoked", lease.to_audit())
```

The `actions = [CgroupLift(...), EgressShape(...), ...]` pattern is a standard transaction script. Each action is reversible; the rollback path is the inverse of the forward path. The lease persists the list of applied actions so revoke/expiry can replay the rollback even after a daemon restart.

### Implementation phases

**WL1 — schema + RegisterWorkload happy path** (~1 day)
- `leases` table extension (or new `workload_leases` table) to hold workload spec + applied-actions list.
- `core/workload.py` (new): `WorkloadSpec`, `WorkloadLease`, validation against project YAML's `workload_max`.
- `RegisterWorkload` RPC handler: spec validation + sub-action preparation + atomic apply.
- Lift cgroup and egress only initially (the deterministic mechanisms).
- **Test:** RegisterWorkload with cgroup + egress lift; verify both applied; revoke; verify both reverted.

**WL2 — bundled NETWORK_REACH + STORAGE_MOUNT** (~1 day)
- Sub-actions for NETWORK_REACH (after 2a.1) and STORAGE_MOUNT (existing handler factored to support being called as a sub-action).
- Unified rollback semantics across mixed sub-types.
- **Test:** RegisterWorkload requesting cgroup + 2 new domains + an S3 mount; verify all applied; force a fail in the middle; verify clean rollback.

**WL3 — Anthropic-token cooperative path** (~half day)
- Sub-action for token-budget lift; on revoke, signals worker via `RestartDeskAgent` RPC.
- Documents the cooperative semantics in the audit log.
- **Test:** RegisterWorkload with token lift; verify quota tracker; revoke; verify worker received signal.

**WL4 — expiry sweeper + daemon-restart recovery** (~half day)
- Sweeper runs every 60s (configurable), scans active leases, revokes expired ones.
- On daemon startup, scan for active leases and re-apply their sub-actions (idempotent — they should still be in effect on the kernel side, but the daemon needs to know about them for the revoke path).
- **Test:** force a daemon restart mid-lease; verify lease tracked correctly afterward.

**WL5 — escalation when workload exceeds standing policy** (~half day)
- If `RegisterWorkload` would require lifting beyond `workload_max`, refuse with a structured response that's also an Amendment proposal (per `amendment-contract.md`).
- The Auditor's deep-analysis path handles the escalation to principal via Telegram.
- Lease becomes effective on principal one-word approval.
- **Test:** RegisterWorkload exceeding `workload_max`; verify amendment created; mock principal approval; verify lease materialized.

### What lives in audit

- `workload.lease_granted` — full structured spec + applied-actions list.
- `workload.lease_revoked` — same plus actuals (declared vs. observed for each resource).
- `workload.lease_partial_apply_rollback` — diagnostic, when atomic apply rolls back; helps debug the action implementations.
- `workload.lease_expired` — sweeper-triggered.
- `workload.amendment_proposed` — when the spec exceeds `workload_max`.

### Failure modes

- **Worker exits without revoking lease.** Sweeper picks up at expiry. Worst case: resources stay lifted until expiry timestamp.
- **Sub-action revert fails** (e.g., `tc` returns error). Logged; subsequent reverts continue; lease marked as `revoke_partial` for manual cleanup. Acceptable — better than blocking on a single failed revert.
- **Worker keeps running after lease expires** but lifts have reverted. Worker may start failing. That's correct: the lease was the contract; expired contract means no resource access. Worker should design for it (retry-with-backoff if RegisterWorkload returns permission_denied).

---

## 2a.4 Migration primitive

### Is this actually necessary?

The biggest case to make. Steven asked specifically. Here's the for/against, in full.

#### Against

- **Meta-primitives can paper over real differences.** A daemon upgrade and an image bump and a project YAML refactor have *different requirements*. Forcing them into one shape risks the abstraction leaking — `drydock migrate --target=...` with a bag of mode flags becomes "drydock do-the-right-thing-for-this-case," which is no abstraction at all.
- **Drydock's design ethos is small composable pieces.** `drydock stop` + `drydock create` + a backup script + `drydock host init` are small things that compose. A migration primitive consumes them rather than composing alongside them. That's spending complexity budget.
- **The "rebuild from config" approach is honest.** The vision doc explicitly says "Hardware refresh is a rebuild-from-config runbook (yaml + registry dump + worktree branches on a fresh box), not a daemon primitive." Cross-host migration was deliberately archived because the state-portability problem (especially named volumes) is hard, and rebuild-from-config sidesteps it by treating state as derived rather than primary.
- **Premature.** Tonight's deploy was hairy but it was *one* event. Bundling rare events into a primitive can be over-engineering. Two ad-hoc upgrades in five months of running drydock is not a frequency that earns a primitive.
- **Cross-host migration is the headline use case but probably won't happen.** Steven explicitly chose sovereign-peer Harbors over a federated model. If desks don't move between Harbors as a routine operation, the hardest case is the rarest case, and the primitive is mostly serving same-host cases that `drydock upgrade` already does adequately.

#### For

- **Tonight wasn't one event; it was the *fifth* time the same shape has appeared.** Initial install. Linux-host bug fixes (papercuts memory). V2 daemon rollout. Phase B INFRA_PROVISION. Tonight's rename. Each was: drain → snapshot → stop → mutate → restore → start → verify, ad-hoc shell each time. Five times is past the threshold where a primitive earns its existence — DRY isn't just for code.
- **Each ad-hoc execution risks data loss.** Tonight, if the registry `ALTER TABLE` had failed midway, we'd have had a half-migrated database with no atomic restore. The S3 backup was the safety net but the restore procedure was *not* exercised. A primitive with structured rollback removes the data-loss class.
- **Pre-flight checks are missing.** Tonight: zero pre-checks. Disk space, registry lock, in-flight leases, daemon liveness, target image presence, git working-tree cleanliness — all unverified. A primitive should have a `--dry-run` that prints what would happen, and a default mode that refuses on pre-check failure. That's where operational maturity goes.
- **It surfaces structural changes in the audit pipeline.** Today, a deploy is invisible to audit because it's shell. With a primitive: every migration emits `drydock.migrated` with full before/after state, the Auditor can flag anomalies (a drydock disappearing for longer than the configured drain TTL is a signal), and the principal has a structured record of what changed when.
- **It defines the seams for cross-host migration if/when that day comes.** Building V1 same-host with cross-host shape in mind costs almost nothing extra; retrofitting later is expensive. The seams are: state-capture is an interface (local-tar / litestream / rsync-over-tailnet are implementations); the daemon-side coordinator is a state machine that doesn't care about the implementation.
- **The drain step forces a worker-side contract that doesn't exist yet.** Today there's no way to tell a worker "finish in-flight work, you're moving." Build the primitive and that contract gets built — small, well-defined ("worker SIGUSR1 → returns a JSON drain status → daemon waits up to TTL → SIGTERM"). That contract is independently useful — it's how `WorkloadLease` revoke should work too, how graceful Anthropic-token throttle should work.
- **Schema migration is a recurring problem that gets harder as the system ages.** Tonight's deploy exposed two real migration bugs — `desk_id` columns missed, stale path strings in the registry. Each future schema change has the same shape. A migration primitive that handles "registry version bump" as one of its supported flows — with smoke tests for the bump itself — is way better than "we'll write a one-off Python script every time."

#### Honest verdict

**The primitive is necessary, but the framing should be sharpened.** It is not "moving things between compute backends" — that's the long-arc cross-host case, deferred. It is "*atomic structural transition with rollback*" — the same-host high-frequency case where the existing alternative (manual shell sequences) is objectively worse than a primitive would be.

The cross-host case is the most ambitious goal but the rarest event. The same-host case (deploy, schema migration, image bump, reload-with-recreate) is the high-frequency event where the primitive pays back the fastest. Build for the high-frequency case first; the cross-host axis slots in later as a different "target" implementation if/when the cost of *not* having it crosses a threshold.

### How migration is achieved

The primitive is a *state machine* the daemon runs, not a CLI shell pipe. Stages in detail:

#### Stage 1 — Plan

**Inputs:** source state (read from registry + filesystem) + target spec (image tag, harness config diff, target Harbor — defaults to current Harbor for same-host).

**Output:** structured delta object with one entry per category that's changing. Example shapes:

```yaml
plan:
  drydock_id: dock_collab
  source_harbor: drydock-hillsboro
  target_harbor: drydock-hillsboro    # same-host
  changes:
    image: ghcr.io/stevefan/drydock-base:v1.0.18 → :v1.0.19
    overlay: regenerate                # because image changed
    cgroup_caps: unchanged
    network_reach: unchanged
    secrets: unchanged
    schema_version: unchanged          # daemon's registry version
  estimated_downtime: 30s              # stop + recreate
  in_flight_leases: 1 NETWORK_REACH (auto-renewable)
  rollback_strategy: snapshot-and-restore
```

`--dry-run` prints this and exits. Side effect: none. Failure mode: principal cancels.

#### Stage 2 — Pre-check

Hard refuses (no migration) on:
- Insufficient disk space for the snapshot tarball (defaults to 2× current desk state size).
- Daemon health check failing (target Harbor's daemon, which is local for same-host).
- Worktree has uncommitted changes that the migration doesn't know how to preserve.
- Active WorkloadLease with `expires_at` after `drain_ttl` — would be cut short.
- Target image not present (would require a pull during the downtime window).

Warns (proceeds with confirmation) on:
- Drain TTL is shorter than the worker's typical work-cycle.
- Snapshot tarball would exceed 1 GB.

`--force` bypasses warnings, never bypasses hard refuses.

#### Stage 3 — Drain

**V0 (initial ship):** `docker stop -t <drain_ttl>`. The container's pid 1 receives SIGTERM, then SIGKILL after the TTL. The worker process inside has whatever signal handling its image author wrote — for Claude Code it's no clean drain, but in-memory state is generally not load-bearing for drydock's worker model (worktrees + DBs are on disk).

**V1 (drain contract, post-V0):** daemon writes `/run/drydock/migration-drain.json` with the migration-id and TTL, sends SIGUSR1 to the worker process. The worker is expected to:
1. Read the file.
2. Checkpoint in-flight work (close DB transactions, flush WAL via litestream, signal any sub-processes).
3. Write `/run/drydock/migration-drain-status.json` with `{state: "draining"|"safe-to-stop", in_flight_summary: ...}`.
4. Daemon polls the status file every 1s; proceeds when state is `safe-to-stop` or TTL expires.

**V2 (negotiation, post-V1):** worker can request extension by writing `state: "needs_more_time", reason: "..."`. Daemon either grants (up to a hard ceiling) or proceeds anyway and logs the override.

V0 is fine for tonight's scope; V1 is the right next step; V2 is when production drydocks have non-trivial in-flight state.

#### Stage 4 — Snapshot

Capture portable state into a single addressable artifact at `~/.drydock/migrations/<migration-id>/snapshot.tgz`:

| Component | Capture |
|---|---|
| Registry row | SQL dump of the `drydocks` row (and FK rows in tokens, leases, amendments, deskwatch_events) into a structured JSON blob |
| Secrets dir | `tar czf` of `~/.drydock/secrets/<id>/` with mode bits preserved (mode 0400 files matter) |
| Worktree | `git stash` + branch ref. The git history is the storage; we don't tar the whole worktree, just record the branch + commit |
| Named volumes | `docker run --rm -v <vol>:/data -v $SNAPSHOT_DIR:/host alpine tar czf /host/<vol>.tgz -C /data .` for each volume the desk owns |
| Overlay JSON | straight file copy from `~/.drydock/overlays/<id>.devcontainer.json` |
| Migration metadata | `{migration_id, started_at, source_spec, target_spec, snapshot_paths}` |

Note: **no live container state**. The container is already stopped (or about to be); we capture the *external* state. The container will be recreated fresh from the new image + restored state.

For cross-host (V2): same shape, but the snapshot is encrypted and uploaded to S3 (or an analogous shared substrate). The target Harbor's daemon downloads + decrypts. Tonight's S3 backup pattern is a precursor to this.

#### Stage 5 — Stop

`docker stop` the source container. State in registry → `migrating`. Container removed.

If a previous migration's container is still running for this drydock (failed previous migration, never cleaned up), refuse — caller has to investigate.

#### Stage 6 — Mutate

Apply the delta on the destination. The mutate logic is per-target-type:

- **Image bump:** update image tag in registry; regenerate overlay; nothing else.
- **Schema migration:** run the registry migration; potentially rewrite filesystem paths (per tonight's experience).
- **Project YAML refactor:** re-pin policy from new YAML; regenerate overlay.
- **Same-Harbor identity rebrand (tonight's case):** rename ID, update FK refs, rename filesystem entries.
- **Cross-host (V2):** replicate snapshot to target Harbor's substrate; target's daemon takes over from Stage 7.

Each target-type implementation is a separate module. The primitive is the state machine; the target implementations are pluggable.

#### Stage 7 — Restore

Reverse of Snapshot, against the (potentially mutated) target spec:
- New container created with new overlay (paths point to the new resource boundary).
- Volumes restored: `docker volume create <vol>`; `docker run --rm -v <vol>:/data -v $SNAPSHOT_DIR:/host alpine tar xzf /host/<vol>.tgz -C /data`.
- Secrets dir extracted to the new path (post-rename if applicable).
- Registry row inserted/updated to `state: starting`.

Failure here triggers Rollback (Stage 10).

#### Stage 8 — Start

`devcontainer up` with the new overlay. Daemon waits for the in-container `drydock-rpc` client to respond to a `daemon.health` round-trip on the bind-mounted socket — that's how we know the worker is reachable. Timeout (default 60s); failure triggers Rollback.

Re-apply any active WorkloadLeases for this drydock (cgroup limits, egress shaping, smokescreen entries) — the kernel state was lost at Stop, the registry record persists.

#### Stage 9 — Verify

Health checks:
- `daemon.health` round-trip succeeds.
- Deskwatch evaluation runs clean.
- Any explicit verification probe specified in the migration plan passes (e.g., "container should respond on port 3000").
- If the migration was meant to lift specific resources (rare), those are observable via `docker inspect`.

Pass: registry state → `running`; emit `drydock.migrated` audit event with full before/after + migration-id; cleanup snapshot tarball after retention period.

Fail: Rollback.

#### Stage 10 — Rollback

Inverse of Stages 5–8 from the snapshot:
- New container down (`docker stop`, `docker rm`).
- Registry row restored from snapshot JSON.
- Secrets dir restored from snapshot tar.
- Volumes restored from snapshot tars.
- Overlay file restored.
- Container started under old config.
- State machine returns to `running` (source state).
- Audit event `drydock.migration_rolled_back` with reason.

#### Stage 11 — Cleanup

On success, after a grace period (default 24h), the snapshot tarball is moved to S3 STANDARD_IA cold storage (mirrors tonight's pattern) or deleted per retention policy. Migration record stays in audit indefinitely.

### CLI surface

```
drydock migrate <name> [options]

Options:
  --target image=<tag>        # image bump
  --target harbor=<host>      # cross-host migration (V2)
  --target reload             # re-pin policy from YAML, recreate
  --target schema=<version>   # daemon schema migration
  --dry-run                   # plan + print, no changes
  --drain-ttl <duration>      # default 60s
  --force                     # skip pre-check warnings
  --rollback                  # restore from last successful migration's snapshot
```

### Implementation phases

**M1 — state machine + image bump target** (~2 days)
- `daemon/migrate.py` (new): the state machine. Stages as Python methods; transitions audited.
- `core/snapshot.py` (new): snapshot + restore for the four state components (registry, secrets, worktree-ref, volumes).
- `cli/migrate.py` (new): user-facing CLI.
- Target implementation: `MigrationTargetImageBump` class.
- V0 drain: `docker stop -t <ttl>`, no worker contract.
- **Test:** image-bump migration with rollback on simulated Stage 8 failure.

**M2 — schema migration target + project-reload target** (~1 day)
- `MigrationTargetSchemaMigration` — for daemon-version upgrades that involve registry changes. Wraps the existing `_migrate_v*` functions in `registry.py` as stages.
- `MigrationTargetProjectReload` — for project YAML refactors that change anything baked into the overlay.
- Hooks into existing CLI commands (`drydock upgrade`, `drydock project reload`) so they call into the migration primitive when the change requires it.
- **Test:** schema migration from V5 → V6 (using a synthetic V5 DB); verify rollback works.

**M3 — drain contract V1** (~1 day)
- Worker-side helper library (Python) with `register_drain_handler(callback)` for workers to opt into.
- Daemon-side `daemon.migration.drain` RPC: writes the side-channel file, signals worker, polls status.
- Worker examples: claude-remote-control + telegram-bot (the two that have meaningful in-flight state).
- **Test:** drain contract integration test; worker reports state correctly; daemon waits and proceeds.

**M4 — pre-flight checks + verification probes** (~half day)
- All Stage-2 checks implemented.
- `verification_probes` field in project YAML for migration-time checks.
- **Test:** pre-flight refusal on insufficient disk; warning on drain-TTL too short.

**M5 — cross-host (cross-Harbor) — DEFERRED** (separate epic, ~2 weeks)
- Snapshot uploaded to S3 (or shared substrate).
- Target Harbor's daemon receives `daemon.migration.import_snapshot` RPC, downloads + verifies + restores.
- Tailnet identity reissue.
- Bearer token reissue.
- This is a real epic, not a stage. Triggers when a cross-host event is realistically expected.

### What lives in audit

- `drydock.migration_started` — full plan, drain TTL, target spec.
- `drydock.migration_drain_*` — drain stage transitions (waiting, status updates).
- `drydock.migration_snapshot_taken` — snapshot path, size, components.
- `drydock.migration_stage_*` — one per stage (stop, mutate, restore, start, verify) with status.
- `drydock.migrated` — success terminal event with full before/after.
- `drydock.migration_rolled_back` — rollback terminal event with reason + which stage failed.

### Failure modes

- **Daemon crashes mid-migration.** On restart, daemon detects in-progress migration via persistent migration record; resumes from last completed stage; or rolls back if recovery isn't safe (e.g., crashed during Mutate).
- **Snapshot tarball corrupted or missing during Rollback.** No clean recovery — desk is in indeterminate state. Mitigation: snapshot integrity verified with checksum at Stage-4 end; daemon refuses to proceed past Stage 5 without verified snapshot.
- **`docker volume` operations fail.** Common cause: volume in use by another container. Pre-check verifies single-tenant usage.
- **Drain TTL exceeded.** V0: SIGKILL. V1: configurable behavior — fail (refuse migration) or force (proceed with possible state loss).
- **Verification probe times out but worker is fine.** Probe failure triggers Rollback. False-positive risk; mitigation: probes have their own retry semantics, configurable.

---

## Cross-cutting concerns

### Audit events

After Tier 2a, these new audit events join the vocabulary (all with `drydock.<event>` namespace):

- Proxy: `proxy.allowlist_updated`, `proxy.request_denied`, `proxy.health_check_failed`
- Cgroup: `workload.cgroup_lifted`, `workload.cgroup_reverted`
- WorkloadLease: `workload.lease_granted`, `workload.lease_revoked`, `workload.lease_expired`, `workload.lease_amendment_proposed`, `workload.lease_partial_apply_rollback`
- Migration: `drydock.migration_started`, `drydock.migration_drain_*`, `drydock.migration_snapshot_taken`, `drydock.migration_stage_*`, `drydock.migrated`, `drydock.migration_rolled_back`

The Auditor's prompt grows to know about these. Its anomaly catalog gains entries:
- "Proxy denials clustering on a specific drydock" → confused worker or amendment-needed.
- "Workload declared X memory; observed Y where Y >> X" → divergence; deep analysis.
- "Migration rolled back twice in a week" → operational instability; principal-facing notice.
- "Workload lease expired but worker still active" → either worker is gracefully degraded (good) or stuck (bad).

### Auditor integration

The Auditor watch loop's existing read access to the audit log makes most of this free — new events flow into its measurement layer automatically. The deep-analysis prompt needs updating to know about the new event vocabulary; one prompt-file update per release of Tier 2a.

The Authority's enforcement surface gains one new bucket-2 action: `cancel_workload_lease(lease_id)` — defensive + reversible (the Auditor can revoke a runaway workload's lifts; the worker is restored to standing caps). Stays well within the bucket-2 boundary defined in `auditor_action_authority` memory.

### Failure modes & rollback (cross-cutting)

The unifying principle: **every state-mutating operation must be either idempotent or roll-back-able.** The four primitives compose: a workload registration that touches proxy + cgroup + storage uses sub-action atomicity (2a.3); a migration that involves a workload-active drydock uses drain (2a.4); a schema migration that touches the proxy allowlist file uses the proxy SIGHUP semantics (2a.1).

The single biggest correctness invariant: **the daemon is the only writer.** Smokescreen reads its allowlist; cgroup limits are written via `docker update`; tc rules via the daemon; volume tarballs by the daemon. No worker process directly mutates harness state. Compromise of a worker can never widen its own boundary; it can only request via the (audited) RPC surface.

---

## Implementation plan

### Order

```
2a.1 Proxy egress (E0 → E1 → E2 → E3)         ← ship first; eliminates frictional class
       │
       ├─→ 2a.2 Cgroup live update              ← half-day; unlocks WorkloadLease meaning
       │      │
       │      └─→ 2a.3 WorkloadLease end-to-end ← keystone; depends on E1+ and 2a.2
       │
       └─→ 2a.4 Migration primitive (M1 → M2 → M3 → M4)
              │
              └─→ M5 cross-host                  ← deferred epic
```

2a.1 unblocks 2a.3 (NETWORK_REACH bundling). 2a.2 unblocks 2a.3 (cgroup sub-action). 2a.4 is parallel — independently shippable, doesn't block or get blocked by the others.

### Estimated effort

| Phase | Effort | Dependencies | Notes |
|---|---|---|---|
| 2a.1 E0 | 1d | — | drydock-base ships smokescreen |
| 2a.1 E1 | 1d | E0 | opt-in per drydock; daemon writes allowlist |
| 2a.1 E2 | 0.5d | E1 | make default; deprecate old fields |
| 2a.1 E3 | 0.5d | E1 | NETWORK_REACH RPC → proxy mutation |
| 2a.2   | 0.5d | — | cgroup live update (small; cleanest win) |
| 2a.3 WL1 | 1d | 2a.2 | RegisterWorkload + cgroup + egress |
| 2a.3 WL2 | 1d | E3 + 2a.3 WL1 | + NETWORK_REACH + STORAGE_MOUNT bundling |
| 2a.3 WL3 | 0.5d | WL2 | Anthropic-token cooperative path |
| 2a.3 WL4 | 0.5d | WL3 | expiry sweeper + restart recovery |
| 2a.3 WL5 | 0.5d | WL4 + amendment-contract | escalation when exceeds workload_max |
| 2a.4 M1 | 2d | — | state machine + image-bump target + V0 drain |
| 2a.4 M2 | 1d | M1 | schema-migration + project-reload targets |
| 2a.4 M3 | 1d | M1 | drain contract V1 |
| 2a.4 M4 | 0.5d | M3 | pre-flight checks + verification probes |

Total: ~11 days of focused work. Realistic calendar time: 3-4 weeks given other obligations.

### Suggested sequencing

**Week 1:** 2a.1 E0 + E1 (proxy ships). Highest immediate value. After this, no more firewall-debugging sessions.

**Week 2:** 2a.2 + 2a.3 WL1 (cgroup live + minimal WorkloadLease). The keystone primitive emerges.

**Week 3:** 2a.1 E2 + E3, 2a.3 WL2/WL3 (proxy goes default; WorkloadLease becomes full). The amendment contract is now operationally real.

**Week 4:** 2a.4 M1/M2 (migration primitive ships V1). Same-host migrations stop being shell pipes. Optional: 2a.3 WL4/WL5 for full WorkloadLease maturity.

**Later:** 2a.4 M3/M4 (drain contract, pre-flight). 2a.4 M5 (cross-host) only when needed.

### Acceptance criteria

The section is "complete" when:

1. **Proxy default for new desks** (2a.1 E2) and existing desks migrated.
2. **No remaining usage of `add-allowed-domain.sh` / `refresh-firewall-allowlist.sh`** (deleted code; ~300 lines gone).
3. **WorkloadLease grants visibly affect cgroup + egress + reach + storage atomically** (2a.3 WL2 lands).
4. **At least one schema migration exercised through `drydock migrate`** (2a.4 M2 — exercise it on the next real schema bump).
5. **Auditor prompt updated to know the new event vocabulary** (rolling, per-phase).

### Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Smokescreen incompatible with some destination (TLS protocol quirk) | Low | Medium | Fallback to `firewall_extra_domains` for affected desks; revisit smokescreen alternatives |
| `docker update` semantics differ across docker versions | Low | Low | Version-pin docker in drydock-base; smoke test |
| Cross-host migration design pulls effort early | Medium | Medium | Keep M5 explicitly deferred; resist scope creep |
| Drain contract V0 (just SIGTERM) loses worker state | Medium | Low | Document expectation; V1 contract is the fix; for V0, only desks with no in-flight state are affected |
| WorkloadLease bundling creates lock-step failure modes (one sub-action's failure rolls back everything) | Medium | Low | Atomic-apply is the design choice; document; degraded-grant alternative is post-V1 |
| Auditor prompt becomes too long with all the new events | Medium | Low | Use the file-based prompt + selective context loading from existing Auditor architecture |

---

## Open questions

1. **Should `drydock migrate` and `drydock upgrade` be the same command?** `upgrade` is an existing CLI verb that does ~80% of what M1's image-bump target does. Either deprecate `upgrade` in favor of `migrate --target image=...`, or keep `upgrade` as a shorthand alias. Lean alias for ergonomics.

2. **WorkloadLease and ResourceLease — same primitive?** ResourceLease is hinted at in `resource-ceilings.md` but not built. Probably WorkloadLease is the only one needed; standing soft caps don't need a "lease" per se, they're just policy. Confirm during WL1.

3. **Audit retention for migration snapshots.** Tonight's pattern (S3 STANDARD_IA, no auto-delete) is unbounded. After N successful migrations of a given desk, do we retain the last one or the last K? Policy decision; lean "last 3 + all failures."

4. **Drain contract semantics for non-Python workers.** The Python helper library is one shape; what about workers in Go/Bash/etc.? Probably the file-based + signal-based contract works for any language without a Python dependency. Document the contract explicitly so worker authors can implement in their stack.

5. **Cross-host snapshot encryption.** When M5 lands, snapshots traverse network. What's the encryption story? Symmetric key per-Harbor pair; bootstrapped via existing tailnet trust; rotated per migration. Spec it when M5's forcing function appears.

6. **Smokescreen vs. NETWORK_REACH narrowness ordering.** If `delegatable_network_reach` says `*.github.com` but a NETWORK_REACH lease wants `evil.github.com`, the lease validates against the glob (per existing narrowness model), then is appended to the proxy allowlist. Is the proxy ever the source of truth, or always derived from leases + project YAML? Lean derived; the daemon is canonical.

---

## What this document is not

It's not a commitment to ship in any specific order. It's a coherent treatment of four primitives, their first principles, and an implementation plan. Reality will find new constraints; the plan will adjust. The first principles are the load-bearing part — those should remain stable across replanning.
