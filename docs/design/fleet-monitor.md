# Fleet monitor

**Status:** sketch · **Depends on:** deskwatch, tailnet-identity, peer-Harbors decision

## Problem

`ws deskwatch` is per-Harbor and exit-coded. Nothing aggregates across the fleet, and nothing alerts when a Harbor itself goes silent (daemon down, host rebooted, network partition). Token-invalidation specifically is invisible: a drydock's container can be `running` while its Claude Code remote-control is dead because the OAuth access token expired. Today this is discovered by trying to use the agent and finding it unreachable.

## Goal

A central observer that, on a schedule:

1. Pulls health from every peer Harbor (`ws daemon status`, `ws deskwatch`, plus a CC-liveness probe).
2. Aggregates to a single fleet-status view (`ws fleet status`, JSON for agents, human table for terminals).
3. Alerts on transitions (healthy→degraded, silent peer, token-invalid) via Telegram (existing side-channel from `1439fce collab`).
4. Stores recent history for trend / "when did this start failing."

Per the peer-Harbors decision (sovereign peers, no federation of state), the monitor is an **observer**, not a coordinator. It reaches in via tailnet-authenticated RPC; it does not own peer state.

## Design

### Topology

A **fleet-monitor desk** runs on one designated Harbor (default: the auth Harbor — same machine, same trust, always-on). The drydock:

- Has a `peers.yaml` listing each peer Harbor's tailnet hostname and the drydocks to monitor (or `*`).
- Runs a polling loop (default 60s) issuing `wsd` RPC over tailnet to each peer's daemon.
- Writes results to a local SQLite (`fleet-status.db`) with schema `(timestamp, harbor, desk, kind, status, detail)`.
- Emits Telegram alerts on state transitions, with a debounce window to avoid flapping.

### What gets probed

| Probe | RPC / Mechanism | Failure means |
|---|---|---|
| Daemon liveness | `wsd.ping` over tailnet socket-forward (or HTTPS sidecar) | Harbor unreachable / daemon dead / tailnet down |
| Container roll-call | `wsd.list_desks` | Desks suspended unexpectedly |
| Per-drydock deskwatch | `wsd.deskwatch_eval <desk>` | Job/output/probe violations |
| **CC liveness** | In-desk RPC: invoke `claude --version` AND a token-validating call (1-token API ping with the active access token) | Container up but auth dead — the gap this whole doc exists to close |
| Cert / token expiry windows | Read `expires_at` from `claude_credentials` on each peer | Expiry < 30min ahead = preemptive refresh trigger |

The CC-liveness probe is the headline feature. Everything else exists in `deskwatch` already; the monitor's contribution is **aggregation + alerting + cross-Harbor visibility**.

### CLI surface

| Command | Purpose |
|---|---|
| `ws fleet status [--peer P] [--since DURATION]` | Current rolled-up status. Exits non-zero if any unhealthy. |
| `ws fleet history <harbor> <desk>` | Time-series of probe results for one drydock. |
| `ws fleet probe <harbor> <desk>` | Run probes once on demand (debug). |
| `ws fleet alerts [--ack ID]` | List active alerts; ack to suppress further notifications. |
| `ws fleet add-peer <hostname>` | Add a Harbor to the monitored set. |

### Telegram alert format

```
🔴 fleet: hetzner/auction-crawl
  CC token invalid (probe failed: 401 from /v1/messages)
  last healthy: 2026-05-04T03:12Z (47min ago)
  → triggering auth-broker refresh
```

Auto-recover actions are intentionally narrow: only **trigger an auth-broker refresh** on token-invalid. Container restart or anything destructive stays manual; monitor observes, doesn't repair (the deskwatch contract).

### Defaults to make this possible

- **Monitor location:** auth Harbor by default (co-locates the two always-on responsibilities). One monitor per fleet; not federated.
- **Poll interval:** 60s for liveness, 5min for deskwatch (which has its own internal cadence), 4h for token-expiry check.
- **Telegram destination:** reuse the existing collab bot's chat. Add a dedicated topic/thread for fleet alerts to separate from collab traffic.
- **Alert debounce:** 3 consecutive failures before alerting; 1 success to clear. Prevents tailnet-blip noise.
- **Peer auth:** each peer Harbor's `wsd` issues a `fleet-monitor` bearer token (scope: read-only ping/list/deskwatch). Stored as `fleet_monitor_token_<peer>` in the monitor drydock's secrets.
- **Storage retention:** 30 days in `fleet-status.db`; rotate / vacuum monthly.
- **History on the auth-broker integration:** when the monitor triggers a refresh, it records the action in audit log on the *auth Harbor*, not the peer (the action originates locally; peer just receives the resulting push).

### Out of scope (V1)

- Cross-Harbor metric aggregation beyond probe results (CPU/disk/network).
- Web dashboard. JSON output + Telegram is enough; if pull-up-on-laptop becomes painful, build later.
- Federated monitor (multiple monitors voting). Single observer per fleet is fine until islanding pain shows up.

## Channel decision (V1)

Peer RPC = **plain SSH to each peer's `ws` CLI** (`ssh hetzner 'ws daemon status --json'`). Reuses existing key topology proven by `claude-refresh.sh`, no new attack surface on `wsd`, ships today. Per-call SSH overhead (~200-500ms) is fine at 60s polling cadence; revisit if cadence drops below 10s or peer count crosses ~10. Upgrade path: `tailscale serve` mapping HTTPS on the tailnet to `wsd`'s Unix socket, with tailnet identity for auth.

## Open questions

1. Should the fleet-monitor itself be a worker pattern (long-running judgment agent per `employee-worker.md`), or a deterministic poller? Likely deterministic for V1; promote to judgment agent when alert routing becomes nuanced ("is this real or ignorable").
3. CC-liveness probe: does the access-token validation cost count against rate limits in a meaningful way at 60s × N drydocks? Probably negligible but check.
