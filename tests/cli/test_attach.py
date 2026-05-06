import json
import subprocess
from unittest.mock import patch

from click.testing import CliRunner

from drydock.cli.main import cli


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    (path / ".devcontainer").mkdir()
    (path / ".devcontainer" / "devcontainer.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)


def _create_drydock(tmp_path, monkeypatch, overlay_content="{}", state="running"):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    overlay_dir = tmp_path / "overlay"
    overlay_dir.mkdir()
    overlay_file = overlay_dir / "devcontainer.json"
    overlay_file.write_text(overlay_content)

    runner = CliRunner()
    with patch("drydock.cli.create.DevcontainerCLI") as MockCLI:
        MockCLI.return_value.up.return_value = {"container_id": "ctr-test", "containerId": "ctr-test"}
        runner.invoke(cli, ["create", "proj", "test-ws", "--repo-path", str(repo)])

    from drydock.core.registry import Registry
    reg = Registry()
    reg.update_drydock("test-ws", config={"overlay_path": str(overlay_file)})
    if state != "running":
        reg.update_state("test-ws", state)
    reg.close()

    return runner


def test_attach_drydock_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["attach", "nonexistent"])
    assert result.exit_code == 1


def test_attach_drydock_not_running(tmp_path, monkeypatch):
    runner = _create_drydock(tmp_path, monkeypatch, state="suspended")
    result = runner.invoke(cli, ["attach", "test-ws"])
    assert result.exit_code == 1


@patch("drydock.cli.attach._find_container", return_value="")
def test_attach_missing_container(mock_find, tmp_path, monkeypatch):
    runner = _create_drydock(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["attach", "test-ws"])
    assert result.exit_code == 1


def test_attach_missing_editor(tmp_path, monkeypatch):
    runner = _create_drydock(tmp_path, monkeypatch)
    with patch("drydock.cli.attach._find_container", return_value="silly_spence"), \
         patch("drydock.cli.attach.shutil.which", return_value=None):
        result = runner.invoke(cli, ["attach", "test-ws"])
    assert result.exit_code == 1
    assert "--editor" in (result.output + (result.stderr or ""))


def test_attach_happy_path(tmp_path, monkeypatch):
    runner = _create_drydock(tmp_path, monkeypatch)
    with patch("drydock.cli.attach._find_container", return_value="silly_spence"), \
         patch("drydock.cli.attach.shutil.which", return_value="/usr/bin/code"), \
         patch("drydock.cli.attach.subprocess.Popen") as mock_popen:
        result = runner.invoke(cli, ["--json", "attach", "test-ws"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    expected_hex = "".join(f"{b:02x}" for b in b"silly_spence")
    assert data["uri"] == f"vscode-remote://attached-container+{expected_hex}/drydock"
    assert data["editor"] == "code"
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert args[0] == "code"
    assert args[1] == "--folder-uri"


def test_attach_custom_workspace_folder(tmp_path, monkeypatch):
    overlay = json.dumps({"drydockFolder": "/custom/path"})
    runner = _create_drydock(tmp_path, monkeypatch, overlay_content=overlay)
    with patch("drydock.cli.attach._find_container", return_value="silly_spence"), \
         patch("drydock.cli.attach.shutil.which", return_value="/usr/bin/code"), \
         patch("drydock.cli.attach.subprocess.Popen"):
        result = runner.invoke(cli, ["--json", "attach", "test-ws"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "/custom/path" in data["uri"]


def test_attach_no_workspace_folder_defaults(tmp_path, monkeypatch):
    runner = _create_drydock(tmp_path, monkeypatch, overlay_content='{"image": "node"}')
    with patch("drydock.cli.attach._find_container", return_value="silly_spence"), \
         patch("drydock.cli.attach.shutil.which", return_value="/usr/bin/code"), \
         patch("drydock.cli.attach.subprocess.Popen"):
        result = runner.invoke(cli, ["--json", "attach", "test-ws"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["uri"].endswith("/drydock")
