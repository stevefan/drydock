# Secrets Management Design

## Principle

Secrets are mounted as files at a well-known path (`/run/secrets/`), not injected as environment variables. This gives a single convention that works across local Docker, Docker Compose, AWS ECS, Google Cloud Run, Kubernetes, and 1Password-bridged workflows.

**Scope:** per-drydock. Not per-project, not shared across drydocks. A compromise of one drydock must not grant access to another drydock's credentials.

## End state (v2 — daemon as broker)

Drydocks do not hold long-lived secrets out-of-band. When a drydock needs `ANTHROPIC_API_KEY`, its Worker requests a capability lease from the `wsd` daemon on the Harbor. The daemon:

1. Checks the drydock's declared entitlements against the policy graph (narrowness pinned at `SpawnChild` time)
2. Fetches the real secret from its configured backend (file-backed by default; pluggable to 1Password, vault, cloud secret manager)
3. Materializes it into the drydock's mounted secrets directory under `/run/secrets/`
4. Revokes it on drydock destroy; audit-emits at issue and release

The drydock never learns the broker's real backend credentials. Scope is the key property: audit, narrowness, and per-drydock isolation all happen at the broker. V2 leases default to `expiry: None` (live until drydock destroy or explicit release) — time-bounded rotation is a V4 concern for cloud credentials that genuinely need it, not a V2 feature.

## v1 stepping stone (today)

The daemon doesn't exist yet. V1 implements the convention without the broker:

- **Container path:** `/run/secrets/` (readonly bind mount)
- **Host path:** `~/.drydock/secrets/<workspace_id>/` (per-drydock directory on the Harbor; `workspace_id` is the frozen code identifier)
- **Populate mechanism:** the operator populates the per-drydock directory before `ws create` — by hand, by shell script, or by 1Password CLI (`op read`)
- **Revocation:** operator removes or rotates files in `~/.drydock/secrets/<workspace_id>/` and restarts the drydock

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

## Per-drydock directory convention

V1 mounts `~/.drydock/secrets/<workspace_id>/` readonly at `/run/secrets/`. The workspace id is deterministic from the `ws create` name argument (`ws_<name_slug>` — dashes and spaces in the name become underscores), so you can set up the directory before running create:

```bash
mkdir -p ~/.drydock/secrets/ws_myapp
cp ~/.local/secrets/tailscale_authkey ~/.drydock/secrets/ws_myapp/
cp ~/.local/secrets/anthropic_api_key ~/.drydock/secrets/ws_myapp/
ws create myapp
```

Or use a setup script that's part of your project workflow (`op read` for each declared secret, written to the per-drydock directory with mode 0440).

If the directory doesn't exist, Docker's bind mount creates an empty one. The drydock will start but `/run/secrets/` will be empty — any script that `cat`s a secret file will see an empty string.

## Sources by environment

The operator's mechanism for populating `~/.drydock/secrets/<workspace_id>/` can vary. Drydock doesn't prescribe it; it only prescribes where the container finds secrets.

| Environment | Populate mechanism |
|---|---|
| **Local laptop (Harbor)** | `op read` from 1Password → file; or keep a trust-anchor dir on the Harbor and copy subsets per drydock |
| **Docker Compose** | `secrets:` top-level key (tmpfs mount), populated from host files |
| **AWS ECS** | AWS Secrets Manager → task-definition-level file mount |
| **Google Cloud Run** | GCP Secret Manager → volume mount |
| **Kubernetes** | K8s Secrets → volume mount |

The v2 daemon takes over the populate-at-create-time mechanism, makes it capability-lease-based instead of file-copy-based, and adds policy-enforced entitlement checks + revocation on destroy.

## Profile declares required secrets

A project's YAML config declares which secrets its drydocks need:

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

On drydock start, a healthcheck verifies required secrets are present:

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
- Harbor secrets directory (`~/.drydock/secrets/`) should have restricted permissions (mode 0700)
- Secrets are never committed to git — `.gitignore` excludes all `.env*` and `~/.drydock/secrets/` is outside the repo
- Cloud secret mounts should use tmpfs (in-memory) by default — no disk persistence
- V2 daemon leases revoke automatically on drydock destroy; finite-TTL + rotation is V4 (cloud credentials) not V2

## Why per-drydock scoping from v1

Per-project scoping (one shared secrets dir for all drydocks of a project) was considered and rejected:

- **Blast radius:** compromise of one drydock grants access to the credentials of every other drydock of the same project. Per-drydock isolates that.
- **Revocation granularity:** with per-project scoping you can't revoke one drydock's secrets without affecting siblings. Per-drydock lets you destroy one drydock's credentials in isolation.
- **Forward compatibility:** v2 daemon leases are intrinsically per-drydock. Starting per-drydock in v1 means the migration path is "replace the directory populate mechanism with a lease mechanism," not "rearchitect the mount shape."

## Decided questions

- **Per-drydock vs per-project scoping:** per-drydock. Decided 2026-04-12.
- **Rotation in running drydocks:** v1 requires restart. V2 ships without live rotation (leases are `expiry: None`). Live rotation by rewriting the mounted tmpfs file + signalling the drydock to re-read is reserved for V4 when cloud credentials make it load-bearing.
- **Vault / 1Password integration:** operator's choice in v1 (populate mechanism is not Drydock's concern). V2 daemon has pluggable backends.
