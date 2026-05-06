# Harbor bootstrap — provisioning a fresh Linux box as a Drydock Harbor

Captures everything a fresh Linux box (Hetzner, EC2, bare metal, anything that can run Docker) needs before `drydock create` will work. Tested on Ubuntu 24.04 LTS; Debian-family should be straightforward; other distros need adapter steps.

A **Harbor** is the machine that runs `drydock daemon` and hosts drydocks (see [../design/vocabulary.md](../design/vocabulary.md)). This doc is about standing up that Linux host so it can become a Harbor. Low-level "host" terminology (linux host, docker host, host uid, `drydock host init/check`) stays — those are OS-level concepts. "Harbor" is the product-level role the host plays.

Everything deterministic is scripted at [`scripts/bootstrap-linux-host.sh`](../../scripts/bootstrap-linux-host.sh). One script covers apt deps, drydock install, state dirs, and systemd unit installation — no second script to remember. Auth (Tailscale, GitHub, Claude) is interactive *unless* you pre-inject credentials via env vars (see "Unattended bootstrap" below).

### Unattended bootstrap (EC2 user-data, Terraform, etc.)

Set env vars before running the script and auth happens silently:

```bash
TAILSCALE_AUTHKEY=tskey-xxx \
HARBOR_HOSTNAME=my-aws-harbor \
GH_TOKEN=ghp_xxx \
    bash <(curl -fsSL https://raw.githubusercontent.com/stevefan/drydock/main/scripts/bootstrap-linux-host.sh)
```

| Env var | Effect |
|---|---|
| `TAILSCALE_AUTHKEY` | Runs `tailscale up --authkey=… --hostname=$HARBOR_HOSTNAME --ssh` non-interactively |
| `HARBOR_HOSTNAME` | Tailnet hostname for this Harbor (defaults to `hostname -s`) |
| `GH_TOKEN` | `gh` respects natively; skip the interactive `gh auth login` |

With all three set, a fresh EC2 instance reaches "Harbor ready for `drydock create`" in one user-data block.

---

## What you bring

- A Linux box reachable over SSH as `root` (or a sudoer)
- Your SSH public key authorized on it
- A Tailscale account
- A GitHub account (HTTPS access via `gh` for cloning private repos)
- A claude.ai subscription account (for in-drydock Claude Code remote-control / interactive sessions by Workers)

## What the bootstrap script does

Idempotent. Safe to re-run.

1. Installs apt packages: `docker-ce` + buildx, `tailscale`, `python3` + `pipx`, `git`, `curl`, `gh`, `nodejs`/`npm`.
2. Installs `@devcontainers/cli` globally via npm.
3. Clones drydock into `/root/drydock` and `pipx install --editable` it. `ws` lands at `/root/.local/bin/ws`.
4. Runs `drydock host init` — creates state directories (`/root/.drydock/{projects,secrets,worktrees,overlays,daemon-secrets,logs}`, `secrets` and `daemon-secrets` at `0700`), `/var/log/drydock/`, and a `/root/.gitconfig` stub (the base devcontainer template bind-mounts `${HOME}/.gitconfig` and Linux docker hard-fails on missing mount sources).
5. Installs systemd units via `scripts/install-linux-services.sh` — `drydock.service` (daemon) and `drydock-desks.service` (resume-on-boot). Enables both.
6. Starts `drydock.service`.
7. If `TAILSCALE_AUTHKEY` is set, runs `tailscale up` with `--ssh` non-interactively.

After the script, `which ws && ws --version` works and `drydock daemon status` reports healthy. `drydock host check` will flag any remaining gaps (e.g. Tailscale not joined, gh not authed).

## Interactive steps (one-time per Harbor)

```bash
# Tailscale — joins the tailnet
tailscale up --hostname=<box-name>
# (device flow — opens a URL; do it on your phone or any browser)
# Once the tailnet ACL permits it, SSH into any drydock with:
#   tailscale ssh node@<drydock>

# GitHub — for private-repo clones
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git    # configures git's credential helper

# Claude Code (after your first drydock create) — persists to the
# `claude-code-config` named volume; subsequent drydocks on this Harbor inherit
docker exec -u node <container-id-of-any-drydock> claude /login
# (device flow)
```

Why three separate logins: Tailscale, GitHub, and Anthropic are independent identity providers. Claude on Mac auto-shares its keychain credentials with you locally; on a remote Harbor each gets a fresh device-flow auth.

## Optional: Claude Code auth (for claude remote-control = Remote Sessions UI)

The `claude remote-control` server (what surfaces a drydock's Worker in your claude.ai Remote Sessions sidebar) requires **full-scope claude.ai OAuth state**. `ANTHROPIC_API_KEY` doesn't satisfy it; `claude setup-token` tokens (inference-only) don't either. The only way to produce that state is running `claude auth login` on some machine with a TTY.

Good news: once you've done that on any machine (e.g. your Mac), you can **transplant the auth state to any Harbor** via two drydock secrets. `sync-claude-auth.sh` (in drydock-base v1.0.6+) picks them up at container startup, materializes them in the `claude-code-config` shared volume, and marks `/workspace` as trusted.

**Important: newer Claude Code versions no longer store credentials on disk.** On macOS, Claude Code now stores OAuth credentials exclusively in the macOS Keychain (service name `Claude Code-credentials`). The file `~/.claude/.credentials.json` no longer exists. Extract credentials from the keychain instead.

The account-state file `~/.claude.json` (contains `organizationUuid`, selected model, etc.) still exists on disk and is still needed.

**One-time per Harbor (and re-run whenever your token refreshes):**

```bash
# On your Mac — extract credentials from macOS Keychain (always has the freshest tokens):
security find-generic-password -s "Claude Code-credentials" -w | drydock secret set <drydock> claude_credentials

# Account state still lives on disk:
drydock secret set <drydock> claude_account_state < ~/.claude.json

# Push both to the remote Harbor:
drydock secret push <drydock> --to root@<host>
```

**Token refresh — empirical result (2026-04-17):** the container's `claude remote-control` refreshes tokens **in memory only** — the process stays alive past the file's `expiresAt`, but `.credentials.json` is NOT updated on disk. File-based consumers (other drydocks via `RequestCapability`, any process reading `/run/secrets/claude_credentials`) get stale tokens after ~8 hours.

**Designed refresh mechanism:** periodic re-extraction from Mac keychain:
```bash
security find-generic-password -s "Claude Code-credentials" -w | drydock secret set <drydock> claude_credentials
drydock secret push <drydock> --to root@<host>
```
Run every 6 hours via Mac launchd, or manually when remote-control auth errors surface. The Mac is the credential source of truth for file-based consumers. Remote-control itself self-sustains and doesn't need this.

**Why not `claude auth login` inside the container?** `claude auth login` requires Ink raw-mode TTY support. Most SSH and `docker exec` PTY combinations do not satisfy this — the login flow hangs or renders garbled output. The keychain-extraction pattern above bypasses this limitation entirely.

After the first container restart (or `drydock create --force`) the drydock registers with claude.ai. The `claude-code-config` volume is shared across all drydocks on a Harbor, so any sibling drydock gets the auth for free.

**Per-drydock?** Technically the secrets are per-drydock in drydock's store, but the materialized auth lives in the Harbor-shared `claude-code-config` volume. So you only *need* to do it once per Harbor — on any drydock — and all drydocks on that Harbor share the login. Putting it on the first drydock you create each Harbor is the simplest mental model.

**Without this:** `claude remote-control` loops with "must be logged in." Other claude usage inside the drydock (scripted `claude --print`, smart operator via `--bare`) still works via `ANTHROPIC_API_KEY` independently.

## Optional: Tailscale admin API token

For `drydock tailnet prune` (cleanup of orphan tailnet device records) and the eventual v2 daemon-side device cleanup on `drydock destroy`:

```bash
# Generate at https://login.tailscale.com → Settings → Keys → Generate API access token
# Required scope: devices

echo -n "<token>" > /root/.drydock/daemon-secrets/tailscale_admin_token
echo -n "<your-tailnet-name>" > /root/.drydock/daemon-secrets/tailscale_tailnet
chmod 400 /root/.drydock/daemon-secrets/*
```

See [../design/tailnet-identity.md](../design/tailnet-identity.md) for the full lifecycle story.

## Tailscale SSH into drydocks

Once your tailnet ACL permits SSH, `tailscale ssh node@<drydock>` is the canonical way to remote into a running drydock. Notes from the auction-crawl deployment (2026-04-14 through 2026-04-16):

- **`safe.directory = *`** — drydock-base v1.0.8 sets `git config --global safe.directory '*'` inside drydocks. Without this, git refuses to operate in `/workspace` when the UID of the SSH session doesn't match the repo owner. The wildcard is acceptable in single-tenant agent containers; do not carry this pattern to shared hosts.
- **ACL `check` vs `accept`** — Tailscale SSH ACLs support `action: "check"` (requires the connecting user to re-authenticate via the admin panel) and `action: "accept"` (trusts the tailnet identity directly). For personal Harbors, `accept` is simpler; for shared hosts or sensitive drydocks, `check` adds an interactive gate. Choose based on your threat model.
- **PTY quality** — Tailscale SSH provides a better PTY than raw `docker exec -it`, which matters for tools like `claude` that use Ink/raw-mode rendering. If you need interactive Claude sessions inside a drydock, prefer Tailscale SSH over docker exec.

## What's per-project (NOT Harbor bootstrap)

Once the Harbor is bootstrapped, each project that wants a drydock needs:

```bash
# Clone the project repo (via gh-mediated HTTPS)
git clone https://github.com/<you>/<project>.git /root/src/<project>

# Drop a project YAML
cat > /root/.drydock/projects/<project>.yaml <<EOF
repo_path: /root/src/<project>
workspace_subdir: <subdir-if-monorepo>
tailscale_hostname: <project>
firewall_extra_domains:
  - <hosts the drydock legitimately needs>
extra_mounts:
  - source=ws-<project>-data,target=/workspace/data,type=volume
EOF

# Push secrets (uid 1000 ownership is automatic when ws runs as root)
echo -n "$ANTHROPIC_API_KEY" | drydock secret set <project> anthropic_api_key
# ...other secrets

# Provision
drydock create <project>
```

If the project has source-of-truth state on another Harbor (e.g. a SQLite DB on your Mac), seed the named volume before `drydock create` — see `~/Notebooks/ops-personal/projects/Auction Crawl.md` (or your equivalent) for an example using `docker run --rm -v <vol>:/dst alpine cp ...`.

## What drydock could automate (but doesn't yet)

| Step | Why drydock can't do it (yet) | Possible future affordance |
|---|---|---|
| Provision the VM | Out of scope — IaC layer (Terraform, OpenTofu, manual). Drydock is a fabric, not a cloud broker. | Never inside drydock proper. A sibling `fleet/` repo could thinly wrap Hetzner / fly / etc. |
| Install host deps | Chicken-and-egg — drydock has to be installed first. | This bootstrap script lives in the drydock repo as the install vector. `curl ... \| bash` it. |
| `drydock host init` | Done — bootstrap invokes it. Creates state dirs + gitconfig stub. Idempotent, safe to re-run. |
| `drydock host check` | Done — returns a structured "what's missing" report. Run after bootstrap or any time as a preflight. |
| Tailscale device API token | Must be generated in the tailnet admin UI (no programmatic flow today). | Could prompt with the URL when the token file is missing; eventually OAuth client tokens auto-rotate. |
| Claude `/login` | Interactive device flow inside the container. | Could prompt "Run `claude /login` in this container before drydock exec --interactive"; not much more drydock can add. |

The honest split: drydock owns drydock lifecycle. Harbor setup is one layer below. The bootstrap script bridges that gap, and `drydock host init` / `drydock host check` would be the modest drydock-side extensions worth landing in v1.x or v2.

## Reference deployment

Harbor `drydock-hillsboro` — Hetzner Cloud, Hillsboro OR. Documented at `~/Notebooks/ops-personal/tech/Drydock Fleet.md`. Used for the auction-crawl daily scraper. End-to-end deployment story in `~/Notebooks/ops-personal/projects/Auction Crawl.md`.

Bootstrap-to-first-drydock on a fresh CX22-class box: ~15 minutes, mostly waiting on docker/tailscale apt installs.
