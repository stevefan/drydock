"""Tests for per-project YAML configuration."""

from pathlib import Path

import pytest

from drydock.core.errors import WsError
from drydock.core.project_config import ProjectConfig, load_project_config


class TestLoadProjectConfig:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_project_config("nope", base_dir=tmp_path) is None

    def test_valid_yaml_full(self, tmp_path):
        (tmp_path / "app.yaml").write_text(
            "repo_path: /srv/code/app\n"
            "image: ghcr.io/acme/app:latest\n"
            "tailscale_hostname: app-dev\n"
            "tailscale_serve_port: 8080\n"
            "tailscale_authkey_env_var: TS_KEY_APP\n"
            "remote_control_name: App Agent\n"
            "firewall_extra_domains:\n"
            "  - api.stripe.com\n"
            "  - sentry.io\n"
            "firewall_ipv6_hosts:\n"
            "  - '[::1]:9090'\n"
        )
        cfg = load_project_config("app", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.repo_path == "/srv/code/app"
        assert cfg.image == "ghcr.io/acme/app:latest"
        assert cfg.tailscale_hostname == "app-dev"
        assert cfg.tailscale_serve_port == 8080
        assert cfg.tailscale_authkey_env_var == "TS_KEY_APP"
        assert cfg.remote_control_name == "App Agent"
        assert cfg.firewall_extra_domains == ["api.stripe.com", "sentry.io"]
        assert cfg.firewall_ipv6_hosts == ["[::1]:9090"]

    def test_partial_yaml_preserves_defaults(self, tmp_path):
        (tmp_path / "web.yaml").write_text("repo_path: /code/web\n")
        cfg = load_project_config("web", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.repo_path == "/code/web"
        assert cfg.image is None
        assert cfg.tailscale_hostname is None
        assert cfg.tailscale_serve_port is None
        assert cfg.firewall_extra_domains == []
        assert cfg.firewall_ipv6_hosts == []

    def test_empty_yaml_returns_empty_config(self, tmp_path):
        (tmp_path / "empty.yaml").write_text("")
        cfg = load_project_config("empty", base_dir=tmp_path)
        assert cfg is not None
        assert cfg == ProjectConfig()

    def test_invalid_yaml_raises_wserror_with_fix(self, tmp_path):
        (tmp_path / "bad.yaml").write_text(":\n  - :\n  bad: [")
        with pytest.raises(WsError) as exc_info:
            load_project_config("bad", base_dir=tmp_path)
        assert "Invalid YAML" in str(exc_info.value)
        assert exc_info.value.fix is not None
        assert "syntax" in exc_info.value.fix.lower()

    def test_non_mapping_yaml_raises_wserror(self, tmp_path):
        (tmp_path / "list.yaml").write_text("- one\n- two\n")
        with pytest.raises(WsError) as exc_info:
            load_project_config("list", base_dir=tmp_path)
        assert "mapping" in str(exc_info.value).lower()

    def test_workspace_subdir_present(self, tmp_path):
        (tmp_path / "mono.yaml").write_text(
            "repo_path: /srv/code/mono\nworkspace_subdir: apps/frontend\n"
        )
        cfg = load_project_config("mono", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.workspace_subdir == "apps/frontend"

    def test_workspace_subdir_absent_defaults_to_none(self, tmp_path):
        (tmp_path / "plain.yaml").write_text("repo_path: /srv/code/plain\n")
        cfg = load_project_config("plain", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.workspace_subdir is None

    def test_workspace_subdir_wrong_type_passthrough(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("workspace_subdir: 42\n")
        cfg = load_project_config("bad", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.workspace_subdir == 42

    def test_secrets_source_rejected_as_unknown(self, tmp_path):
        (tmp_path / "sec.yaml").write_text("repo_path: /code/x\nsecrets_source: vault\n")
        with pytest.raises(WsError) as exc_info:
            load_project_config("sec", base_dir=tmp_path)
        assert "secrets_source" in str(exc_info.value)

    # Unknown keys are rejected so typos don't silently become no-ops
    def test_unknown_keys_rejected(self, tmp_path):
        (tmp_path / "typo.yaml").write_text("repo_path: /code/x\ntypo_field: oops\n")
        with pytest.raises(WsError) as exc_info:
            load_project_config("typo", base_dir=tmp_path)
        assert "typo_field" in str(exc_info.value)
        assert exc_info.value.fix is not None
