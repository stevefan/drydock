"""Tests for ws project reload + ws overlay regenerate.

These close the YAML-drift papercut: after first create, editing project
YAML didn't reach the registry until sqlite surgery or --force recreate.
`ws project reload` re-reads YAML, updates registry config + V2 policy
columns, regenerates the overlay.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from drydock.cli.main import cli
from drydock.core.policy import CapabilityKind
from drydock.core.registry import Registry
from drydock.core.workspace import Workspace


def _init_repo(path: Path, with_devcontainer: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    if with_devcontainer:
        (path / ".devcontainer").mkdir()
        (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


def _seed(tmp_path, project_yaml: str) -> None:
    """Create a drydock at a known state + project YAML. Registry closed on return."""
    drydock_home = tmp_path / ".drydock"
    drydock_home.mkdir()
    (drydock_home / "projects").mkdir()
    (drydock_home / "overlays").mkdir()
    worktree = tmp_path / "worktree"
    _init_repo(worktree)

    (drydock_home / "projects" / "myproj.yaml").write_text(project_yaml)

    registry = Registry(db_path=drydock_home / "registry.db")
    try:
        ws = Workspace(
            name="myws",
            project="myproj",
            repo_path=str(worktree),
            worktree_path=str(worktree),
            branch="ws/myws",
            state="suspended",
            container_id="cid_old",
            config={
                "overlay_path": str(drydock_home / "overlays" / "ws_myws.devcontainer.json"),
                "firewall_extra_domains": ["old.example.com"],
            },
        )
        registry.create_workspace(ws)
    finally:
        registry.close()


# Core contract: a YAML edit picks up on reload.
def test_project_reload_updates_firewall_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, (
        "repo_path: /srv/code/myproj\n"
        "firewall_extra_domains:\n"
        "  - new.example.com\n"
        "  - also-new.example.com\n"
    ))
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "project", "reload", "myws"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["registry_updated"] is True

    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    cfg = registry.get_workspace("myws").config
    assert cfg["firewall_extra_domains"] == ["new.example.com", "also-new.example.com"]
    registry.close()


# V2 policy columns (capabilities, delegatable_secrets, etc.) travel
# through a separate registry method — pin that they actually update.
def test_project_reload_updates_v2_policy_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, (
        "repo_path: /srv/code/myproj\n"
        "capabilities:\n"
        "  - request_secret_leases\n"
        "  - request_storage_leases\n"
        "delegatable_secrets:\n"
        "  - anthropic_api_key\n"
        "delegatable_storage_scopes:\n"
        "  - 's3://lab-data/*'\n"
    ))
    runner = CliRunner()
    result = runner.invoke(cli, ["project", "reload", "myws"])
    assert result.exit_code == 0, result.output

    registry = Registry(db_path=tmp_path / ".drydock" / "registry.db")
    policy = registry.load_desk_policy("ws_myws")
    assert policy is not None
    assert set(json.loads(policy["capabilities"])) == {
        "request_secret_leases", "request_storage_leases",
    }
    assert json.loads(policy["delegatable_secrets"]) == ["anthropic_api_key"]
    assert json.loads(policy["delegatable_storage_scopes"]) == ["s3://lab-data/*"]
    registry.close()


def test_project_reload_picks_up_extra_env(tmp_path, monkeypatch):
    """extra_env from YAML flows to registry config AND into the regenerated
    overlay's containerEnv. Enables AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE
    style redirects without per-feature YAML knobs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, (
        "repo_path: /srv/code/myproj\n"
        "extra_env:\n"
        "  AWS_CONFIG_FILE: /opt/aws-config/config\n"
        "  AWS_SHARED_CREDENTIALS_FILE: /opt/aws-config/credentials\n"
    ))
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "project", "reload", "myws"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    overlay = json.loads(Path(data["overlay_path"]).read_text())
    env = overlay.get("containerEnv", {})
    assert env.get("AWS_CONFIG_FILE") == "/opt/aws-config/config"
    assert env.get("AWS_SHARED_CREDENTIALS_FILE") == "/opt/aws-config/credentials"


def test_project_reload_regenerates_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, (
        "repo_path: /srv/code/myproj\n"
        "firewall_extra_domains:\n"
        "  - new.example.com\n"
    ))
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "project", "reload", "myws"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["overlay_regenerated"] is True
    overlay = json.loads(Path(data["overlay_path"]).read_text())
    # The new firewall domain is now in the container env string.
    assert "new.example.com" in overlay["containerEnv"]["FIREWALL_EXTRA_DOMAINS"]


def test_project_reload_no_regenerate_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, (
        "repo_path: /srv/code/myproj\n"
        "firewall_extra_domains:\n  - x.example.com\n"
    ))
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "project", "reload", "myws", "--no-regenerate"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["overlay_regenerated"] is False


def test_project_reload_unknown_drydock(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # no seed — registry empty
    (tmp_path / ".drydock" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "project", "reload", "nope"])
    assert result.exit_code != 0
    err = json.loads(result.output.strip())
    assert err["error"] == "desk_not_found"


def test_project_reload_yaml_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, "repo_path: /srv/code/myproj\n")
    # delete the yaml
    (tmp_path / ".drydock" / "projects" / "myproj.yaml").unlink()
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "project", "reload", "myws"])
    assert result.exit_code != 0
    err = json.loads(result.output.strip())
    assert err["error"] == "project_yaml_not_found"


# ws overlay regenerate — a narrower version; no YAML re-read, just rewrite.
def test_overlay_regenerate_writes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, "repo_path: /srv/code/myproj\n")

    overlay_path = tmp_path / ".drydock" / "overlays" / "ws_myws.devcontainer.json"
    assert not overlay_path.exists()

    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "overlay", "regenerate", "myws"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["overlay_path"] == str(overlay_path)
    assert overlay_path.exists()


def test_overlay_regenerate_unknown_drydock(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".drydock" / "projects").mkdir(parents=True)
    # Registry exists but has no rows
    Registry(db_path=tmp_path / ".drydock" / "registry.db").close()
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "overlay", "regenerate", "nope"])
    assert result.exit_code != 0
    err = json.loads(result.output.strip())
    assert err["error"] == "desk_not_found"
