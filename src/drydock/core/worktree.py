"""Git worktree creation for workspace isolation."""

import subprocess
from pathlib import Path

from .errors import WsError
from .workspace import Workspace

DEFAULT_WORKTREE_BASE = Path.home() / ".drydock" / "worktrees"


def create_worktree(ws: Workspace, base_dir: Path | None = None) -> Path:
    """Create a git worktree for the workspace, returning the worktree path.

    Creates branch ``ws.branch`` from ``ws.base_ref``. If the branch already
    exists, checks it out in the worktree instead of creating a new one.
    """
    base_dir = base_dir or DEFAULT_WORKTREE_BASE
    repo = Path(ws.repo_path)

    if not (repo / ".git").exists():
        raise WsError(
            f"Not a git repository: {repo}",
            fix=f"Ensure '{repo}' is a valid git repo, or pass --repo-path",
        )

    worktree_path = base_dir / ws.id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    if _branch_exists(repo, ws.branch):
        _run_git(
            repo,
            ["git", "worktree", "add", str(worktree_path), ws.branch],
            error_context=f"worktree add (existing branch '{ws.branch}')",
        )
    else:
        _run_git(
            repo,
            ["git", "worktree", "add", str(worktree_path), "-b", ws.branch, ws.base_ref],
            error_context=f"worktree add -b {ws.branch} {ws.base_ref}",
        )

    return worktree_path


def remove_worktree(repo_path: str, worktree_path: str) -> None:
    """Remove a git worktree. Raises on failure."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", worktree_path],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
    )
    return result.returncode == 0


def _run_git(repo: Path, cmd: list[str], error_context: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise WsError(
            f"git failed ({error_context}): {e.stderr.strip()}",
            fix="Check that the repo, branch, and base ref are valid",
        ) from e
