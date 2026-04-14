# Secrets — roadmap

Drydock's secrets handling evolves through four phases. Each phase preserves the CLI surface from the previous one; only the backend changes. This doc captures the trajectory so the initial implementation doesn't paint v2+ into a corner.

## The principle

Thin command surface, backend evolves. Users write `ws secret set myapp anthropic_api_key` from Phase 1 through Phase 4; the plumbing behind it swaps without CLI churn.

## Phase 1 — file-backed (v0.1.x, shipped as of v0.1.0 + this iteration)

Backend: `~/.drydock/secrets/<ws_id>/<key-name>` files with mode 400. Same mechanism drydock always had; now wrapped behind CLI commands.

```
ws secret set <workspace> <key-name>     # stdin, write mode 400
ws secret list <workspace>               # keys only, never values
ws secret rm  <workspace> <key-name>
ws secret push <workspace> --to <host>   # rsync scoped-dir to remote drydock host
```

Invariants:

- Values enter via stdin only. Never argv, never env var, never temp file.
- `list` shows key names + mode + size + mtime, never content.
- `push` is scoped per workspace; does not copy all of `~/.drydock/secrets/`.
- No `get`/`show` command that echoes values. Users who need the value `cat` the file themselves.
- No log line anywhere echoes content.

## Phase 2 — daemon-mediated broker (v2 daemon era)

Backend: `wsd` daemon holds secrets in memory + encrypted rest-store; issues time-bounded lease files to containers. Same CLI; daemon replaces the file-backed store.

New capabilities the CLI gains:

- **Scoped delegation**: `ws secret set <parent-ws> <key> --delegatable-to <child-ws>` — parent desk declares which secrets it may hand to spawned children. Enforces narrowness (child can request a subset, never more).
- **Rotation**: daemon rotates secrets on a cadence; containers receive fresh leases automatically.
- **Immediate revocation**: `ws secret rm` → daemon kills leases; running containers lose access next request.
- **Scoping per principal**: in multi-user era (V3+), secrets are scoped to owner; one user's secrets don't leak to another user's desks.

Migration: `ws secret migrate --to broker` copies file-backed secrets into the daemon's store, updates mounts from bind-readonly-file to daemon-lease. One-time.

## Phase 3 — pluggable external brokers

Backend: pluggable via flag or per-workspace YAML.

```yaml
# in a project YAML
secrets_backend: 1password   # or "vault", "aws-secrets", "gcp-secret-manager", "files"
secrets_source: "op://Private/MyappKeys"
```

Drydock resolves secrets from the external source at create time; the daemon mediates the lease semantics; local files become a cache at most.

CLI stays stable; the `--backend` flag is the escape hatch for one-off overrides.

## Phase 4 — capability broker (V4+)

Backend: the same daemon grown into a generalized capability broker. Secrets are one type of capability:

- **API secrets** (ANTHROPIC_API_KEY, GitHub tokens)
- **Cloud credentials** (S3 access keys scoped to specific buckets, bounded TTL)
- **Compute quotas** (CPU-hours on a specific host, wall-clock budget for a desk)
- **Network reachability tokens** (may-call-a-peer-desk, may-reach-a-specific-cross-workspace-endpoint)
- **Storage mounts** (may-mount `s3://bucket/path`, readonly)

All issued via the same lease mechanism. The CLI becomes `ws cap` or `ws lease` (or stays as `ws secret` for backward compat + grows sibling commands). Command surface remains user-friendly; the broker's policy graph decides what's allowed.

## Design invariants across all phases

- **No value ever enters via argv or env var.** Stdin or file only.
- **No value is echoed.** Any output is key-names, metadata, hashes, never content.
- **No accidental argv leakage via `ps`.** Stdin-fed processes don't expose secrets in process listings.
- **Permission model is explicit.** File mode 400 in Phase 1; capability-graph in Phase 2+.
- **`list` is safe for scripting.** Produces machine-readable JSON by default when not a TTY.
- **Migration between phases is a one-time operation**, not a rewrite.

## What this roadmap is NOT

- It is not a promise that Phase 4 ships. Each phase is buildable independently; phase N is built when phase N-1 friction surfaces real need.
- It is not a specific pluggable-plugin spec. That's designed when Phase 3 starts.
- It is not a justification for over-engineering Phase 1. Today's file-backed version is the whole system, not a toy precursor.

## Where the line moves

The meaningful architectural threshold is Phase 2 — the move from file-backed to daemon-mediated. That's when secrets become policy-enforced rather than filesystem-enforced. It's the same threshold as "drydock becomes infrastructure" per the v2-scope doc.
