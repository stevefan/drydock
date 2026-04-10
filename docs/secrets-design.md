# Secrets Management Design

## Principle

Secrets are mounted as files at a well-known path, not injected as environment variables. This gives a single convention that works identically across local Docker, cloud container services, and Kubernetes.

## Convention

Secrets are mounted read-only at `/run/secrets/` inside the workspace container. Each secret is a plain file named after the key (lowercase, underscored):

```
/run/secrets/
  tailscale_authkey
  anthropic_api_key
  openai_api_key
  supabase_service_role_key
  ...
```

Scripts and applications read secrets from this path:

```bash
TAILSCALE_AUTHKEY=$(cat /run/secrets/tailscale_authkey)
```

## Sources by environment

| Environment | Secret source | Mount mechanism |
|---|---|---|
| **Local Docker** | Host directory (e.g. `/srv/secrets/<workspace>`) | `docker run -v /srv/secrets/ws_foo:/run/secrets:ro` |
| **Docker Compose** | `secrets:` top-level key | Native Docker secrets (tmpfs mount) |
| **AWS ECS** | AWS Secrets Manager | `secrets` in task definition → file mount |
| **Google Cloud Run** | GCP Secret Manager | Secret volume mount |
| **Kubernetes** | K8s Secrets | Secret volume mount |
| **1Password CLI** | `op read` | Populate host dir before container start |

## Workspace integration

### Profile declares required secrets

Each workspace profile lists the secrets it needs:

```yaml
permissions:
  profile: dev-readwrite
  secrets:
    required:
      - tailscale_authkey
      - anthropic_api_key
    optional:
      - openai_api_key
```

### Orchestrator provisions at create time

When `ws create` runs, the orchestrator:
1. Reads the workspace profile's secret requirements
2. Resolves secrets from the configured source
3. Populates the host secrets directory (local) or configures the cloud mount
4. Mounts `/run/secrets` into the container read-only

### Validation

On workspace start, a healthcheck can verify required secrets are present:

```bash
for secret in tailscale_authkey anthropic_api_key; do
  if [ ! -f "/run/secrets/$secret" ]; then
    echo "ERROR: Missing required secret: $secret"
    exit 1
  fi
done
```

## Local development setup

For v1, the host secrets directory is a plain folder:

```
/srv/secrets/
  shared/              # secrets available to all workspaces
    tailscale_authkey
    anthropic_api_key
  ws_payments_001/     # workspace-specific overrides
    supabase_service_role_key
```

The orchestrator merges shared + workspace-specific secrets into a single mount. Workspace-specific files override shared ones.

## Migration from .env.local

The current `.env.local` grep-scanning in `start-tailscale.sh` should be replaced with:

```bash
# Before (scanning .env files)
TAILSCALE_AUTHKEY=$(grep -s '^TAILSCALE_AUTHKEY=' /workspace/*/.env.local | cut -d'=' -f2-)

# After (mounted secret)
TAILSCALE_AUTHKEY=$(cat /run/secrets/tailscale_authkey 2>/dev/null || echo "")
```

## Security considerations

- Secret files should be owned by root, readable by the container user (mode 0440)
- Never log secret values — log only whether a secret was found
- Host secrets directory should have restricted permissions (mode 0700)
- Secrets are never committed to git — the `.gitignore` excludes all `.env*` files and the host path is outside the repo
- Cloud secret mounts use tmpfs (in-memory) by default — no disk persistence

## Open questions

- **Rotation:** How to handle secret rotation in running workspaces? Restart required, or file-watch?
- **Shared vs isolated:** Should workspaces share a secrets mount, or always get their own copy?
- **Vault integration:** Worth supporting HashiCorp Vault or similar for teams, or overkill for v1?
