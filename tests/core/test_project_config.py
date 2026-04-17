"""Tests for per-project YAML configuration."""

import pytest

from drydock.core import WsError
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

    def test_workspace_subdir_wrong_type_passthrough(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("workspace_subdir: 42\n")
        cfg = load_project_config("bad", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.workspace_subdir == 42

    def test_devcontainer_subpath_present(self, tmp_path):
        (tmp_path / "variant.yaml").write_text(
            'devcontainer_subpath: ".devcontainer/drydock"\n'
        )
        cfg = load_project_config("variant", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.devcontainer_subpath == ".devcontainer/drydock"

    def test_devcontainer_subpath_missing_is_none(self, tmp_path):
        (tmp_path / "variant.yaml").write_text("repo_path: /srv/code/app\n")
        cfg = load_project_config("variant", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.devcontainer_subpath is None

    def test_secrets_source_rejected_as_unknown(self, tmp_path):
        (tmp_path / "sec.yaml").write_text("repo_path: /code/x\nsecrets_source: vault\n")
        with pytest.raises(WsError) as exc_info:
            load_project_config("sec", base_dir=tmp_path)
        assert "secrets_source" in str(exc_info.value)

    def test_forward_ports_parses_int_list(self, tmp_path):
        (tmp_path / "fp.yaml").write_text(
            "forward_ports:\n  - 8000\n  - 3000\n"
        )
        cfg = load_project_config("fp", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.forward_ports == [8000, 3000]

    def test_forward_ports_rejects_strings(self, tmp_path):
        (tmp_path / "bad.yaml").write_text(
            "forward_ports:\n  - '8080:80'\n"
        )
        cfg = load_project_config("bad", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.forward_ports == ["8080:80"]

    def test_extra_mounts_parses_string_list(self, tmp_path):
        (tmp_path / "mnt.yaml").write_text(
            "extra_mounts:\n"
            "  - 'source=/data,target=/data,type=bind'\n"
            "  - 'source=vol,target=/vol,type=volume'\n"
        )
        cfg = load_project_config("mnt", base_dir=tmp_path)
        assert cfg is not None
        assert len(cfg.extra_mounts) == 2
        assert "source=/data" in cfg.extra_mounts[0]

    def test_claude_profile_parsed(self, tmp_path):
        (tmp_path / "prof.yaml").write_text("claude_profile: staging\n")
        cfg = load_project_config("prof", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.claude_profile == "staging"

    # Unknown keys are rejected so typos don't silently become no-ops
    def test_unknown_keys_rejected(self, tmp_path):
        (tmp_path / "typo.yaml").write_text("repo_path: /code/x\ntypo_field: oops\n")
        with pytest.raises(WsError) as exc_info:
            load_project_config("typo", base_dir=tmp_path)
        assert "typo_field" in str(exc_info.value)
        assert exc_info.value.fix is not None

    # Regression: V2 delegation fields must flow through the YAML loader.
    # Daemon's CreateDesk handler already accepts these; loader has to too.
    def test_v2_delegation_fields_parsed(self, tmp_path):
        (tmp_path / "emp.yaml").write_text(
            "repo_path: /srv/infra\n"
            "capabilities:\n"
            "  - request_secret_leases\n"
            "  - spawn_children\n"
            "secret_entitlements:\n"
            "  - anthropic_api_key\n"
            "delegatable_secrets:\n"
            "  - claude_credentials\n"
            "  - anthropic_api_key\n"
            "delegatable_firewall_domains:\n"
            "  - api.anthropic.com\n"
        )
        cfg = load_project_config("emp", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.capabilities == ["request_secret_leases", "spawn_children"]
        assert cfg.secret_entitlements == ["anthropic_api_key"]
        assert cfg.delegatable_secrets == ["claude_credentials", "anthropic_api_key"]
        assert cfg.delegatable_firewall_domains == ["api.anthropic.com"]
