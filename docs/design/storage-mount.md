# STORAGE_MOUNT

Scoped cloud storage credentials as capability leases. A worker inside a drydock requests `RequestCapability(type=STORAGE_MOUNT, scope={bucket, prefix, mode})`; the daemon calls `sts:AssumeRole` against a Harbor-held IAM role with an **inline session policy** that narrows S3 access to exactly that bucket/prefix/mode; the resulting temporary credentials materialize as four `aws_*` files in the caller's `/run/secrets/`.

Vocabulary: [vocabulary.md](vocabulary.md). Lease primitive: [capability-broker.md](capability-broker.md). Narrowness: [narrowness.md](narrowness.md).

## Why leases, not static creds

The alternative — long-lived AWS access keys mounted into every drydock — fails three ways:

1. **Blast radius.** A leaked worker cred reads/writes everything the Harbor can.
2. **Rotation.** Static creds rot; nothing refreshes them automatically.
3. **Narrowness.** A worker that needs `s3://lab/scraped/*` shouldn't be able to touch `s3://lab/admin/`.

Session policies on AWS STS solve all three. Creds expire (default 4h, matches `drydock-agent`'s max-session-duration). Scope is inline-encoded per-request. Leaks are time-bounded.

## Architecture

```
Harbor (.aws/credentials)                   drydock (/run/secrets/)
    │                                            │
    │  drydock-runner (long-lived IAM user)      │
    ▼                                            │
 [ wsd.toml: storage.backend = "sts",            │
            role_arn,                            │
            source_profile = drydock-runner ]    │
    │                                            │
    │ RequestCapability(STORAGE_MOUNT,           │
    │   scope={bucket, prefix, mode})◄───────────┤ drydock-rpc
    ▼                                            │
 StsAssumeRoleBackend.mint()                     │
   └─ aws sts assume-role --profile drydock-runner
      --role-arn  <drydock-agent>
      --policy    <session-policy JSON>
      --role-session-name drydock-<desk_id>
      --duration-seconds 14400
    │                                            │
    ▼                                            │
 daemon writes ~/.drydock/secrets/<caller>/       │
   ├─ aws_access_key_id    (ASIA...)              │
   ├─ aws_secret_access_key                       │
   ├─ aws_session_token    (~1KB)                 │
   └─ aws_session_expiration  (ISO8601)           │
    │                                            │
    │  (bind-mount surfaces them immediately)    │
    ▼                                            ▼
   ... visible to worker as /run/secrets/aws_*
```

## Scope shape

```python
{"bucket": "lab-data", "prefix": "scraped", "mode": "ro" | "rw"}
```

`bucket`: S3 bucket name (`_BUCKET_RE = ^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$`).
`prefix`: path within the bucket; empty means whole bucket. Trailing `/` stripped.
`mode`:
- `ro` — `s3:GetObject` + `s3:ListBucket`
- `rw` — additionally `s3:PutObject`, `s3:DeleteObject`

The session policy renders via `build_session_policy()` in `src/drydock/core/storage.py`. Two statements — one for object-level actions on `arn:aws:s3:::<bucket>/<prefix>/*`, one for `ListBucket` on the bucket with a `s3:prefix` condition narrowing to the prefix.

Additive scope fields (new modes, new action sets) go in without RPC changes.

## Backends

`StorageBackend` Protocol (`src/drydock/core/storage.py`):

```python
def mint(*, desk_id, bucket, prefix, mode) -> StorageCredential
```

Two concrete implementations ship:

| Backend | Used for | Requires |
|---|---|---|
| `StsAssumeRoleBackend` | Real AWS | `aws` CLI on Harbor, `drydock-runner` profile, `drydock-agent` role ARN |
| `StubStorageBackend` | Tests, dev Harbors without AWS wired up | nothing |

Configured via `~/.drydock/wsd.toml`:

```toml
[storage]
backend = "sts"
role_arn = "arn:aws:iam::047535447308:role/drydock-agent"
source_profile = "drydock-runner"
session_duration_seconds = 14400
```

Missing `[storage]` section → STORAGE_MOUNT leases reject with `storage_backend_not_configured` and a `fix:` pointing at the TOML knob. Failing at startup (unknown backend name) beats failing mid-RPC where the caller may be non-interactive.

## Harbor-side IAM scaffolding

`scripts/aws/` provisions the identity stack. See [operations/harbor-bootstrap.md](../operations/harbor-bootstrap.md).

- **`drydock-runner`** (IAM user) — long-lived keys live on the Harbor at `~/.aws/credentials`. Single purpose: `sts:AssumeRole` on `drydock-agent`.
- **`drydock-agent`** (IAM role) — admin-in-sandbox. `AdministratorAccess` attached, `drydock-boundary` as permission boundary (denies self-elevation, expensive EC2, account closure, Route 53 domain buy, …), 4h max session. Trust policy requires `sts:RoleSessionName` starting `drydock-*` so CloudTrail filters cleanly.
- **`drydock-boundary`** — the permission ceiling. Session policies issued per-lease narrow BELOW this, never above.

`aws sts assume-role --profile drydock` (configured profile that auto-assumes) is the reference for "what the Harbor can ultimately do." STORAGE_MOUNT leases narrow below it per-request.

## Materialization

Same-drydock, same scheme as SECRET leases (see [capability-broker.md](capability-broker.md)):

`_materialize_storage_credentials()` writes four files to `~/.drydock/secrets/<caller_desk_id>/`:

- `aws_access_key_id`
- `aws_secret_access_key`
- `aws_session_token`
- `aws_session_expiration` (ISO8601, daemon-written for worker-side freshness polling)

Each file mode 0400, chowned to container uid 1000 (the `node` user in `drydock-base`). The overlay already bind-mounts the drydock's secret dir read-only at `/run/secrets/`, so the files are immediately visible inside.

Worker reads them directly — no `aws` CLI required inside the drydock:

```python
import boto3
s3 = boto3.client(
    "s3",
    aws_access_key_id=open("/run/secrets/aws_access_key_id").read().strip(),
    aws_secret_access_key=open("/run/secrets/aws_secret_access_key").read().strip(),
    aws_session_token=open("/run/secrets/aws_session_token").read().strip(),
    region_name="us-west-2",
)
s3.put_object(Bucket="lab-data", Key="scraped/item.json", Body=b"...")
```

(Or `aws-cli`, `rclone`, any SDK — the creds are standard AWS session creds.)

## Single-active-lease-per-drydock

Each drydock has exactly one active STORAGE_MOUNT lease at a time. Issuing a new one supersedes any prior active lease: the daemon auto-revokes prior lease(s) before issuing the new one and emits `lease.released` with `reason: superseded_by_new_storage_lease`. Rationale: the four `aws_*` files overwrite in place; ref-counting on release would be ambiguous about which lease owns the files.

`ReleaseCapability` on the last active STORAGE_MOUNT lease removes the four `aws_*` files — the worker's AWS SDK loses access immediately (beyond in-flight requests already holding creds in memory).

## Narrowness

Per-drydock: `request_storage_leases` capability grants the coarse ability to request storage leases at all.

Per-bucket/prefix: `delegatable_storage_scopes` in the project YAML constrains WHICH buckets/prefixes. Format: `"s3://bucket/prefix/*"` (ro-only) or `"rw:s3://bucket/prefix/*"` (rw permitted). Empty list = no narrowness declared = capability gate alone governs (default-permissive-when-empty for back-compat; see [narrowness.md](narrowness.md)).

Scoring: a request matches a granted scope if the granted scope's bucket equals the requested bucket AND the granted prefix is a path-prefix of the requested prefix AND (for `rw` requests) the granted scope has the `rw:` marker. Path-prefix matching is segment-aware — `data/` matches `data/foo/` but not `data2/`.

## Firewall interplay

Scoped creds say what the worker MAY do in AWS. The drydock's default-deny firewall says which hosts the worker CAN reach. Both must permit an operation — defense in depth.

S3 virtual-host addressing (`<bucket>.s3.<region>.amazonaws.com`) means each bucket is a distinct hostname. `firewall_extra_domains` must include each specific bucket (or `s3.<region>.amazonaws.com` for path-style addressing — AWS SDK defaults to virtual-host). A known operational follow-up is wildcard/CIDR support for AWS IP ranges; today each bucket's host needs to land in the allowlist with a DNS refresh cycle.

## Declarative storage_mounts (Phase C, shipped 2026-04-18)

Project YAML declares S3 mounts directly; the daemon handles lease + `s3fs` mount at drydock start:

```yaml
storage_mounts:
  - source: s3://my-bucket/data
    target: /mnt/data
    mode: rw          # ro (default) or rw
    region: us-west-2 # optional, default us-west-2
```

One entry expands at YAML-load time (`expand_storage_mounts` in `project_config.py`) into:

- `request_storage_leases` capability (added if absent)
- `s3://bucket/prefix/*` (or `rw:...`) appended to `delegatable_storage_scopes`
- `<region>:AMAZON` appended to `firewall_aws_ip_ranges`

User-declared values on those fields are preserved and deduped.

### Overlay wiring

`OverlayConfig.storage_mounts` emits:

- `STORAGE_MOUNTS_JSON` in `containerEnv` — JSON-encoded list consumed by `setup-storage-mounts.sh` inside the container.
- Three `runArgs` required for FUSE: `--cap-add=SYS_ADMIN`, `--device=/dev/fuse`, `--security-opt=apparmor=unconfined`. The last one exists because Ubuntu Harbors run docker with a default AppArmor profile that blocks `mount()` even when the SYS_ADMIN cap is present — surfaced during the first Hetzner smoke test.

### Lifecycle

`wsd` runs `setup-storage-mounts.sh` via `docker exec -u node` after `devcontainer up` returns (both on `CreateDesk` and `ResumeDesk`). This works regardless of what the project's `devcontainer.json` does at `postStartCommand` — drydock-base's script is daemon-triggered. Mount survives the life of the container; a stop → create recreates it.

The script parses `STORAGE_MOUNTS_JSON`, requests one `STORAGE_MOUNT` lease per entry, and `s3fs bucket:/prefix target …` with creds from `/run/secrets/aws_*`. Errors are logged to `/tmp/storage-mounts.log` but don't fail the drydock — a misdeclared mount leaves the rest running.

### Cred refresh (Phase C.1)

STS sessions expire after 4h (`aws_session_expiration`). `s3fs` reads creds once at mount and holds them in memory — after expiry, requests start 403'ing and the mount silently dies.

`refresh-storage-mounts.sh` runs as a backgrounded daemon spawned at the end of `setup-storage-mounts.sh` (idempotent via `/tmp/storage-mounts-refresh.pid`). Each successful mount is recorded to `/tmp/storage-mounts-state.json`. The daemon reads `/run/secrets/aws_session_expiration`, sleeps until `LEAD_SECS` (default 600s) before expiry, then for each entry: `RequestCapability STORAGE_MOUNT` (mints fresh STS creds, overwriting `/run/secrets/aws_*`), `fusermount -u` the target, and re-run `s3fs` with the fresh env creds. Disruption is bounded to ~1s per mount per refresh cycle. The loop then reads the new expiration and sleeps again.

## Other follow-ups

- `COMPUTE_QUOTA` and `NETWORK_REACH` capability types are enum-reserved for the same lease-issuance pattern applied to compute grants and fine-grained network reach.
