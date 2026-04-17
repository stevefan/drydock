# Drydock Employee: Fleet Auth Desk

**Status:** First instance live as of 2026-04-17. `infra` desk on
`drydock-hillsboro` holds fleet-level secrets and delegates
`claude_credentials` to `auction-crawl` via the V2.1 capability broker.
Cross-desk delegation path empirically validated: lease issued, source
bytes materialized into caller's secret dir, visible inside caller's
container at `/run/secrets/claude_credentials`, audit log stamped.

**Known gap:** in-desk RPC access (desk calling the daemon from inside
its own container) isn't wired up yet — `wsd.sock` isn't bind-mounted
into desks, and `ws` CLI isn't installed in `drydock-base`. Today
cross-desk delegation must be triggered from the host. The consumer-
side flow through the daemon works end-to-end; it just requires a
host-side RPC client rather than an in-desk one. Follow-up: bind-mount
the socket to `/run/drydock/wsd.sock` in the overlay and set
`DRYDOCK_WSD_SOCKET` in containerEnv.

The first concrete instance of the "drydock employee" pattern described
in `project_drydock_employee_pattern.md`: a long-running desk whose
purpose is to hold + refresh credentials and serve them to other desks
via the V2 capability broker.

## What it does

The fleet-auth desk runs `claude remote-control` (or just a background
`claude auth` refresh loop) inside a drydock container. Claude Code's
built-in OAuth refresh mechanism keeps the `.credentials.json` alive
indefinitely once seeded. The desk exposes `claude_credentials` and
`claude_account_state` as secrets that other desks can request via
`RequestCapability(type=SECRET, scope={secret_name: "claude_credentials"})`.

## Why a desk (not a cron job)

- The credential refresh loop is a long-running process, not a
  scheduled batch — it needs to be alive continuously.
- The desk model gives it an identity (desk_id, bearer token), a
  policy scope (what it can delegate), and an audit trail.
- Other desks request credentials through the existing RPC surface
  rather than sharing filesystem paths or volumes.
- The desk can be stopped/destroyed/recreated using `ws` — same
  lifecycle as every other workspace.

## Configuration

### Project YAML (`~/.drydock/projects/fleet-auth.yaml`)

```yaml
repo_path: ~/Unified Workspaces/drydock
devcontainer_subpath: .devcontainer
workspace_subdir: ""
tailscale_hostname: fleet-auth
remote_control_name: Fleet Auth
```

### Capabilities (set at CreateDesk time)

```
delegatable_secrets:
  - claude_credentials
  - claude_account_state
capabilities:
  - request_secret_leases
```

### Secrets on host (`~/.drydock/secrets/ws_fleet_auth/`)

```
claude_credentials      # extracted from Mac keychain via `security find-generic-password`
claude_account_state    # ~/.claude.json from the Mac
drydock-token           # auto-issued by the daemon
```

### Lifecycle

1. `ws create fleet-auth` — provisions the desk
2. Desk starts, init-firewall runs, tailscale joins (for reachability
   from the user's own devices over tailnet)
3. `sync-claude-auth.sh` materializes credentials from secrets into
   the shared claude-code-config volume
4. `claude remote-control` starts and refreshes tokens in the background
5. Other desks call `RequestCapability(type=SECRET, scope={secret_name:
   "claude_credentials"})` — daemon checks entitlement, reads bytes
   from the fleet-auth desk's secret dir, issues lease

### What's needed before this ships

1. **Cross-desk secret access.** Today, RequestCapability reads
   `~/.drydock/secrets/<caller_desk_id>/<name>`. The fleet-auth pattern
   needs a desk to request a secret that lives in ANOTHER desk's secret
   dir. This requires either:
   - A "source_desk_id" parameter in RequestCapability (trust model change)
   - A shared secret namespace (simpler but loses per-desk isolation)
   - A "secret delegation" primitive where fleet-auth grants a lease
     and the daemon copies bytes to the requester's /run/secrets/
   Option 3 (daemon-mediated copy) fits the existing model best and
   preserves per-desk isolation.

2. **Credential refresh validation.** Empirically confirm that the
   container's Claude Code refresh loop actually keeps tokens alive
   without re-extraction from the Mac keychain. As of 2026-04-16
   this is still being validated (noted in docs/host-bootstrap.md).

3. **Health monitoring.** A `ws schedule` job that periodically checks
   whether the fleet-auth desk's credentials are still valid (e.g.
   `ws exec fleet-auth -- claude --print "echo ok"` as a canary).

## Architectural note

The fleet-auth desk is the forcing function for cross-desk capability
delegation (item 1 above). V2.0's RequestCapability is single-desk
(caller requests its OWN secrets). Fleet-auth needs cross-desk access.
This is the gap between V2.0 and V2.1 — and the reason to ship this
employee first, before building the general-purpose delegation model.
