# Known issues

## drydock-base:v1.0.7 — postStart chain breaks silently in verify block

**Observed:** 2026-04-16, ASI drydock, drydock-base:v1.0.7. Reproduced across two destroy/recreate cycles of the same drydock on the same Harbor.

**Symptom:** After `ws create`, container is running and firewall is applied, but `tailscale` is `disconnected` and `supervisor` / `refresh_supervisor` are `dead` — the postStart `&&` chain never reaches `start-tailscale.sh` or `start-remote-control.sh`.

**Log fingerprint:** `/tmp/firewall.log` inside the container ends literally at:
```
Firewall configuration complete
Verifying firewall rules...
```
No `Firewall verification passed ...`, no `ERROR:`, no `=== Firewall init completed successfully ===`. Script exits silently between line 223 and line 249 of `base/init-firewall.sh`.

**Suspected cause (not confirmed):** combination of `set -euo pipefail` + `exec > >(tee -a "$LOG") 2>&1` — an exit in the verify block may be dropping its own error message because the process-substitution `tee` never flushes once the parent dies. Candidate offenders in that block:
- line 224 / 231 `curl --connect-timeout 5 ... >/dev/null 2>&1` — pipefail + `if`/`!` interaction
- line 240 `first_extra="${EXTRA[0]}"` — `set -u` would trip if `EXTRA` somehow isn't populated (it's set on line 148 under the same `$FIREWALL_EXTRA_DOMAINS` gate, so this shouldn't fire, but worth ruling out)
- line 241 curl→grep pipeline — pipefail propagation

**To diagnose:** run `sudo bash -x /usr/local/bin/init-firewall.sh 2>&1 | tee /tmp/fw-trace.log` inside a fresh v1.0.7 container with ASI's `FIREWALL_EXTRA_DOMAINS` set. The `-x` trace will show the exact failing line.

**Workaround (per-drydock, non-persistent):**
```
docker exec <container> /usr/local/bin/start-tailscale.sh
docker exec -d <container> /usr/local/bin/start-remote-control.sh
```

**Fix target:** `base/init-firewall.sh` lines 223–247. Consider either removing `set -u`/`pipefail` for the verify block, or rewriting the verify as a function that never causes the parent shell to exit, or at minimum adding a `trap 'echo "init-firewall: exiting at line $LINENO rc=$?"' EXIT` so future silent exits identify themselves.

**v1.0.8 status:** v1.0.8 (commit `9207584`) only touches git `safe.directory`, does not address this. Bug is still live on `main`.

---

## Resolution — drydock-base v1.0.9 (commit `6497cb3`, 2026-04-16)

**Status:** Fixed by elimination of the silent-exit class, not pinpointing the original failing line. The intermittent trigger (suspected: tee-subprocess SIGPIPE losing buffered bytes when the parent shell exited under cgroup signal propagation) was unreproducible on demand in a fresh container; rather than chase the flake, the fix makes silent exits *impossible by construction*:

1. **Direct file append.** `exec >> "$LOG" 2>&1` replaces `exec > >(tee -a "$LOG") 2>&1`. No tee subprocess to lose buffered bytes. Every byte written to stdout/stderr hits disk before the process can exit.
2. **ERR + EXIT traps.** ERR fires on any failed command under `set -e`, capturing `$LINENO` + `$BASH_COMMAND`. EXIT fires on every exit path with the final `rc`. Future failures *cannot* be silent — even on SIGTERM the EXIT trap line lands in the log; absence of the bottom-of-script "completed successfully" message tells you the script was killed mid-execution.
3. **`verify_firewall` function.** Verify block restructured for defensive set-e/set-u behavior: parameter expansion (`${FIREWALL_EXTRA_DOMAINS%% *}`) replaces the `EXTRA[0]` array reference (which depended on a separate earlier block having populated the array — fine on the happy path, brittle to debug if anything went sideways), explicit per-check exit-code capture, error messages always emit before the function returns.

**Verified:** Fresh v1.0.9 container with full ASI `FIREWALL_EXTRA_DOMAINS` env runs end-to-end with the new "Firewall verification passed - able to reach pypi.org (HTTP 200)" line + "init-firewall EXIT rc=0" trap message at the tail. Trap mechanism independently verified by deliberate `false` injection (ERR caught line + cmd, EXIT caught rc=1, both reached the log file before process exit).

**To consume on asi:** bump `~/Unified Workspaces/asi/.devcontainer/drydock/Dockerfile` FROM line to `:v1.0.9` and recreate the drydock. (asi's bump committed separately in the asi repo.)

**Future bug recurrence:** if the asi (or any other) drydock fails postStart on v1.0.9+, `/tmp/firewall.log` will contain either an `=== init-firewall EXIT rc=N` line (with N = exit code) and probably an `!!! ERR line=L rc=R cmd=[…]` line above it. That data will pin the failing command for the next round of investigation.

---

## Field report — 2026-04-25, kyber-beam first-time provisioning (drydock-base:v1.0.7)

**Context:** First-time onboarding of the kyber-beam Elixir/Phoenix project (`~/Development/kyber-beam`) into a drydock desk on Steven's laptop Harbor. Goal was "see the harness working" — minimum-viable boot of an unfamiliar project that had never been drydocked before. Surfaced multiple papercuts that aren't single bugs but compound friction worth addressing in design + docs. Items below are roughly ordered by severity / leverage.

### P0 — Tailscale authkey leaked to stdout

**Observed:** When the desk's postStart `start-tailscale.sh` did not bring Tailscale up (state remained `NeedsLogin`), the operator/agent ran `tailscale up --authkey="$(cat /run/secrets/tailscale_authkey)"` manually inside the desk via `ws exec`. Tailscale's CLI responded with a `non-default settings` advisory that **echoes the full authkey back as a re-runnable command**, in stdout, where it propagated to the agent's tool-result transcript. Authkey had to be rotated.

**Two compounding causes:**

1. **Secret-to-env mapping is missing.** `start-tailscale.sh` reads `$TAILSCALE_AUTHKEY` from the environment, but secrets land at `/run/secrets/tailscale_authkey` (a file). The scaffolded `.devcontainer/drydock/devcontainer.json` from `ws new` has:
   ```json
   "TAILSCALE_AUTHKEY": "${localEnv:TAILSCALE_AUTHKEY:}"
   ```
   This reads from the *Mac host's* environment, not from the workspace's own secret. Unless the operator independently sets `TAILSCALE_AUTHKEY` in their host shell before `ws create`, the secret is on disk but unused, Tailscale silently fails to authenticate, and the natural-but-wrong recovery (`tailscale up --authkey=$(cat /run/secrets/...)`) is the leak path.

2. **`tailscale up` echoes its authkey in advisory output.** Drydock can't fix the upstream tool, but it *can* avoid making operators reach for the manual command.

**Fix directions:**

- **Base-image side:** `start-tailscale.sh` should fall back from `$TAILSCALE_AUTHKEY` to `/run/secrets/tailscale_authkey` when the env var is empty. One-liner: `[ -z "${TAILSCALE_AUTHKEY:-}" ] && [ -r /run/secrets/tailscale_authkey ] && export TAILSCALE_AUTHKEY=$(cat /run/secrets/tailscale_authkey)`. Same pattern probably wanted for any other base script that bridges secret → env (currently `sync-claude-auth.sh` is the only other example, and it already reads files directly).
- **Scaffolder side:** `ws new`'s `devcontainer.json` should reference secrets, not host env. Either change the template to a marker like `${secret:tailscale_authkey}` (drydock-resolved at overlay generation time) or simply drop the `localEnv:` lines and rely on the base-image fallback above.
- **Doc side:** add a section to `operations/secrets.md` titled "How a secret reaches a process" enumerating the three paths (file at `/run/secrets/X`, env var via base-script fallback, devcontainer overlay mapping) and which to use when.

---

### P1 — `drydock-token` violates documented secret-name rule

**Observed:** README and operator docs state secret names must be "alphanumeric + underscores only." Inventory of actual secrets across 8 workspaces shows the name `drydock-token` (with a dash) in `ws_asi`, `ws_auction_crawl`, `ws_fleet_auth`, `ws_patchwork`. Either the validator has a carve-out, or the rule has drifted, or those secrets were written before the rule existed. Inconsistency confuses both new operators and any agent trying to follow conventions.

**Action:** decide whether to (a) tighten the validator and rename the existing secrets to `drydock_token`, or (b) loosen the documented rule to allow dashes. Either is fine; current state is the worst because it's de facto allowed but documented as forbidden.

---

### P1 — No preflight / "what secrets does this desk need?" check

**Observed:** `ws_substrate` has a project yaml at `~/.drydock/projects/substrate.yaml` but its secrets directory `~/.drydock/secrets/ws_substrate/` is empty. `ws create substrate` would presumably boot, then fail at runtime when whatever process inside the desk tries to read a secret that isn't there.

Conversely, this kyber-beam onboarding hit the inverse: the project needed an OAuth token in `auth-profiles.json` format, and there was no declarative way to say so — every required secret had to be discovered by reading the project's source and inferring.

**Fix direction:** project yaml gains a `secrets:` block:
```yaml
secrets:
  required:
    - name: kyber_auth_profiles
      mount_path: ~/.openclaw/agents/main/agent/auth-profiles.json
      mode: 0600
      description: "Kyber-BEAM OAuth credentials (sk-ant-oat... format)."
    - name: tailscale_authkey
      env: TAILSCALE_AUTHKEY
      description: "Tailscale ephemeral auth key."
  optional:
    - name: discord_bot_token
      env: DISCORD_BOT_TOKEN
```

`ws create` then refuses to start with a clear error listing missing required secrets and the exact `ws secret set` commands to run. Bonus: `ws doctor` could cross-reference this against existing harbor state and flag stranded secrets dirs (case of `ws_substrate`) or stranded project yamls.

---

### P1 — Token-to-path mapping is manual

**Observed:** Apps want secrets at specific paths in specific formats. Kyber-BEAM wants `~/.openclaw/agents/main/agent/auth-profiles.json`. Claude Code (in `ws_infra`) wants `~/.claude/.credentials.json` and `~/.claude.json`. Each app's solution is bespoke: kyber-beam's onboarding used `ln -sf /run/secrets/kyber_auth_profiles ~/.openclaw/agents/main/agent/auth-profiles.json`; `sync-claude-auth.sh` is hardcoded into the base image for the Claude case.

This is a generalizable pattern that's currently 1-of-N with no abstraction.

**Fix direction:** the `secrets:` block in project yaml above carries a `mount_path:` field. Drydock generates a small `place-secrets.sh` into the overlay that runs at postStart, symlinks each secret into its declared location with the declared mode, and logs each placement to `/tmp/secrets.log`. Removes one bespoke base-image script (`sync-claude-auth.sh` becomes a special-cased example, then redundant).

---

### P1 — OAuth vs API-key auth story is underdocumented

**Observed:** `getting-started.md` line 26 lists `ANTHROPIC_API_KEY` as the canonical Claude Code auth secret. But the actual fleet uses two different auth modes:
- `ws_microfoundry`, `ws_patchwork` use `anthropic_api_key` (API key path)
- `ws_infra`, `ws_fleet_auth` use `claude_credentials` + `claude_account_state` (OAuth path, consumed by `sync-claude-auth.sh`)

The OAuth path is documented only in code comments at the top of `base/sync-claude-auth.sh`. Operators on the Anthropic Max plan don't realize they can use OAuth end-to-end and may pay API rates unnecessarily. Kyber-BEAM's own LLM plugin auto-detects either format (`api_client.ex:23–26`), so OAuth works transparently when the file format is right — but you have to know.

**Fix direction:** lift the auth-mode discussion from `sync-claude-auth.sh`'s header comment into a dedicated `operations/auth.md`, cross-reference from `getting-started.md`, and document the two file formats (`.credentials.json` for Claude CLI, OAuth-bearing JSON for direct API use).

---

### P2 — Firewall allowlist needs ecosystem recipes

**Observed:** Every new-language project rediscovers its package mirrors and toolchain hosts. For kyber-beam (Elixir + Node) the discovered allowlist was:
- Elixir/Hex: `hex.pm`, `repo.hex.pm`, `builds.hex.pm`
- Node/npm: `registry.npmjs.org`, `nodejs.org`
- asdf + GitHub-hosted release artifacts: `github.com`, `codeload.github.com`, `objects.githubusercontent.com`, `raw.githubusercontent.com`
- apt mirrors (Erlang build deps): `deb.debian.org`, `security.debian.org`
- Erlang Solutions packages: `erlang-solutions.com`, `packages.erlang-solutions.com`, `binaries.erlang-solutions.com` (these 502'd at the time and we fell back to elsewhere — but worth allowlisting for next attempt)

None of these are obvious from a clean start. The "iterate until the build stops failing" loop is wasteful.

**Fix direction:** ship `~/Unified Workspaces/drydock/recipes/firewall/` (or similar) with `elixir.yaml`, `python.yaml`, `node.yaml`, `rust.yaml`, etc. Project yaml gains an `extends:` field:
```yaml
extends:
  - recipes/firewall/elixir.yaml
  - recipes/firewall/node.yaml
firewall_extra_domains:
  - api.anthropic.com  # project-specific additions only
```
Lowers the barrier-to-first-boot for any new project type.

---

### P2 — `ws sync` blocks on untracked files in worktree

**Observed:** Inside-desk build steps (e.g. `npm install` under `/workspace/priv/agent-sdk/`, or `mix deps.compile` writing `_build/`) emit files into the bind-mounted worktree at `~/.drydock/worktrees/ws_<name>/`. Those land as untracked files in the `ws/<name>` git branch. `ws sync` then refuses with `worktree_dirty: Worktree has uncommitted changes`.

**Repro:**
```bash
ws create kyber-beam
ws exec kyber-beam -- bash -c 'cd /workspace/priv/agent-sdk && npm install'
# now in source repo:
git commit ...
ws sync kyber-beam   # → worktree_dirty
```

**Workaround used during this session:** `rm -rf` the offending paths in the worktree before sync. Brittle.

**Fix directions, ordered by ambition:**
1. Document the failure mode in `operations/secrets.md` or a new `operations/syncing.md`. (Cheap.)
2. Add `ws sync --clean` flag that runs `git clean -fd` in the worktree before fast-forwarding, with a confirmation prompt or `--yes`.
3. Discourage in-desk builds from polluting the worktree by recommending build artifacts go to `/home/node/build/` or a named volume, with appropriate symlinks. (Pattern already exists for `claude-code-config` and `commandhistory` volumes — could generalize.)

---

### P2 — No `ws rebuild` verb

**Observed:** Editing the project Dockerfile (e.g. to install a missing system package) and wanting the change to take effect required `ws stop kyber-beam && ws create kyber-beam --force`. Both verbs technically exist but the rebuild path isn't obvious — `ws upgrade` exists for bumping the drydock-base tag, which is a different concern. New operators will look for `ws rebuild` / `ws update` / `ws refresh` and find nothing.

**Fix direction:** add `ws rebuild <name>` as a documented alias for `stop && create --force`. Bonus: `ws rebuild --no-cache` for stuck Dockerfile layer issues. Trivial implementation, large UX win.

---

### P3 — `ws new` Dockerfile starter lacks language hints

**Observed:** `ws new`'s scaffolded Dockerfile contains a single comment showing `python3 python3-pip` as the apt example. For an Elixir project, the operator (or agent) has to figure out from scratch that:
1. Bookworm's stock `elixir` package is 1.14 — too old for Phoenix 1.7
2. The fix is to install `erlang-dev erlang-xmerl ...` from apt and unpack a precompiled Elixir 1.16 from `github.com/elixir-lang/elixir/releases/download/v1.16.3/elixir-otp-25.zip`
3. `build-essential inotify-tools` are also wanted

That's a real 30-minute detour for a first-time Elixir-on-drydock onboarding. See the kyber-beam Dockerfile (`~/Development/kyber-beam/.devcontainer/drydock/Dockerfile`) for the worked example.

**Fix direction:** `ws new <project> --lang elixir` (or `--stack elixir-phoenix`) emits a Dockerfile with the worked-out apt + GitHub-release-binary pattern and a matching `recipes/firewall/elixir.yaml` reference.

---

### P3 — Worktree branches (`ws/<name>`) lack documented lifecycle

**Observed:** Drydock cuts a `ws/<name>` branch on `ws create` and the worktree lives there. If an operator (or agent) makes useful changes inside the desk — e.g. fixing the project's Dockerfile, as happened repeatedly during this session — there's no documented path for landing those changes back on the project's main branch. We worked around it by editing the source repo on the host and running `ws sync` to fast-forward, but that's only obvious in retrospect.

**Fix direction:** `operations/worktrees.md` (or a section in getting-started) covering: where commits go, how `ws sync` interacts with diverging branches, recommended workflow for in-desk-edits-meant-for-main (cherry-pick? PR from `ws/<name>`? source-repo edit + sync?).

---

### P3 — Audit log rotation

**Observed:** `~/.drydock/audit.log` was 886 KB at the time of this session, with no apparent rotation policy. Will eventually become a chore.

**Fix direction:** simple monthly rotation via `logrotate` or a built-in size-based truncation in `wsd`.

---

### P3 — Secret-age and rotation visibility

**Observed:** `ws secret list <desk>` shows name, mode, size, modified-time. Modified-time is good; what's missing is a higher-level "is anything stale" view. No `ws secret info <desk> <name>` and no fleet-wide `ws secret audit` ("show all secrets older than 90 days").

**Fix direction:** add `ws secret audit [--older-than 90d]` that walks `~/.drydock/secrets/` and reports. Cheap given the metadata is already exposed.

---

### Note for future agents

If you (Claude or otherwise) are working on drydock and any of the above resonates, the kyber-beam case study is reproducible end-to-end:

1. `~/Development/kyber-beam/.devcontainer/drydock/{Dockerfile,devcontainer.json}` — committed worked example of the Elixir-on-drydock pattern (commits `b801d55`, `19d7501`, `f698ed1` on `main`).
2. `~/.drydock/projects/kyber-beam.yaml` — example of `firewall_extra_domains:` for the Elixir+Node ecosystem.
3. `~/.drydock/secrets/ws_kyber_beam/` — has `kyber_auth_profiles` (reused from `ws_infra/claude_credentials`, demonstrating the fleet-shared-auth pattern in action) and `tailscale_authkey`.

The kyber-beam desk itself was running and serving the LiveView dashboard (`localhost:4000`) and HTTP API (`localhost:4001`) at the end of the session. To revisit: `ws exec kyber-beam -- tail ~/kyber-beam.log` for live state.
