"""Tests for ephemeral-container lifecycle: suspended resume, running-error-with-fix,
--force rebuild, and error-state rejection."""

import json
import subprocess
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core.project_config import ProjectConfig
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _assert_no_volume_rm(calls, volume_name):
    """Assert no recorded subprocess call removes the named docker volume.

    Catches regressions where ws create --force or ws destroy accidentally
    tears down project-declared named volumes — the thin-runtime/thick-volumes
    contract requires data volumes to outlive any single container.
    """
    for call in calls:
        args = call.args[0] if call.args else call.kwargs.get("args", [])
        if not isinstance(args, (list, tuple)):
            continue
        argv = [str(a) for a in args]
        # Explicit `docker volume rm <name>` invocation
        if len(argv) >= 3 and argv[:3] == ["docker", "volume", "rm"]:
            assert volume_name not in argv, (
                f"docker volume rm invoked against project volume {volume_name!r}: {argv}"
            )
        # `docker rm -v` / `docker rm --volumes` removes anonymous volumes but
        # must not target this container, and in any case the flag shouldn't
        # appear — named volumes survive anyway, but the flag is the nearest
        # regression surface worth catching.
        if len(argv) >= 2 and argv[:2] == ["docker", "rm"]:
            assert "-v" not in argv and "--volumes" not in argv, (
                f"docker rm called with volume-removal flag: {argv}"
            )


def _init_repo(path, devcontainer=True):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    if devcontainer:
        (path / ".devcontainer").mkdir()
        (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


# Regression: suspended workspaces couldn't be resumed — WorkspaceExistsError
@patch("drydock.cli.create.DevcontainerCLI")
def test_create_on_suspended_workspace_resumes(MockCLI, tmp_path, monkeypatch):
    """Catches failure where suspended workspaces can't be resumed via ws create."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    # Set up a suspended workspace with an existing checkout
    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    checkout_dir = tmp_path / ".drydock" / "worktrees" / "ws_myws"
    subprocess.run(
        ["git", "clone", str(repo), str(checkout_dir)],
        capture_output=True, text=True, check=True,
    )
    ws = Workspace(
        name="myws", project="proj", repo_path=str(repo),
        worktree_path=str(checkout_dir), branch="ws/myws",
        state="suspended", container_id="old-ctr-dead",
    )
    registry.create_workspace(ws)
    registry.update_state("myws", "suspended")

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "new-ctr-123", "containerId": "new-ctr-123"}

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "myws", "--repo-path", str(repo)],
    )
    assert result.exit_code == 0, result.output

    refreshed = registry.get_workspace("myws")
    assert refreshed.state == "running"
    assert refreshed.container_id == "new-ctr-123"
    registry.close()


# LLM-usability contract: JSON error must include executable fix field
def test_create_on_running_workspace_errors_with_fix(tmp_path, monkeypatch):
    """Catches LLM-usability failure: caller must parse fix from JSON and re-run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    ws = Workspace(
        name="active", project="proj", repo_path="/tmp/repo",
        state="running", container_id="ctr-live",
    )
    registry.create_workspace(ws)
    registry.update_state("active", "running")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "active"],
        obj={"registry": registry, "output": Output(force_json=True), "dry_run": False},
    )

    assert result.exit_code != 0
    err = json.loads(result.output.strip())
    assert err["error"] == "workspace_already_running"
    assert "fix" in err
    assert "--force" in err["fix"]
    registry.close()


# Scariest regression in this lifecycle change: --force silently wipes user data
@patch("drydock.cli.create.DevcontainerCLI")
def test_force_rebuild_preserves_checkout(MockCLI, tmp_path, monkeypatch):
    """Catches --force silently destroying checkout (volumes are the container's
    concern; checkout is what we control and must preserve across rebuild)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    checkout_dir = tmp_path / ".drydock" / "worktrees" / "ws_forcews"
    subprocess.run(
        ["git", "clone", str(repo), str(checkout_dir)],
        capture_output=True, text=True, check=True,
    )
    # Seed a user file in the checkout — this must survive --force
    sentinel = checkout_dir / "user-work.txt"
    sentinel.write_text("precious user data")

    ws = Workspace(
        name="forcews", project="proj", repo_path=str(repo),
        worktree_path=str(checkout_dir), branch="ws/forcews",
        state="running", container_id="old-ctr",
    )
    registry.create_workspace(ws)
    registry.update_state("forcews", "running")

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "new-ctr-456", "containerId": "new-ctr-456"}

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "forcews", "--repo-path", str(repo), "--force"],
    )
    assert result.exit_code == 0, result.output

    assert sentinel.exists(), "User data in checkout was destroyed by --force"
    assert sentinel.read_text() == "precious user data"

    refreshed = registry.get_workspace("forcews")
    assert refreshed.state == "running"
    assert refreshed.container_id == "new-ctr-456"
    registry.close()


# Contract: error-state workspaces need --force to rebuild; non-forced create must
# not silently reuse them (which would ignore new CLI args and mask the failure).
def test_create_on_error_workspace_requires_force(tmp_path, monkeypatch):
    """Catches regression where error-state workspaces get silently reused on plain
    ws create, hiding the original failure and ignoring new CLI args."""
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    ws = Workspace(
        name="broken", project="proj", repo_path="/tmp/repo",
        state="error", container_id="dead-ctr",
    )
    registry.create_workspace(ws)
    registry.update_state("broken", "error")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "broken"],
        obj={"registry": registry, "output": Output(force_json=True), "dry_run": False},
    )

    assert result.exit_code != 0
    err = json.loads(result.output.strip())
    assert err["error"] == "workspace_in_error_state"
    assert "--force" in err["fix"]
    registry.close()


# Thin-runtime/thick-volumes contract: --force rebuilds the container but
# MUST NOT touch project-declared named volumes. Regressing this silently
# wipes user data on what's advertised as a non-destructive recreate.
@patch("drydock.cli.create.DevcontainerCLI")
def test_force_rebuild_preserves_named_volumes(MockCLI, tmp_path, monkeypatch):
    """Catches --force silently removing project-declared named volumes
    (blocks the v2 daemon from regressing the thick-volumes contract)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    volume_name = "foo-data"
    monkeypatch.setattr(
        "drydock.cli.create.load_project_config",
        lambda project: ProjectConfig(
            repo_path=str(repo),
            extra_mounts=[f"source={volume_name},target=/x,type=volume"],
        ),
    )

    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    checkout_dir = tmp_path / ".drydock" / "worktrees" / "ws_volws"
    subprocess.run(
        ["git", "clone", str(repo), str(checkout_dir)],
        capture_output=True, text=True, check=True,
    )

    ws = Workspace(
        name="volws", project="proj", repo_path=str(repo),
        worktree_path=str(checkout_dir), branch="ws/volws",
        state="running", container_id="old-ctr",
    )
    registry.create_workspace(ws)
    registry.update_state("volws", "running")

    mock_devc = MockCLI.return_value
    mock_devc.up.return_value = {"container_id": "new-ctr-789", "containerId": "new-ctr-789"}

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "create", "proj", "volws", "--repo-path", str(repo), "--force"],
    )
    assert result.exit_code == 0, result.output

    # No devcontainer CLI method should remove volumes — only stop and remove
    # (the container) are legitimate during force-rebuild.
    assert not any(
        name.startswith("volume") or "volume" in name.lower()
        for name, _, _ in mock_devc.mock_calls
    ), f"Unexpected volume-related CLI call: {mock_devc.mock_calls}"

    # Belt-and-suspenders: inspect the actual docker invocations that stop()
    # and remove() would issue, to make sure no --volumes flag creeps in.
    with patch("drydock.core.devcontainer.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        from drydock.core.devcontainer import DevcontainerCLI
        real_devc = DevcontainerCLI()
        real_devc.stop(container_id="old-ctr")
        real_devc.remove(container_id="old-ctr")
        _assert_no_volume_rm(mock_run.call_args_list, volume_name)

    registry.close()


# Same contract on the destroy path: ws destroy tears down the container
# and worktree but MUST leave project-declared named volumes intact.
@patch("drydock.cli.destroy.DevcontainerCLI")
def test_destroy_preserves_named_volumes(MockCLI, tmp_path, monkeypatch):
    """Catches ws destroy removing named volumes — v2 daemon regression guard."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    checkout_dir = tmp_path / ".drydock" / "worktrees" / "ws_destws"
    subprocess.run(
        ["git", "clone", str(repo), str(checkout_dir)],
        capture_output=True, text=True, check=True,
    )

    volume_name = "foo-data"
    overlay_dir = tmp_path / ".drydock" / "overlays"
    overlay_dir.mkdir(parents=True)
    overlay_file = overlay_dir / "destws.json"
    overlay_file.write_text(
        json.dumps({"mounts": [f"source={volume_name},target=/x,type=volume"]})
    )

    ws = Workspace(
        name="destws", project="proj", repo_path=str(repo),
        worktree_path=str(checkout_dir), branch="ws/destws",
        state="running", container_id="live-ctr",
        config={"overlay_path": str(overlay_file)},
    )
    registry.create_workspace(ws)
    registry.update_state("destws", "running")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "destroy", "destws", "--force"],
    )
    assert result.exit_code == 0, result.output

    mock_devc = MockCLI.return_value
    # Destroy path must not call any volume-removal method on the CLI wrapper.
    assert not any(
        name.startswith("volume") or "volume" in name.lower()
        for name, _, _ in mock_devc.mock_calls
    ), f"Unexpected volume-related CLI call during destroy: {mock_devc.mock_calls}"

    # And the underlying docker invocations from stop()/remove() must not
    # carry --volumes or issue `docker volume rm` against project volumes.
    with patch("drydock.core.devcontainer.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        from drydock.core.devcontainer import DevcontainerCLI
        real_devc = DevcontainerCLI()
        real_devc.stop(container_id="live-ctr")
        real_devc.remove(container_id="live-ctr")
        _assert_no_volume_rm(mock_run.call_args_list, volume_name)

    registry.close()
