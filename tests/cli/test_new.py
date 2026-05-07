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
        assert not (tmp_path / ".devcontainer" / "workspace").exists()

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


class TestNewAuditor:
    """Phase PA3.4: --role auditor uses the role-locked template +
    writes a project YAML that passes role_validator.

    The contract is end-to-end: scaffold + load + validate. If any
    of those break, the test surfaces it."""

    def test_scaffolds_auditor_devcontainer(self, tmp_path, monkeypatch):
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", tmp_path / "projects")
        result = _invoke(
            tmp_path, "port-auditor", "--repo-path", str(tmp_path),
            "--role", "auditor",
        )
        assert result.exit_code == 0, result.output
        dc = tmp_path / ".devcontainer" / "drydock" / "devcontainer.json"
        df = tmp_path / ".devcontainer" / "drydock" / "Dockerfile.example"
        assert dc.exists()
        assert df.exists()
        # Auditor template references the baked image directly, no build
        dc_data = json.loads(dc.read_text())
        assert "image" in dc_data
        assert "drydock-port-auditor:v0.1.0" in dc_data["image"]
        assert dc_data["name"] == "port-auditor"
        # Dockerfile.example is just an opt-in extension stub
        assert "FROM ghcr.io/stevefan/drydock-port-auditor" in df.read_text()
        assert "OPTIONAL" in df.read_text()

    def test_writes_role_locked_project_yaml(self, tmp_path, monkeypatch):
        from drydock.core.project_config import load_project_config
        from drydock.core.auditor.role_validator import validate_auditor_role
        projects_dir = tmp_path / "projects"
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", projects_dir)
        result = _invoke(
            tmp_path, "port-auditor", "--repo-path", str(tmp_path),
            "--role", "auditor",
        )
        assert result.exit_code == 0, result.output
        py = projects_dir / "port-auditor.yaml"
        assert py.exists()
        # End-to-end contract: load + validate
        cfg = load_project_config("port-auditor", base_dir=projects_dir)
        assert cfg is not None
        assert cfg.role == "auditor"
        v = validate_auditor_role(cfg)
        assert v.ok is True, [
            (vi.code, vi.message) for vi in v.violations
        ]

    def test_auditor_yaml_pins_image_version(self, tmp_path, monkeypatch):
        projects_dir = tmp_path / "projects"
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", projects_dir)
        result = _invoke(
            tmp_path, "pa", "--repo-path", str(tmp_path),
            "--role", "auditor", "--base-tag", "v0.2.0",
        )
        assert result.exit_code == 0, result.output
        py = projects_dir / "pa.yaml"
        body = py.read_text()
        assert "image: ghcr.io/stevefan/drydock-port-auditor:v0.2.0" in body
        # And the devcontainer.json got the same tag
        dc = tmp_path / ".devcontainer" / "drydock" / "devcontainer.json"
        assert "drydock-port-auditor:v0.2.0" in dc.read_text()

    def test_worker_role_unchanged_default(self, tmp_path, monkeypatch):
        """--role worker (default) preserves the existing scaffolding —
        worker desks aren't perturbed by the role addition."""
        monkeypatch.setattr("drydock.cli.new.PROJECTS_DIR", tmp_path / "projects")
        result = _invoke(
            tmp_path, "myproj", "--repo-path", str(tmp_path),
            "--no-write-project-yaml",
        )
        assert result.exit_code == 0, result.output
        df = tmp_path / ".devcontainer" / "drydock" / "Dockerfile"
        assert df.exists()
        # Worker still uses drydock-base (not drydock-port-auditor)
        assert "FROM ghcr.io/stevefan/drydock-base" in df.read_text()
