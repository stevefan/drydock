"""ws host — manage the local drydock host installation.

Two subcommands:

- `ws host init` — idempotent post-pipx setup (state dirs, gitconfig stub).
  Closes the gap between "drydock CLI installed" and "ready to ws create".
- `ws host check` — preflight that verifies docker, devcontainer CLI, gh
  auth, tailscale, drydock state dirs/modes. Returns structured pass/warn/fail.
  Exits non-zero on any required-check failure so it can gate CI / scripted
  bootstraps. Warnings exit 0 (you can still ws create; some niceties absent).

These are the small drydock-side affordances called out in
`docs/host-bootstrap.md`. The bash bootstrap script remains the install
vector (drydock can't bootstrap itself); this is what comes after.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

from drydock.core import WsError


def _drydock_state_dirs() -> dict[str, dict]:
    """Canonical drydock state directories with their expected modes.

    `secrets` and `daemon-secrets` are 0700 because they hold credential
    material (per-drydock secrets and Harbor-level admin tokens respectively).
    The rest are 0755 — readable by other users on the Harbor but only the
    owner writes.
    """
    home = Path.home()
    return {
        "projects": {"path": home / ".drydock" / "projects", "mode": 0o755},
        "secrets": {"path": home / ".drydock" / "secrets", "mode": 0o700},
        "worktrees": {"path": home / ".drydock" / "worktrees", "mode": 0o755},
        "overlays": {"path": home / ".drydock" / "overlays", "mode": 0o755},
        "daemon-secrets": {"path": home / ".drydock" / "daemon-secrets", "mode": 0o700},
        "logs": {"path": home / ".drydock" / "logs", "mode": 0o755},
        "bin": {"path": home / ".drydock" / "bin", "mode": 0o755},
        # Dedicated dir for the drydock daemon socket so the overlay can bind-mount
        # the dir (not the socket file) into drydock containers — durable
        # across daemon restarts.
        "run": {"path": home / ".drydock" / "run", "mode": 0o755},
    }


def _repo_root() -> Path | None:
    """Find the drydock repo root from the installed package location.

    Works for pipx-editable installs (the common case): `src/drydock/cli/host.py`
    is at `<repo>/src/drydock/cli/host.py`, so parents[3] is the repo root.
    Non-editable installs (wheel) won't have the scripts/ dir — caller
    handles the None case gracefully.
    """
    try:
        candidate = Path(__file__).resolve().parents[3]
    except IndexError:
        return None
    if (candidate / "scripts").is_dir():
        return candidate
    return None


def _ensure_drydock_symlink(
    target: Path = Path("/usr/local/bin/drydock"),
    home: Path | None = None,
) -> str | None:
    """Idempotent: symlink ``target`` → the pipx-installed drydock binary.

    pipx installs at ``~/.local/bin/drydock`` (a symlink) or directly at
    ``~/.local/share/pipx/venvs/drydock/bin/drydock``. Non-interactive
    ssh sessions don't have ``~/.local/bin`` on PATH by default, so
    ``ssh harbor 'drydock list'`` fails with "command not found" without
    a system-wide symlink. Tonight's smoke harness hit this; codifying.

    Returns a short action string if a symlink was created/updated,
    None if no action was needed. Refuses to overwrite a regular file
    at ``target`` (returns None) — that's an unusual install state we
    don't want to surprise-clobber.
    """
    home = home or Path.home()
    source = shutil.which("drydock")
    if not source:
        for candidate in (
            home / ".local" / "bin" / "drydock",
            home / ".local" / "share" / "pipx" / "venvs" / "drydock" / "bin" / "drydock",
        ):
            if candidate.exists():
                source = str(candidate)
                break
    if not source:
        return None  # nothing installed; bootstrap script handles install

    if target.exists() and not target.is_symlink():
        # Regular file at the path — don't clobber. Operator chose to
        # install something there; we don't second-guess.
        return None

    if target.is_symlink():
        current = os.readlink(target)
        if current == source:
            return None  # already correct; no action
        target.unlink()

    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source)
    return f"symlinked {target} → {source}"


def _install_drydock_rpc(bin_dir: Path) -> str | None:
    """Copy scripts/drydock-rpc into ~/.drydock/bin/drydock-rpc.

    The overlay bind-mounts this file into every drydock container at
    /usr/local/bin/drydock-rpc, giving workers a tiny stdlib-only JSON-RPC
    client for daemon. See docs/v2-design-protocol.md §1.

    Idempotent — no-op when the target already matches the source byte-for-byte.
    Returns a short action string on change, None otherwise.
    """
    repo = _repo_root()
    if repo is None:
        return None
    source = repo / "scripts" / "drydock-rpc"
    if not source.exists():
        return None
    target = bin_dir / "drydock-rpc"
    if target.exists() and target.read_bytes() == source.read_bytes():
        # Still ensure mode — harmless if already 0755.
        if target.stat().st_mode & 0o777 != 0o755:
            os.chmod(target, 0o755)
            return f"chmod 0o755 {target}"
        return None
    bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    os.chmod(target, 0o755)
    return f"installed {target}"


@click.group()
def host():
    """Manage the local drydock host installation."""


@host.command("init")
@click.pass_context
def host_init(ctx):
    """Set up drydock state dirs and host bootstrap files (idempotent)."""
    out = ctx.obj["output"]
    actions: list[str] = []

    for name, spec in _drydock_state_dirs().items():
        path = spec["path"]
        mode = spec["mode"]
        if not path.exists():
            path.mkdir(parents=True)
            actions.append(f"created {path}")
        actual_mode = path.stat().st_mode & 0o777
        if actual_mode != mode:
            os.chmod(path, mode)
            actions.append(f"chmod {oct(mode)} {path}")

    # One-time vocabulary migration: legacy ws_<slug> filesystem artifacts
    # (secrets dirs, worktrees, overlays) renamed to dock_<slug>. Idempotent.
    from drydock.core.migrate_v1_artifacts import migrate_v1_artifacts
    fs_summary = migrate_v1_artifacts(Path.home() / ".drydock")
    for entry in fs_summary["renamed"]:
        actions.append(f"v1-migrate: {entry}")
    for entry in fs_summary["skipped"]:
        actions.append(f"v1-migrate skipped: {entry}")

    # Devcontainer template bind-mounts ${HOME}/.gitconfig; on Linux without it
    # docker hard-fails with "bind source path does not exist". Touch a stub.
    gitconfig = Path.home() / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.touch(mode=0o644)
        actions.append(f"touched stub {gitconfig}")

    # /var/log/drydock is the canonical cron-output sink on Linux. Create it
    # only when running as root on Linux — owning a system path on macOS or
    # as a non-root user is the wrong instinct.
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        sys_log = Path("/var/log/drydock")
        if not sys_log.exists():
            sys_log.mkdir(parents=True)
            actions.append(f"created {sys_log}")

    # Git safe.directory for worktrees. Drydock creates worktrees
    # owned by uid 1000 (the container's node user) so pip editable
    # installs + npm + playwright-browser-install can write inside
    # the container. When Harbor-side git commands run (ws sync, any
    # Harbor-root debugging), git refuses with "dubious ownership"
    # because root != 1000. Allowlisting the worktrees tree once
    # fixes this for every current and future desk.
    worktrees_glob = str(Path.home() / ".drydock" / "worktrees" / "*")
    existing = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        capture_output=True, text=True,
    )
    if worktrees_glob not in (existing.stdout or "").splitlines():
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", worktrees_glob],
            check=False,
        )
        actions.append(f"git safe.directory += {worktrees_glob}")

    # Deploy the in-desk JSON-RPC client (drydock-rpc). The overlay bind-mounts
    # this file into every drydock container; shipping it via `ws host init`
    # keeps the source-of-truth in the repo + a stable deploy path per Harbor.
    bin_dir = _drydock_state_dirs()["bin"]["path"]
    rpc_action = _install_drydock_rpc(bin_dir)
    if rpc_action:
        actions.append(rpc_action)

    # System-wide drydock symlink — Linux+root only. See _ensure_drydock_symlink.
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        symlink_action = _ensure_drydock_symlink()
        if symlink_action:
            actions.append(symlink_action)

    out.success(
        {"actions": actions, "noop": len(actions) == 0},
        human_lines=[f"  {a}" for a in actions]
        or ["host already initialized; nothing to do."],
    )


def _check_docker() -> tuple[str, str | None]:
    """Returns (status, detail) where status is 'ok' | 'warn' | 'fail'."""
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=8,
        )
    except FileNotFoundError:
        return ("fail", "docker not installed")
    except subprocess.TimeoutExpired:
        return ("fail", "docker info timed out (daemon hung?)")
    if r.returncode != 0:
        return ("fail", f"docker daemon not responding: {r.stderr.strip()[:80]}")
    return ("ok", f"server {r.stdout.strip()}")


def _check_devcontainer() -> tuple[str, str | None]:
    try:
        r = subprocess.run(
            ["devcontainer", "--version"],
            capture_output=True, text=True, timeout=8,
        )
    except FileNotFoundError:
        return ("fail", "devcontainer CLI not installed (npm install -g @devcontainers/cli)")
    except subprocess.TimeoutExpired:
        return ("fail", "devcontainer --version timed out")
    return ("ok", r.stdout.strip())


def _check_tailscale() -> tuple[str, str | None]:
    try:
        r = subprocess.run(
            ["tailscale", "status", "--self", "--peers=false"],
            capture_output=True, text=True, timeout=8,
        )
    except FileNotFoundError:
        return ("warn", "not installed (recommended for tailnet identity)")
    except subprocess.TimeoutExpired:
        return ("warn", "tailscale status timed out")
    if r.returncode != 0:
        return ("warn", "installed but not connected (tailscale up --hostname=...)")
    return ("ok", "connected")


def _check_gh_auth() -> tuple[str, str | None]:
    try:
        r = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=8,
        )
    except FileNotFoundError:
        return ("warn", "gh not installed (recommended for private repo clones)")
    except subprocess.TimeoutExpired:
        return ("warn", "gh auth status timed out")
    if r.returncode != 0:
        return ("warn", "gh installed but not authenticated (gh auth login --web)")
    return ("ok", "authenticated")


def _check_state_dir(name: str, path: Path, expected_mode: int) -> tuple[str, str | None]:
    if not path.exists():
        return ("warn", f"missing — run `ws host init`")
    actual_mode = path.stat().st_mode & 0o777
    if actual_mode != expected_mode:
        return ("warn", f"mode {oct(actual_mode)} != expected {oct(expected_mode)} — run `ws host init`")
    return ("ok", str(path))


def _check_gitconfig() -> tuple[str, str | None]:
    gc = Path.home() / ".gitconfig"
    if not gc.exists():
        return ("warn", f"{gc} missing — devcontainer bind-mount will fail; run `ws host init`")
    return ("ok", str(gc))


@host.command("check")
@click.pass_context
def host_check(ctx):
    """Preflight: verify host has everything `ws create` needs.

    Exits 1 on any required-check failure (docker, devcontainer CLI).
    Warnings (missing tailscale, gh auth, state dirs, gitconfig stub) exit 0.
    """
    out = ctx.obj["output"]
    checks: list[dict] = []

    def add(name: str, result: tuple[str, str | None]):
        checks.append({"check": name, "status": result[0], "detail": result[1]})

    # Required: docker, devcontainer CLI
    add("docker", _check_docker())
    add("devcontainer", _check_devcontainer())

    # State directories
    for name, spec in _drydock_state_dirs().items():
        add(f"dir:{name}", _check_state_dir(name, spec["path"], spec["mode"]))

    # gitconfig stub
    add("gitconfig", _check_gitconfig())

    # Recommended: tailscale, gh auth
    add("tailscale", _check_tailscale())
    add("gh-auth", _check_gh_auth())

    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    oks = len(checks) - fails - warns

    summary_lines = []
    for c in checks:
        symbol = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[c["status"]]
        line = f"  [{symbol}] {c['check']}"
        if c["detail"]:
            line += f" — {c['detail']}"
        summary_lines.append(line)
    summary_lines.append("")
    if fails:
        summary_lines.append(f"FAIL: {fails} required check(s) failed; ws create will not work.")
    elif warns:
        summary_lines.append(f"OK with {warns} warning(s).")
    else:
        summary_lines.append("All checks passed.")

    out.success(
        {
            "checks": checks,
            "summary": {"ok": oks, "warn": warns, "fail": fails},
            "passed": fails == 0,
        },
        human_lines=summary_lines,
    )

    if fails:
        raise SystemExit(1)


# --------------- ws host audit ---------------

@host.command("audit")
@click.option("--probe-desks", is_flag=True,
              help="Also docker-exec into one desk per project to probe "
                   "in-container helper presence (slower; detects "
                   "feature-merged-but-base-image-not-rebuilt).")
@click.pass_context
def host_audit(ctx, probe_desks):
    """Snapshot what's actually live on this Harbor.

    Closes the gap between "code in main" and "code running here".
    Six layers (plus optional helpers probe): code, daemon, capability
    surface, base images, desks, leases. Each layer fails independently;
    a single broken layer doesn't kill the whole audit.
    """
    out = ctx.obj["output"]
    from drydock.core.host_audit import gather_audit
    audit = gather_audit(probe_desks=probe_desks)
    out.success(audit, human_lines=_format_audit_human(audit))


def _format_audit_human(audit: dict) -> list[str]:
    lines: list[str] = []
    lines.append(f"Harbor audit  ({audit['audited_at']})")
    lines.append("")

    # ---- code ----
    c = audit.get("code") or {}
    lines.append("== Code ==")
    if c.get("ok") is False:
        lines.append(f"  ! {c.get('error')}")
    else:
        ver = c.get("package_version", "?")
        loc = c.get("install_location", "?")
        editable = " (editable)" if c.get("editable") else ""
        lines.append(f"  package: drydock {ver}{editable}")
        lines.append(f"  install: {loc}")
        if c.get("git_sha"):
            branch = c.get("git_branch") or "?"
            dirty = "  ⚠ DIRTY" if c.get("dirty") else ""
            ahead = c.get("ahead_origin_main")
            behind = c.get("behind_origin_main")
            drift = ""
            if ahead is not None and behind is not None:
                if ahead == 0 and behind == 0:
                    drift = "  (in sync with origin/main)"
                else:
                    bits = []
                    if ahead:  bits.append(f"+{ahead} ahead")
                    if behind: bits.append(f"-{behind} behind")
                    drift = f"  ({', '.join(bits)} origin/main)"
            lines.append(f"  git:     {branch}@{c['git_sha']}{drift}{dirty}")
    lines.append("")

    # ---- daemon ----
    d = audit.get("daemon") or {}
    lines.append("== Daemon ==")
    if d.get("ok") is False:
        lines.append(f"  ! {d.get('error')}")
    else:
        running = d.get("running")
        mark = "✓" if running and d.get("health_responsive") else ("⚠" if running else "✗")
        state = "running + responsive" if (running and d.get("health_responsive")) \
            else ("running but socket unresponsive" if running else "NOT RUNNING")
        lines.append(f"  [{mark}] daemon: {state}")
        if d.get("pid"):
            lines.append(f"      pid={d['pid']}  uptime={d.get('uptime_human', '?')}")
        lines.append(f"      socket={d.get('socket_path')}  present={d.get('socket_present')}")
        if d.get("last_log_line"):
            lines.append(f"      last log: {d['last_log_line'][:120]}")
    lines.append("")

    # ---- capability surface ----
    cs = audit.get("capability") or {}
    lines.append("== Capability surface (per running CLI code) ==")
    if cs.get("ok") is False:
        lines.append(f"  ! {cs.get('error')}")
    else:
        lines.append(f"  supported: {', '.join(cs.get('supported_types', []))}")
        if cs.get("reserved_types"):
            lines.append(f"  reserved:  {', '.join(cs['reserved_types'])}")
        lines.append(f"  kinds:     {', '.join(cs.get('capability_kinds', []))}")
    lines.append("")

    # ---- base images ----
    bi = audit.get("base_images") or {}
    lines.append("== drydock-base images ==")
    if bi.get("ok") is False:
        lines.append(f"  ! {bi.get('error')}")
    else:
        tags = bi.get("tags", [])
        if not tags:
            lines.append(f"  (none pulled)")
        for t in tags:
            lines.append(f"  {t['tag']:<14} id={t['id']:<12}  pulled {t['created']}")
    lines.append("")

    # ---- desks ----
    ds = audit.get("desks") or {}
    lines.append(f"== Desks ({ds.get('count', 0)}) ==")
    if ds.get("ok") is False:
        lines.append(f"  ! {ds.get('error')}")
    else:
        for desk in ds.get("desks", []):
            state_mark = {"running": "✓", "suspended": "·", "created": "·"}.get(desk["state"], "?")
            base = desk.get("base_image_tag") or "(custom)"
            lines.append(f"  [{state_mark}] {desk['name']:<24} state={desk['state']:<10} base={base}")
            caps = desk.get("capabilities") or []
            if caps:
                lines.append(f"        capabilities: {', '.join(caps)}")
            ent_bits = []
            for label, key in (
                ("secrets", "delegatable_secrets"),
                ("storage", "delegatable_storage_scopes"),
                ("network", "delegatable_network_reach"),
            ):
                v = desk.get(key) or []
                if v:
                    ent_bits.append(f"{label}={len(v)}")
            sc = desk.get("secrets_count", 0)
            if sc:
                ent_bits.append(f"secret-files={sc}")
            if ent_bits:
                lines.append(f"        entitlements: {', '.join(ent_bits)}")
            rh = desk.get("resources_hard") or {}
            if rh:
                rh_bits = [f"{k}={v}" for k, v in rh.items()]
                lines.append(f"        ceilings: {', '.join(rh_bits)}")
            # Phase 0: surface YAML drift between pinned and current.
            drift = desk.get("yaml_drift")
            if drift in ("drifted", "yaml_missing"):
                pinned = desk.get("pinned_yaml_sha256") or "?"
                current = desk.get("current_yaml_sha256") or "missing"
                marker = "⚠" if drift == "drifted" else "✗"
                msg = (
                    f"YAML EDITED since pin (pinned={pinned}, current={current}) — run `ws project reload {desk['name']}`"
                    if drift == "drifted"
                    else f"YAML FILE MISSING (pinned={pinned}) — restore or destroy this drydock"
                )
                lines.append(f"        [{marker}] yaml drift: {msg}")
    lines.append("")

    # ---- leases ----
    ls = audit.get("leases") or {}
    lines.append("== Leases ==")
    if ls.get("ok") is False:
        lines.append(f"  ! {ls.get('error')}")
    else:
        active = ls.get("active_total", 0)
        revoked = ls.get("revoked_total", 0)
        lines.append(f"  active={active}  revoked={revoked}")
        for t, n in (ls.get("active_by_type") or {}).items():
            lines.append(f"    active {t}: {n}")
    lines.append("")

    # ---- helpers (only if probed) ----
    h = audit.get("helpers")
    if h is not None:
        lines.append("== In-container helpers ==")
        if h.get("ok") is False:
            lines.append(f"  ! {h.get('error')}")
        elif not h.get("probed"):
            lines.append(f"  ({h.get('note', 'nothing probed')})")
        else:
            for p in h["probed"]:
                missing = p.get("missing", [])
                if not missing:
                    lines.append(f"  ✓ {p['desk']} ({p['project']}): all helpers present")
                else:
                    lines.append(f"  ⚠ {p['desk']} ({p['project']}): missing {len(missing)} helper(s)")
                    for path, implication in zip(missing, p.get("feature_implications", [])):
                        lines.append(f"      ✗ {path}  →  {implication}")
        lines.append("")

    # Trim trailing blank
    while lines and lines[-1] == "":
        lines.pop()
    return lines
