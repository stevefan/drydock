# Secrets

Drydock has two secret surfaces: **per-drydock secrets** (the normal case) and **Harbor-level admin secrets** (daemon infrastructure). Both are file-backed on the Harbor, chmod 0400 / 0o700 dirs, bind-mounted read-only into the drydock where applicable.

Design context: [../design/capability-broker.md](../design/capability-broker.md) covers the lease model and `FileBackend`. This doc is the operator's view.

## Per-drydock secrets

Stored at `~/.drydock/secrets/<dock_id>/` on the Harbor, 0700. Individual files 0400 owned by uid 1000 (the container's `node` user). The overlay bind-mounts the directory read-only at `/run/secrets/` inside the container, so `cat /run/secrets/anthropic_api_key` works with zero setup.

### CLI surface

```
drydock secret set <drydock> <key>        # value read from stdin
drydock secret list <drydock>              # key names only; never values
drydock secret rm <drydock> <key>
drydock secret push <drydock> --to <harbor>  # rsync secrets to a remote Harbor
```

`drydock secret set` is atomic (temp file + rename). `drydock secret rm` is TOCTOU-safe.

### Common keys

| Key | Source | Auto-materialized into container by |
|---|---|---|
| `tailscale_authkey` | Tailscale admin console | `start-tailscale.sh` (reads `/run/secrets/tailscale_authkey` at container start) |
| `anthropic_api_key` | Anthropic console | consumed directly from `/run/secrets/` by whatever needs it |
| `claude_credentials` | Mac keychain: `security find-generic-password -s "Claude Code-credentials" -w` | `sync-claude-auth.sh` → `~/.claude/.credentials.json` |
| `claude_account_state` | Mac `~/.claude.json` | `sync-claude-auth.sh` → `~/.claude.json` |
| `aws_access_key_id` + `aws_secret_access_key` | AWS IAM console (for drydocks that hold static AWS creds; rare — most use STORAGE_MOUNT leases) | `sync-aws-auth.sh` → `~/.aws/credentials` |
| `drydock-token` | auto-issued by `drydock daemon` at `CreateDesk` | Used by `drydock-rpc` as bearer auth |

### Lease-materialized secrets

The daemon writes additional files into `~/.drydock/secrets/<dock_id>/` at lease-issue time:

- **Cross-drydock SECRET** (`source_desk_id` on RequestCapability): daemon copies bytes from `~/.drydock/secrets/<source>/<name>` to `~/.drydock/secrets/<caller>/<name>`. Removed on lease release (if no other active lease grants the same name).
- **STORAGE_MOUNT** leases: daemon writes four files — `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`, `aws_session_expiration`. Overwrites on each new lease (supersede semantics). Removed on release of the last active STORAGE_MOUNT lease.

All lease-materialized files are chowned to uid 1000 and chmod 0400 so the container's `node` user can read them (daemon runs as root; without chown the file is root-owned and unreadable from inside — a latent bug before today's fix).

## Harbor-level admin secrets

Stored at `~/.drydock/daemon-secrets/` on the Harbor, 0700. Not bind-mounted anywhere — only the daemon (running as root) reads them.

| Key | Purpose |
|---|---|
| `tailscale_admin_token` | Tailscale API token. Daemon uses it on `DestroyDesk` cleanup to DELETE the device record and for `drydock tailnet prune --apply` orphan cleanup. See [../design/tailnet-identity.md](../design/tailnet-identity.md). |
| `tailscale_tailnet` | Tailscale tailnet name (e.g. `tail7b11b0.ts.net`). Paired with the admin token. |

## Refresh patterns

- **Claude OAuth tokens** rot over time (refresh tokens eventually expire; Mac keychain IS the refresh mechanism for file-consumers — the on-disk `.credentials.json` does not self-update). When `infra`'s auth-check cron warns:

  ```sh
  # On Mac
  security find-generic-password -s "Claude Code-credentials" -w \
    | ssh root@<harbor> 'cat > /root/.drydock/secrets/dock_infra/claude_credentials
                          && chmod 400 /root/.drydock/secrets/dock_infra/claude_credentials
                          && chown 1000:1000 /root/.drydock/secrets/dock_infra/claude_credentials'
  cat ~/.claude.json \
    | ssh root@<harbor> 'cat > /root/.drydock/secrets/dock_infra/claude_account_state && ...'
  ```

  Then re-run `sync-claude-auth.sh` inside the container (or `drydock stop infra && drydock create infra`).

- **AWS STS credentials** (STORAGE_MOUNT): automatic — they expire on the lease's `expiration` timestamp, and a worker calls `RequestCapability` again to get a fresh set.

- **Tailscale authkeys**: re-issue from Tailscale admin console, `drydock secret set <drydock> tailscale_authkey`, `drydock stop && drydock create`.

## Security posture

- Values never appear in audit logs — only names, hashes, or scope descriptors.
- Plaintext tokens exist on disk only at `~/.drydock/secrets/<dock_id>/` (per-drydock) or `~/.drydock/daemon-secrets/` (Harbor); the daemon stores SHA-256 hashes of bearer tokens in SQLite, never plaintext.
- Secrets are never committed to git. `.gitignore` covers `~/.drydock/` by being outside the repo.
- Per-drydock isolation: each drydock sees only its own secrets via the bind-mount. Cross-drydock secret access requires an explicit lease through the capability broker (audit-recorded, policy-gated).
