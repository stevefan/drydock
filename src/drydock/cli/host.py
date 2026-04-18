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
        # Dedicated dir for the wsd socket so the overlay can bind-mount
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


def _install_drydock_rpc(bin_dir: Path) -> str | None:
    """Copy scripts/drydock-rpc into ~/.drydock/bin/drydock-rpc.

    The overlay bind-mounts this file into every drydock container at
    /usr/local/bin/drydock-rpc, giving workers a tiny stdlib-only JSON-RPC
    client for wsd. See docs/v2-design-protocol.md §1.

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

    # Deploy the in-desk JSON-RPC client (drydock-rpc). The overlay bind-mounts
    # this file into every drydock container; shipping it via `ws host init`
    # keeps the source-of-truth in the repo + a stable deploy path per Harbor.
    bin_dir = _drydock_state_dirs()["bin"]["path"]
    rpc_action = _install_drydock_rpc(bin_dir)
    if rpc_action:
        actions.append(rpc_action)

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
