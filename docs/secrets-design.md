# Secrets Management Design

## Principle

Secrets are mounted as files at a well-known path (`/run/secrets/`), not injected as environment variables. This gives a single convention that works across local Docker, Docker Compose, AWS ECS, Google Cloud Run, Kubernetes, and 1Password-bridged workflows.

**Scope:** per-workspace. Not per-project, not shared across workspaces. A compromise of one workspace must not grant access to another workspace's credentials.

## End state (v2 — daemon as broker)

Workspaces do not hold long-lived secrets. When a workspace needs `ANTHROPIC_API_KEY`, it requests a credential lease from the `wsd` daemon. The daemon:

1. Checks the workspace's declared entitlements against the policy graph
2. Fetches the real secret from its configured backend (1Password, vault, cloud secret manager, or a trust-anchor directory)
3. Issues a time-bounded, scoped credential — possibly the real key, possibly a short-lived derived credential
4. Writes it to the workspace's mounted secrets directory (tmpfs, readonly)
5. Rotates it before expiry; revokes it on workspace destroy

The workspace never learns the broker's real keys. The broker is the only thing that holds them. Rotation, revocation, and audit all happen at the broker.

## v1 stepping stone (today)

The daemon doesn't exist yet. V1 implements the convention without the broker:

- **Container path:** `/run/secrets/` (readonly bind mount)
- **Host path:** `~/.drydock/secrets/<workspace_id>/` (per-workspace directory on the host)
- **Populate mechanism:** the operator populates the per-workspace directory before `ws create` — by hand, by shell script, or by 1Password CLI (`op read`)
- **Revocation:** operator removes or rotates files in `~/.drydock/secrets/<workspace_id>/` and restarts the workspace

Each secret is a plain file named after the key (lowercase, underscored):

```
/run/secrets/
  tailscale_authkey
  anthropic_api_key
  openai_api_key
  ...
```

Scripts and applications read secrets from this path:

```bash
TAILSCALE_AUTHKEY=$(cat /run/secrets/tailscale_authkey)
```

## Per-workspace directory convention

V1 mounts `~/.drydock/secrets/<workspace_id>/` readonly at `/run/secrets/`. The workspace id is deterministic from `ws create` args (`ws_<project>_<name_slug>`), so you can set up the directory before running create:

```bash
mkdir -p ~/.drydock/secrets/ws_microfoundry_microfoundry
cp ~/.local/secrets/tailscale_authkey ~/.drydock/secrets/ws_microfoundry_microfoundry/
cp ~/.local/secrets/anthropic_api_key ~/.drydock/secrets/ws_microfoundry_microfoundry/
ws create microfoundry
```

Or use a setup script that's part of your project workflow (`op read` for each declared secret, written to the per-workspace directory with mode 0440).

If the directory doesn't exist, Docker's bind mount creates an empty one. The workspace will start but `/run/secrets/` will be empty — any script that `cat`s a secret file will see an empty string.

## Sources by environment

The operator's mechanism for populating `~/.drydock/secrets/<workspace_id>/` can vary. Drydock doesn't prescribe it; it only prescribes where the container finds secrets.

| Environment | Populate mechanism |
|---|---|
| **Local laptop** | `op read` from 1Password → file; or keep a trust-anchor dir on the host and copy subsets per workspace |
| **Docker Compose** | `secrets:` top-level key (tmpfs mount), populated from host files |
| **AWS ECS** | AWS Secrets Manager → task-definition-level file mount |
| **Google Cloud Run** | GCP Secret Manager → volume mount |
| **Kubernetes** | K8s Secrets → volume mount |

The v2 daemon takes over the populate-at-create-time mechanism, makes it credential-lease-based instead of file-copy-based, and adds rotation + revocation.

## Profile declares required secrets

A project's YAML config declares which secrets its workspaces need:

```yaml
# drydock/projects/myproject.yaml
permissions:
  secrets:
    required:
      - tailscale_authkey
      - anthropic_api_key
    optional:
      - openai_api_key
```

(Not yet wired into the v1 overlay generator; per-project YAML currently only covers networking + firewall + identity. The secrets declaration is a v2 daemon concern — the daemon uses it to decide what credentials to lease to a spawning child.)

## Validation

On workspace start, a healthcheck verifies required secrets are present:

```bash
for secret in tailscale_authkey anthropic_api_key; do
  if [ ! -f "/run/secrets/$secret" ]; then
    echo "ERROR: Missing required secret: $secret"
    exit 1
  fi
done
```

In v2 the daemon does this at lease time instead of at container-start time.

## Security considerations

- Secret files should be readable by the container user (mode 0440) and owned by a user the container can map to
- Never log secret values — log only whether a secret was found
- Host secrets directory (`~/.drydock/secrets/`) should have restricted permissions (mode 0700)
- Secrets are never committed to git — `.gitignore` excludes all `.env*` and `~/.drydock/secrets/` is outside the repo
- Cloud secret mounts should use tmpfs (in-memory) by default — no disk persistence
- V2 daemon leases are time-bounded; rotate and revoke automatically

## Why per-workspace scoping from v1

Per-project scoping (one shared secrets dir for all workspaces of a project) was considered and rejected:

- **Blast radius:** compromise of one workspace grants access to the credentials of every other workspace of the same project. Per-workspace isolates that.
- **Revocation granularity:** with per-project scoping you can't revoke one workspace's secrets without affecting siblings. Per-workspace lets you destroy one workspace's credentials in isolation.
- **Forward compatibility:** v2 daemon leases are intrinsically per-workspace. Starting per-workspace in v1 means the migration path is "replace the directory populate mechanism with a lease mechanism," not "rearchitect the mount shape."

## Decided questions

- **Per-workspace vs per-project scoping:** per-workspace. Decided 2026-04-12.
- **Rotation in running workspaces:** v1 requires restart. V2 daemon supports live rotation by rewriting the mounted tmpfs file and optionally signaling the workspace to re-read.
- **Vault / 1Password integration:** operator's choice in v1 (populate mechanism is not Drydock's concern). V2 daemon has pluggable backends.
