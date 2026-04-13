"""Git checkout (standalone clone) for workspace isolation."""

import logging
import shutil
import subprocess
from pathlib import Path

from .errors import WsError
from .workspace import Workspace

logger = logging.getLogger(__name__)

DEFAULT_CHECKOUT_BASE = Path.home() / ".drydock" / "worktrees"


def create_checkout(ws: Workspace, base_dir: Path | None = None) -> Path:
    """Create a standalone git clone for the workspace, returning the checkout path.

    Clones from ``ws.repo_path`` using ``--reference`` for disk efficiency.
    If ``ws.branch`` already exists in the source repo, clones that branch
    directly; otherwise clones the default branch and creates ``ws.branch``
    from ``ws.base_ref``.
    """
    base_dir = base_dir or DEFAULT_CHECKOUT_BASE
    repo = Path(ws.repo_path)

    if not (repo / ".git").exists():
        raise WsError(
            f"Not a git repository: {repo}",
            fix=f"Ensure '{repo}' is a valid git repo, or pass --repo-path",
        )

    dest = base_dir / ws.id
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        raise WsError(
            f"Checkout directory already exists: {dest}",
            fix=f"Remove '{dest}' or destroy the existing workspace first",
        )

    if _branch_exists(repo, ws.branch):
        _run_git(
            repo,
            ["git", "clone", "--reference", str(repo), "--branch", ws.branch, str(repo), str(dest)],
            error_context=f"clone (existing branch '{ws.branch}')",
        )
    else:
        _run_git(
            repo,
            ["git", "clone", "--reference", str(repo), str(repo), str(dest)],
            error_context="clone",
        )
        _run_git(
            dest,
            ["git", "checkout", "-b", ws.branch, ws.base_ref],
            error_context=f"checkout -b {ws.branch} {ws.base_ref}",
        )

    _rewrite_origin(repo, dest)

    return dest


def remove_checkout(repo_path: str, checkout_path: str) -> None:
    """Remove a checkout directory. Tolerates missing directories."""
    p = Path(checkout_path)
    if not p.exists():
        return
    try:
        shutil.rmtree(p)
    except Exception as exc:
        logger.warning("Failed to remove checkout %s: %s", checkout_path, exc)


def _rewrite_origin(source_repo: Path, dest: Path) -> None:
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=source_repo,
        capture_output=True,
        text=True,
    )
    origin_url = result.stdout.strip()
    if result.returncode == 0 and origin_url:
        _run_git(
            dest,
            ["git", "remote", "set-url", "origin", origin_url],
            error_context="remote set-url origin",
        )


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
    )
    return result.returncode == 0


def _run_git(cwd: Path, cmd: list[str], error_context: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise WsError(
            f"git failed ({error_context}): {e.stderr.strip()}",
            fix="Check that the repo, branch, and base ref are valid",
        ) from e
