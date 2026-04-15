# Host bootstrap — provisioning a fresh Linux drydock host

Captures everything a fresh Linux box (Hetzner, EC2, bare metal, anything that can run Docker) needs before `ws create` will work. Tested on Ubuntu 24.04 LTS; Debian-family should be straightforward; other distros need adapter steps.

The deterministic parts are scripted at [`scripts/bootstrap-linux-host.sh`](../scripts/bootstrap-linux-host.sh). The auth steps (Tailscale, GitHub, Claude) are interactive device flows — one-time per host.

---

## What you bring

- A Linux box reachable over SSH as `root` (or a sudoer)
- Your SSH public key authorized on it
- A Tailscale account
- A GitHub account (HTTPS access via `gh` for cloning private repos)
- A claude.ai subscription account (for in-desk Claude Code remote-control / interactive sessions)

## What the bootstrap script does

Idempotent. Safe to re-run.

1. Installs apt packages: `docker-ce` + buildx, `tailscale`, `python3` + `pipx`, `git`, `curl`, `gh`, `nodejs`/`npm`.
2. Installs `@devcontainers/cli` globally via npm.
3. Creates drydock state directories with proper modes:
   - `/root/.drydock/{projects,secrets,worktrees,overlays,daemon-secrets,logs}` — `secrets` and `daemon-secrets` get `0700`.
   - `/var/log/drydock/` for cron output capture.
4. Touches `/root/.gitconfig` if missing — the base devcontainer template bind-mounts `${HOME}/.gitconfig` and Linux docker hard-fails on missing mount sources (see [historical context](../docs/_archive/) and the project memory `project_linux_host_papercuts.md` if available).
5. Clones drydock into `/root/drydock` and `pipx install --editable` it. `ws` lands at `/root/.local/bin/ws`.

After the script, `which ws && ws --version` should work.

## Interactive steps (one-time per host)

```bash
# Tailscale — joins the tailnet
tailscale up --hostname=<box-name>
# (device flow — opens a URL; do it on your phone or any browser)

# GitHub — for private-repo clones
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git    # configures git's credential helper

# Claude Code (after your first ws create) — persists to the
# `claude-code-config` named volume; subsequent desks on this host inherit
docker exec -u node <container-id-of-any-desk> claude /login
# (device flow)
```

Why three separate logins: Tailscale, GitHub, and Anthropic are independent identity providers. Claude on Mac auto-shares its keychain credentials with you locally; on a remote box each gets a fresh device-flow auth.

## Optional: Claude Code auth (for claude remote-control = Remote Sessions UI)

The `claude remote-control` server (what surfaces a desk in your claude.ai Remote Sessions sidebar) requires **full-scope claude.ai OAuth state**. `ANTHROPIC_API_KEY` doesn't satisfy it; `claude setup-token` tokens (inference-only) don't either. The only way to produce that state is running `claude auth login` on some machine with a TTY.

Good news: once you've done that on any machine (e.g. your Mac), you can **transplant the auth state to any drydock host** via two drydock secrets. `sync-claude-auth.sh` (in drydock-base v1.0.6+) picks them up at container startup, materializes them in the `claude-code-config` shared volume, and marks `/workspace` as trusted.

**One-time per host (and re-run whenever your token refreshes):**

```bash
# On the machine where you're logged in to Claude Code (e.g. your Mac):
ws secret set <desk> claude_credentials   < ~/.claude/.credentials.json
ws secret set <desk> claude_account_state < ~/.claude.json
ws secret push <desk> --to root@<host>
```

After the first container restart (or `ws create --force`) the desk registers with claude.ai. The `claude-code-config` volume is shared across all desks on a host, so any sibling desk gets the auth for free.

**Per-desk?** Technically the secrets are per-desk in drydock's store, but the materialized auth lives in the host-shared `claude-code-config` volume. So you only *need* to do it once per host — on any desk — and all desks on that host share the login. Putting it on the first desk you create each host is the simplest mental model.

**Without this:** `claude remote-control` loops with "must be logged in." Other claude usage inside the desk (scripted `claude --print`, smart operator via `--bare`) still works via `ANTHROPIC_API_KEY` independently.

## Optional: Tailscale admin API token

For `ws tailnet prune` (cleanup of orphan tailnet device records) and the eventual v2 daemon-side device cleanup on `ws destroy`:

```bash
# Generate at https://login.tailscale.com → Settings → Keys → Generate API access token
# Required scope: devices

echo -n "<token>" > /root/.drydock/daemon-secrets/tailscale_admin_token
echo -n "<your-tailnet-name>" > /root/.drydock/daemon-secrets/tailscale_tailnet
chmod 400 /root/.drydock/daemon-secrets/*
```

See [v2-design-tailnet-identity.md](v2-design-tailnet-identity.md) for the full lifecycle story.

## What's per-project (NOT host bootstrap)

Once the host is bootstrapped, each project that wants a desk needs:

```bash
# Clone the project repo (via gh-mediated HTTPS)
git clone https://github.com/<you>/<project>.git /root/src/<project>

# Drop a project YAML
cat > /root/.drydock/projects/<project>.yaml <<EOF
repo_path: /root/src/<project>
workspace_subdir: <subdir-if-monorepo>
tailscale_hostname: <project>
firewall_extra_domains:
  - <hosts the desk legitimately needs>
extra_mounts:
  - source=ws-<project>-data,target=/workspace/data,type=volume
EOF

# Push secrets (uid 1000 ownership is automatic when ws runs as root)
echo -n "$ANTHROPIC_API_KEY" | ws secret set <project> anthropic_api_key
# ...other secrets

# Provision
ws create <project>
```

If the project has source-of-truth state on another host (e.g. a SQLite DB on your Mac), seed the named volume before `ws create` — see `~/Notebooks/ops-personal/projects/Auction Crawl.md` (or your equivalent) for an example using `docker run --rm -v <vol>:/dst alpine cp ...`.

## What drydock could automate (but doesn't yet)

| Step | Why drydock can't do it (yet) | Possible future affordance |
|---|---|---|
| Provision the VM | Out of scope — IaC layer (Terraform, OpenTofu, manual). Drydock is a fabric, not a cloud broker. | Never inside drydock proper. A sibling `fleet/` repo could thinly wrap Hetzner / fly / etc. |
| Install host deps | Chicken-and-egg — drydock has to be installed first. | This bootstrap script lives in the drydock repo as the install vector. `curl ... \| bash` it. |
| `ws host init` | Could create state dirs, gitconfig stub, daemon-secrets dir post-install. | Worth a small addition: `ws host init` runs steps the bootstrap script does post-pipx-install. Idempotent, safe to re-run after a bad install. Closes the gap between "drydock CLI installed" and "ready to `ws create`". |
| `ws host check` | Could verify docker present, devcontainer CLI present, gh auth, claude auth (volume present), tailnet status, daemon-secrets dir mode. | Worth adding: returns a structured "what's missing" report. Would make Linux-host papercuts self-diagnosing. Also useful before `ws create` as a preflight. |
| Tailscale device API token | Must be generated in the tailnet admin UI (no programmatic flow today). | Could prompt with the URL when the token file is missing; eventually OAuth client tokens auto-rotate. |
| Claude `/login` | Interactive device flow inside the container. | Could prompt "Run `claude /login` in this container before ws exec --interactive"; not much more drydock can add. |

The honest split: drydock owns desk lifecycle. Host setup is one layer below. The bootstrap script bridges that gap, and `ws host init` / `ws host check` would be the modest drydock-side extensions worth landing in v1.x or v2.

## Reference deployment

`drydock-hillsboro` — Hetzner Cloud, Hillsboro OR. Documented at `~/Notebooks/ops-personal/tech/Drydock Fleet.md`. Used for the auction-crawl daily scraper. End-to-end deployment story in `~/Notebooks/ops-personal/projects/Auction Crawl.md`.

Bootstrap-to-first-desk on a fresh CX22-class box: ~15 minutes, mostly waiting on docker/tailscale apt installs.
