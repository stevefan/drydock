# Employee-worker pattern

A class of Worker that lives persistently on a Harbor, holds real permissions inside a narrow policy scope, and makes bounded judgment calls. Distinct from interactive Claude (short-lived, tied to a human session) and deterministic cron (no judgment). Employees are what V2's capability broker, narrowness validator, and audit log are ultimately *for* beyond nested spawn.

See [vocabulary.md](vocabulary.md) for Harbor / DryDock / Worker. See [capability-broker.md](capability-broker.md) for the lease primitive employees consume.

## Why employees need infra, not a laptop

| Slot | Lives where | Character |
|---|---|---|
| Interactive Claude | Human's laptop | Short session. Scoped to the conversation. Fragile across reboots / OS updates / keychain invalidations. Bound to a human being present. |
| Deterministic cron / scripts | Any Harbor | Persistent. Zero judgment. Breaks silently when assumptions drift. |
| **Employee-worker** | Persistent Harbor (laptop is too ephemeral) | Runs continuously or wakes on schedule. Holds real permissions inside a narrow policy scope. Can read logs, notice anomalies, propose or make bounded fixes, refresh credentials, maintain archipelago state. |

Laptops sleep, reboot, get OS-updated, travel. Hetzner-class Harbors are durable — same identity day after day, same audit principal, same policy scope. Credentials with multi-hour OAuth expiry want to live where something can refresh them on a timer, not where they decay in a sleeping keychain.

## The fleet-auth employee (first instance)

`infra` drydock on `drydock-hillsboro`. Its job:

1. Hold archipelago-level Claude Code OAuth credentials at `~/.drydock/secrets/ws_infra/{claude_credentials, claude_account_state}`.
2. Run `claude remote-control` inside the drydock — that process's built-in refresh loop keeps the OAuth token alive indefinitely.
3. Delegate to peer drydocks on request: another drydock with `request_secret_leases` + `claude_credentials` in its `delegatable_secrets` calls `RequestCapability(type=SECRET, scope={secret_name: "claude_credentials", source_desk_id: "ws_infra"})`. The daemon reads bytes from infra's secret dir, writes a copy into the caller's secret dir (chowned to container uid), audits, returns the lease.

Project YAML shape:

```yaml
repo_path: /root/src/infra
tailscale_hostname: infra
remote_control_name: infra
firewall_extra_domains:
  - claude.com
  - api.anthropic.com
  - login.tailscale.com
  - api.github.com
  - controlplane.tailscale.com
capabilities:
  - request_secret_leases
  - spawn_children
secret_entitlements:
  - anthropic_api_key
  - claude_credentials
  - claude_account_state
delegatable_secrets:
  - anthropic_api_key
  - claude_credentials
  - claude_account_state
```

Secrets on Harbor at `~/.drydock/secrets/ws_infra/` include a daemon-issued `drydock-token` and the human-seeded Claude credentials. An `/etc/cron.d/drydock-infra-auth-check` cron runs every 4h to warn if the OAuth token's `expiresAt` has passed.

## Why V2's primitives unlock this

Employees want permissions beyond what you'd give a random script — modify secrets, call external admin APIs, push PRs. Without V2 that's an unbounded process with no audit trail, no narrowness enforcement, no revocation except "kill the container." With V2:

- **Capability-broker leases** — the employee holds only the leases it needs (`SECRET:claude_credentials` with delegation scope, `STORAGE_MOUNT` for a specific bucket). Revocable.
- **Narrowness validator** — same uniform validator scopes employee actions. "This employee may edit `sites/` but not `firewall-extras.yaml`" is enforced by the daemon, not by ad-hoc CLI allowlist flags.
- **Audit log** — every authority-using action is principal-stamped. Humans review retroactively.

## Variants worth naming

Each is a Worker class; the employee framing is the common pattern.

- **Fleet-auth employee** (shipped). Holds credentials; refreshes them in its own `remote-control` loop; delegates to peers.
- **Provisioner employee**. Runs on Harbor-level broad creds (`drydock-agent` role via mounted `~/.aws/`) to create buckets, wire IAM, set up infrastructure for peer drydocks. Partially lit today — infra can run `aws sts` and `aws s3` commands against a mounted AWS profile. Per-resource narrowness (provision these buckets, not those) is a follow-up capability (`REQUEST_INFRA_PROVISION` or similar).
- **Maintenance employee** (not yet instantiated). Prunes orphan tailnet records, refreshes firewall IP ranges, rotates schedules, rolls logs.
- **Smart-operator / cadence-wrapped worker**. Runs on a schedule but with Claude judgment inside. The auction-crawl smart operator is the prototype: cron fires an adaptive-cadence wrapper that either runs the Claude prompt or skips based on interval-ladder state. Employee flavor of `batch-worker`.

## How to apply

- **Start narrow.** First employee owns one thing. Prove the pattern. Expand scope only when a forcing function surfaces.
- **Persistence > cleverness.** An employee that runs dumbly once a day but has never missed is more valuable than a clever one that tries to do too much.
- **Audit-first design.** Whatever the employee does, a human should be able to read a one-line summary per run. If that line grows noisy, the employee is overreaching.
- **Name what it is.** Call it the archipelago-auth employee, the provisioner employee, the auction-crawl smart operator. Anthropomorphizing here is honest — these ARE long-lived agents with judgment operating on behalf of a human.
