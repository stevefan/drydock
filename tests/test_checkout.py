"""Tests for git checkout (standalone clone) creation."""

import subprocess
from pathlib import Path

import pytest

from drydock.core.errors import WsError
from drydock.core.checkout import create_checkout, remove_checkout
from drydock.core.workspace import Workspace


def _git(repo: Path, *args: str):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _init_repo_with_origin(tmp_path: Path) -> Path:
    """Create a repo with a fake origin URL set."""
    repo = _init_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/example/project.git")
    return repo


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path)


@pytest.fixture
def repo_with_origin(tmp_path):
    return _init_repo_with_origin(tmp_path)


@pytest.fixture
def ws(repo):
    return Workspace(
        name="my-feature",
        project="app",
        repo_path=str(repo),
        branch="ws/my-feature",
        base_ref="HEAD",
    )


class TestCreateCheckout:
    def test_happy_path_new_branch(self, ws, repo, tmp_path):
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)

        assert dest == base / ws.id
        assert dest.is_dir()
        assert (dest / "README.md").exists()

    def test_clone_has_git_directory_not_file(self, ws, tmp_path):
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)

        git_path = dest / ".git"
        assert git_path.exists()
        assert git_path.is_dir(), ".git should be a directory (standalone clone), not a file (worktree)"

    def test_creates_branch(self, ws, repo, tmp_path):
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)

        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ws.branch

    def test_git_operations_work_inside_clone(self, ws, tmp_path):
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)

        for cmd in [
            ["git", "log", "--oneline"],
            ["git", "status"],
            ["git", "branch"],
        ]:
            result = subprocess.run(cmd, cwd=dest, capture_output=True, text=True)
            assert result.returncode == 0, f"{' '.join(cmd)} failed: {result.stderr}"

    def test_existing_branch_used(self, ws, repo, tmp_path):
        _git(repo, "branch", ws.branch)

        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)

        assert dest.is_dir()
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ws.branch

    def test_origin_url_rewritten(self, repo_with_origin, tmp_path):
        ws = Workspace(
            name="feat",
            project="app",
            repo_path=str(repo_with_origin),
            branch="ws/feat",
            base_ref="HEAD",
        )
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)

        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "https://github.com/example/project.git"

    def test_no_origin_in_source_no_error(self, ws, tmp_path):
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)

        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=dest, capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert str(ws.repo_path) in result.stdout.strip()

    def test_repo_missing_raises(self, tmp_path):
        ws = Workspace(
            name="bad", project="x",
            repo_path=str(tmp_path / "nonexistent"),
            branch="ws/bad",
        )
        with pytest.raises(WsError, match="Not a git repository"):
            create_checkout(ws, base_dir=tmp_path / "co")

    def test_repo_missing_error_has_fix(self, tmp_path):
        ws = Workspace(
            name="bad", project="x",
            repo_path=str(tmp_path / "nonexistent"),
            branch="ws/bad",
        )
        with pytest.raises(WsError) as exc_info:
            create_checkout(ws, base_dir=tmp_path / "co")
        assert exc_info.value.fix is not None
        assert "--repo-path" in exc_info.value.fix

    def test_dest_exists_raises(self, ws, tmp_path):
        base = tmp_path / "checkouts"
        (base / ws.id).mkdir(parents=True)

        with pytest.raises(WsError, match="already exists"):
            create_checkout(ws, base_dir=base)

    def test_invalid_base_ref_raises(self, repo, tmp_path):
        ws = Workspace(
            name="bad-ref", project="app",
            repo_path=str(repo),
            branch="ws/bad-ref",
            base_ref="nonexistent-ref-abc123",
        )
        with pytest.raises(WsError, match="git failed"):
            create_checkout(ws, base_dir=tmp_path / "co")

    def test_creates_base_dir_if_missing(self, ws, tmp_path):
        base = tmp_path / "a" / "b" / "c"
        dest = create_checkout(ws, base_dir=base)
        assert dest.exists()

    def test_checkout_path_uses_ws_id(self, ws, tmp_path):
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)
        assert dest.name == ws.id


class TestRemoveCheckout:
    def test_happy_path(self, ws, tmp_path):
        base = tmp_path / "checkouts"
        dest = create_checkout(ws, base_dir=base)
        assert dest.exists()

        remove_checkout(ws.repo_path, str(dest))
        assert not dest.exists()

    def test_missing_dir_no_error(self, tmp_path):
        remove_checkout("/some/repo", str(tmp_path / "nonexistent"))
