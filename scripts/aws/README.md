# drydock AWS identity

Provisions a scoped IAM role for the drydock agent to run as, on Steven's personal AWS account.

## What this creates

| Resource | Purpose |
|---|---|
| `drydock-boundary` (managed policy) | Permission ceiling. Allows `*`, denies self-elevation (on drydock-runner, drydock-agent, drydock-boundary, and Personal-MacBook-Pro user), new IAM identities, account closure, Route 53 domain purchases, and expensive EC2 families (`p*`, `g*`, `x*`, `*.metal`, `*.{12,16,24}xlarge`). |
| `drydock-agent` (IAM role) | Admin-in-sandbox. `AdministratorAccess` attached, `drydock-boundary` as permission boundary, 4h max session. Trust policy allows `drydock-runner` to assume with `sts:RoleSessionName` starting `drydock-*`. |
| `drydock-runner` (IAM user) | Single purpose: `sts:AssumeRole` on `drydock-agent`. Long-lived access keys live on the host at `~/.aws/credentials` as `[drydock-runner]`. |
| `[profile drydock]` in `~/.aws/config` | Auto-assumes the role via `source_profile = drydock-runner`. SDK handles refresh on session expiry. |

## Usage

- `aws --profile drydock <command>` — assumes the role (or uses cached creds if still valid) and runs as drydock-agent
- `scripts/aws/provision.sh` — idempotent create/update of the identity stack (boundary, role, user, keys, profile)
- `scripts/aws/setup-budget.sh` — idempotent create/update of the $25/month cost cap with email alerts
- `scripts/aws/kill.sh` — emergency stop: deactivates runner keys and revokes active role sessions

## Design notes

- **No MFA on assume-role.** Deliberate: drydock runs unattended, a human tap per session would defeat the point. The blast radius is managed via (a) the permission boundary, (b) billing guardrails outside this stack, (c) a fast kill switch.
- **Separate runner user, not `personal`.** If the runner's keys leak, the attacker only gets what drydock-agent can do — not whatever the interactive `personal` user can do. Runner and interactive identities are deliberately uncoupled.
- **4h sessions.** Long enough to be practical for agent runs, short enough that leaked session creds self-expire. Extend to up to 12h by bumping `--max-session-duration` on the role and `duration_seconds` in `~/.aws/config`.
- **Session name prefix.** Trust policy requires `sts:RoleSessionName` matches `drydock-*` so CloudTrail `AssumeRole` events filter cleanly.

## Billing guard

`setup-budget.sh` creates the `drydock-cost-cap` budget ($25/month) with three email notifications to `stevenc.fan@gmail.com`: 50% actual, 100% actual, and 100% forecasted. AWS Budgets is account-level, not role-scoped — this cap applies to all spend on the account, not just drydock. That's deliberate: if drydock spins up something expensive, it'll alert regardless of attribution. Emails come from `no-reply@budgets.amazonaws.com` — whitelist if you filter aggressively. The kill switch (`kill.sh`) does not touch the budget.
