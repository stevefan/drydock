"""Tests for ws new — devcontainer scaffolder."""

import json
from pathlib import Path

from click.testing import CliRunner

from drydock.cli.new import new
from drydock.output.formatter import Output


def _invoke(tmp_path, *args, dry_run=False, projects_dir: Path | None = None,
            monkeypatch=None):
    if projects_dir is not None and monkeypatch is not None:
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", projects_dir)
    runner = CliRunner()
    return runner.invoke(
        new, list(args),
        obj={"output": Output(force_json=True), "dry_run": dry_run},
    )


class TestNew:
    def test_scaffolds_with_substitution(self, tmp_path, monkeypatch):
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", tmp_path / "projects")
        result = _invoke(tmp_path, "myproj", "--repo-path", str(tmp_path),
                         "--base-tag", "v1.0.7", "--no-write-project-yaml")
        assert result.exit_code == 0, result.output

        df = tmp_path / ".devcontainer" / "drydock" / "Dockerfile"
        dc = tmp_path / ".devcontainer" / "drydock" / "devcontainer.json"
        assert df.exists()
        assert dc.exists()
        assert "FROM ghcr.io/stevefan/drydock-base:v1.0.7" in df.read_text()
        # devcontainer.json must parse and substitute project_name
        dc_data = json.loads(dc.read_text())
        assert dc_data["name"] == "myproj"
        assert "myproj" in dc_data["containerEnv"]["TAILSCALE_HOSTNAME"]

    def test_refuses_overwrite_without_force(self, tmp_path, monkeypatch):
        # Pre-create the file
        drydock_dir = tmp_path / ".devcontainer" / "drydock"
        drydock_dir.mkdir(parents=True)
        (drydock_dir / "Dockerfile").write_text("# existing\n")

        result = _invoke(tmp_path, "p", "--repo-path", str(tmp_path),
                         "--no-write-project-yaml")
        assert result.exit_code == 1
        # Still has original
        assert (drydock_dir / "Dockerfile").read_text() == "# existing\n"

    def test_force_overwrites(self, tmp_path, monkeypatch):
        drydock_dir = tmp_path / ".devcontainer" / "drydock"
        drydock_dir.mkdir(parents=True)
        (drydock_dir / "Dockerfile").write_text("# existing\n")

        result = _invoke(tmp_path, "p", "--repo-path", str(tmp_path),
                         "--no-write-project-yaml", "--force")
        assert result.exit_code == 0, result.output
        assert "FROM ghcr.io/stevefan/drydock-base" in (drydock_dir / "Dockerfile").read_text()

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        result = _invoke(tmp_path, "p", "--repo-path", str(tmp_path),
                         "--no-write-project-yaml", dry_run=True)
        assert result.exit_code == 0
        assert not (tmp_path / ".devcontainer" / "drydock").exists()

    def test_writes_project_yaml(self, tmp_path, monkeypatch):
        projects_dir = tmp_path / "projects"
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", projects_dir)
        result = _invoke(tmp_path, "myproj", "--repo-path", str(tmp_path))
        assert result.exit_code == 0, result.output
        py = projects_dir / "myproj.yaml"
        assert py.exists()
        body = py.read_text()
        assert "devcontainer_subpath: .devcontainer/drydock" in body
        assert f"repo_path: {tmp_path.resolve()}" in body

    def test_skips_existing_project_yaml(self, tmp_path, monkeypatch):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        existing = projects_dir / "myproj.yaml"
        existing.write_text("custom_field: keep-me\n")
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", projects_dir)

        result = _invoke(tmp_path, "myproj", "--repo-path", str(tmp_path))
        assert result.exit_code == 0
        # Existing content preserved
        assert existing.read_text() == "custom_field: keep-me\n"
