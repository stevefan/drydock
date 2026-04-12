import json

from click.testing import CliRunner

from drydock.cli.main import cli


def test_list_json_output():
    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0


def test_create_and_list_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(cli, ["create", "testproj", "my-ws"])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["--json", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["name"] == "my-ws"
    assert data[0]["project"] == "testproj"


def test_create_dry_run():
    runner = CliRunner()
    result = runner.invoke(cli, ["--dry-run", "create", "proj", "ws1"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["dry_run"] is True


def test_destroy_requires_force(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(cli, ["create", "proj", "ws1"])
    result = runner.invoke(cli, ["destroy", "ws1"])
    assert result.exit_code == 1


def test_inspect_not_found():
    runner = CliRunner()
    result = runner.invoke(cli, ["inspect", "nonexistent"])
    assert result.exit_code == 1
