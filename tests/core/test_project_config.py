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

    # Phase 1b: per-bucket narrowness for STORAGE_MOUNT. YAML passthrough.
    def test_delegatable_storage_scopes_parsed(self, tmp_path):
        (tmp_path / "lab.yaml").write_text(
            "repo_path: /srv/lab\n"
            "capabilities:\n"
            "  - request_storage_leases\n"
            "delegatable_storage_scopes:\n"
            "  - 's3://lab-data/scraped/*'\n"
            "  - 'rw:s3://lab-data/output/*'\n"
        )
        cfg = load_project_config("lab", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.delegatable_storage_scopes == [
            "s3://lab-data/scraped/*",
            "rw:s3://lab-data/output/*",
        ]

    # extra_env passthrough: general containerEnv knob for project YAML.
    # Motivating case: pointing AWS_CONFIG_FILE at a readonly bind-mount
    # so the CLI cache dir stays writable in a separate path.
    def test_extra_env_parsed(self, tmp_path):
        (tmp_path / "infra.yaml").write_text(
            "repo_path: /srv/infra\n"
            "extra_env:\n"
            "  AWS_CONFIG_FILE: /opt/aws-config/config\n"
            "  AWS_SHARED_CREDENTIALS_FILE: /opt/aws-config/credentials\n"
        )
        cfg = load_project_config("infra", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.extra_env == {
            "AWS_CONFIG_FILE": "/opt/aws-config/config",
            "AWS_SHARED_CREDENTIALS_FILE": "/opt/aws-config/credentials",
        }

    def test_extra_env_defaults_empty(self, tmp_path):
        (tmp_path / "x.yaml").write_text("repo_path: /srv/x\n")
        cfg = load_project_config("x", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.extra_env == {}

    def test_delegatable_storage_scopes_default_empty(self, tmp_path):
        # Regression: back-compat invariant. A project YAML without the
        # field must not raise and must produce an empty list, so existing
        # drydocks keep their default-permissive (capability-only) gate.
        (tmp_path / "old.yaml").write_text("repo_path: /srv/old\n")
        cfg = load_project_config("old", base_dir=tmp_path)
        assert cfg is not None
        assert cfg.delegatable_storage_scopes == []


# Phase C: storage_mounts declarative shorthand. One YAML block expands to
# capability + scope + firewall. Pin the expansion so regressions can't
# silently widen (missing request_storage_leases) or narrow (missing scope).
class TestStorageMountsExpansion:
    def _load(self, tmp_path, yaml_text):
        from drydock.core.project_config import load_project_config
        (tmp_path / "p.yaml").write_text(yaml_text)
        return load_project_config("p", base_dir=tmp_path)

    def test_adds_capability_scope_firewall(self, tmp_path):
        cfg = self._load(tmp_path, """
storage_mounts:
  - source: s3://my-bucket/data
    target: /mnt/data
    mode: ro
""")
        assert "request_storage_leases" in cfg.capabilities
        assert "s3://my-bucket/data/*" in cfg.delegatable_storage_scopes
        assert "us-west-2:AMAZON" in cfg.firewall_aws_ip_ranges

    def test_rw_mode_wraps_scope(self, tmp_path):
        cfg = self._load(tmp_path, """
storage_mounts:
  - source: s3://b/p
    target: /mnt/p
    mode: rw
""")
        assert "rw:s3://b/p/*" in cfg.delegatable_storage_scopes

    def test_explicit_region_respected(self, tmp_path):
        cfg = self._load(tmp_path, """
storage_mounts:
  - source: s3://b
    target: /mnt/b
    region: eu-central-1
""")
        assert "eu-central-1:AMAZON" in cfg.firewall_aws_ip_ranges

    def test_user_scopes_preserved_additive(self, tmp_path):
        # Explicit delegatable_storage_scopes survive expansion. Regression
        # guard: a user might declare extra scopes outside storage_mounts
        # for leases they'll request programmatically; expansion must not
        # clobber them.
        cfg = self._load(tmp_path, """
delegatable_storage_scopes:
  - s3://other-bucket/*
storage_mounts:
  - source: s3://my-bucket/data
    target: /mnt/data
""")
        assert "s3://other-bucket/*" in cfg.delegatable_storage_scopes
        assert "s3://my-bucket/data/*" in cfg.delegatable_storage_scopes

    def test_duplicate_mount_dedups(self, tmp_path):
        cfg = self._load(tmp_path, """
storage_mounts:
  - source: s3://b/p
    target: /mnt/a
  - source: s3://b/p
    target: /mnt/aa
""")
        assert cfg.delegatable_storage_scopes.count("s3://b/p/*") == 1
        assert cfg.firewall_aws_ip_ranges.count("us-west-2:AMAZON") == 1

    def test_rejects_non_s3_source(self, tmp_path):
        from drydock.core import WsError
        with pytest.raises(WsError, match="s3:// URL"):
            self._load(tmp_path, """
storage_mounts:
  - source: gs://bucket/data
    target: /mnt/x
""")

    def test_rejects_invalid_mode(self, tmp_path):
        from drydock.core import WsError
        with pytest.raises(WsError, match="must be 'ro' or 'rw'"):
            self._load(tmp_path, """
storage_mounts:
  - source: s3://b
    target: /mnt/b
    mode: admin
""")

    def test_rejects_non_absolute_target(self, tmp_path):
        from drydock.core import WsError
        with pytest.raises(WsError, match="absolute container path"):
            self._load(tmp_path, """
storage_mounts:
  - source: s3://b
    target: data
""")
