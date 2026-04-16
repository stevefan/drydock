# Known issues

## drydock-base:v1.0.7 — postStart chain breaks silently in verify block

**Observed:** 2026-04-16, ASI workspace, drydock-base:v1.0.7. Reproduced across two destroy/recreate cycles of the same workspace on the same host.

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

**Workaround (per-workspace, non-persistent):**
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

**To consume on asi:** bump `~/Unified Workspaces/asi/.devcontainer/drydock/Dockerfile` FROM line to `:v1.0.9` and recreate the workspace. (asi's bump committed separately in the asi repo.)

**Future bug recurrence:** if the asi (or any other) workspace fails postStart on v1.0.9+, `/tmp/firewall.log` will contain either an `=== init-firewall EXIT rc=N` line (with N = exit code) and probably an `!!! ERR line=L rc=R cmd=[…]` line above it. That data will pin the failing command for the next round of investigation.
