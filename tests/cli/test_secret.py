"""Tests for ws secret commands."""

import json
import os
import stat
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from drydock.cli.secret import secret
from drydock.core.workspace import Workspace
from drydock.output.formatter import Output


def _registry(ws=None):
    r = MagicMock()
    r.get_workspace.return_value = ws
    return r


def _make_ws():
    return Workspace(name="test-ws", project="proj", repo_path="/tmp/repo")


def _invoke(subcmd_args, registry=None, input=None):
    runner = CliRunner()
    out = Output(force_json=True)
    return runner.invoke(
        secret,
        subcmd_args,
        obj={"registry": registry or _registry(), "output": out, "dry_run": False},
        input=input,
    )


class TestSet:
    def test_writes_mode_400_file(self, tmp_path):
        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["set", "test-ws", "API_KEY"], registry=reg, input="s3cret")

        assert result.exit_code == 0
        data = json.loads(result.output)
        secret_path = tmp_path / "ws_test_ws" / "API_KEY"
        assert secret_path.read_text() == "s3cret"
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o400
        assert data["bytes"] == 6
        assert data["key"] == "API_KEY"
        assert "s3cret" not in json.dumps(data)

    def test_parent_dir_mode_700(self, tmp_path):
        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            _invoke(["set", "test-ws", "K"], registry=reg, input="v")

        parent = tmp_path / "ws_test_ws"
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700

    def test_warns_when_workspace_not_in_registry(self, tmp_path):
        reg = _registry(None)
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["set", "new-ws", "K"], registry=reg, input="v")

        assert result.exit_code == 0
        assert "warning" in result.stderr or "warning" in (result.output + (result.stderr or ""))
        assert (tmp_path / "ws_new_ws" / "K").exists()

    def test_empty_stdin_is_error(self, tmp_path):
        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["set", "test-ws", "K"], registry=reg, input="")

        assert result.exit_code == 1


class TestList:
    def test_returns_names_and_metadata(self, tmp_path):
        secret_dir = tmp_path / "ws_test_ws"
        secret_dir.mkdir(parents=True)
        f = secret_dir / "DB_PASS"
        f.write_text("hidden")
        os.chmod(f, 0o400)

        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["list", "test-ws"], registry=reg)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["keys"]) == 1
        entry = data["keys"][0]
        assert entry["name"] == "DB_PASS"
        assert entry["mode"] == "0o400"
        assert entry["size"] == 6
        assert "hidden" not in result.output

    def test_empty_dir_returns_empty_list_with_fix(self, tmp_path):
        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["list", "test-ws"], registry=reg)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["keys"] == []


class TestRm:
    def test_removes_file(self, tmp_path):
        secret_dir = tmp_path / "ws_test_ws"
        secret_dir.mkdir(parents=True)
        (secret_dir / "TOKEN").write_text("x")

        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["rm", "test-ws", "TOKEN", "--force"], registry=reg)

        assert result.exit_code == 0
        assert not (secret_dir / "TOKEN").exists()
        data = json.loads(result.output)
        assert data["removed"] is True

    def test_missing_file_is_idempotent(self, tmp_path):
        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["rm", "test-ws", "NOPE", "--force"], registry=reg)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["removed"] is False


class TestPush:
    @patch("drydock.cli.secret.subprocess.run")
    def test_constructs_expected_rsync_command(self, mock_run, tmp_path):
        secret_dir = tmp_path / "ws_test_ws"
        secret_dir.mkdir(parents=True)
        (secret_dir / "K").write_text("v")

        reg = _registry(_make_ws())
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = _invoke(["push", "test-ws", "--to", "host1"], registry=reg)

        assert result.exit_code == 0
        assert mock_run.call_count == 2
        mkdir_call = mock_run.call_args_list[0]
        rsync_call = mock_run.call_args_list[1]
        assert "ssh" in mkdir_call[0][0]
        assert "host1" in mkdir_call[0][0]
        assert "rsync" in rsync_call[0][0]
        assert "-a" in rsync_call[0][0]
        assert any("host1:" in arg for arg in rsync_call[0][0])

    def test_dry_run_does_not_execute(self, tmp_path):
        secret_dir = tmp_path / "ws_test_ws"
        secret_dir.mkdir(parents=True)
        (secret_dir / "K").write_text("v")

        reg = _registry(_make_ws())
        runner = CliRunner()
        out = Output(force_json=True)
        with patch("drydock.cli.secret._secrets_root", return_value=tmp_path):
            result = runner.invoke(
                secret,
                ["push", "test-ws", "--to", "host1"],
                obj={"registry": reg, "output": out, "dry_run": True},
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert "rsync" in str(data["rsync_cmd"])
