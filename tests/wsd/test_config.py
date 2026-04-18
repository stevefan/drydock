"""Tests for wsd.toml loader (Slice 3d)."""

import pytest

from drydock.wsd.config import ConfigError, WsdConfig, load_wsd_config


class TestLoadWsdConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = load_wsd_config(tmp_path / "nope.toml")
        assert cfg == WsdConfig(secrets_backend="file")

    def test_empty_file_returns_defaults(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text("")
        assert load_wsd_config(path).secrets_backend == "file"

    def test_explicit_file_backend(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text('[secrets]\nbackend = "file"\n')
        assert load_wsd_config(path).secrets_backend == "file"

    # Unknown backends must fail at startup so the daemon never serves
    # a misconfigured RequestCapability.
    def test_unknown_backend_raises_with_clear_message(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text('[secrets]\nbackend = "1password"\n')
        with pytest.raises(ConfigError, match="unknown_secrets_backend"):
            load_wsd_config(path)

    def test_malformed_toml_raises(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text("not = valid = toml")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_wsd_config(path)

    def test_non_string_backend_rejected(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text("[secrets]\nbackend = 42\n")
        with pytest.raises(ConfigError, match="non-empty string"):
            load_wsd_config(path)


# V4 Phase 1: [storage] section governs STORAGE_MOUNT lease issuance.
# Missing section → None → daemon rejects STORAGE_MOUNT with
# storage_backend_not_configured. Present but misconfigured → fail fast
# at daemon startup, never mid-RPC.
class TestStorageConfig:
    def test_missing_storage_section_leaves_none(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text('[secrets]\nbackend = "file"\n')
        cfg = load_wsd_config(path)
        assert cfg.storage_backend is None
        assert cfg.storage_role_arn is None

    def test_sts_backend_parsed(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text(
            '[storage]\n'
            'backend = "sts"\n'
            'role_arn = "arn:aws:iam::123:role/drydock-agent"\n'
            'source_profile = "drydock-runner"\n'
            'session_duration_seconds = 7200\n'
        )
        cfg = load_wsd_config(path)
        assert cfg.storage_backend == "sts"
        assert cfg.storage_role_arn == "arn:aws:iam::123:role/drydock-agent"
        assert cfg.storage_source_profile == "drydock-runner"
        assert cfg.storage_session_duration_seconds == 7200

    def test_stub_backend_accepted(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text('[storage]\nbackend = "stub"\n')
        cfg = load_wsd_config(path)
        assert cfg.storage_backend == "stub"

    def test_sts_without_role_arn_rejected(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text('[storage]\nbackend = "sts"\n')
        with pytest.raises(ConfigError, match="role_arn"):
            load_wsd_config(path)

    def test_unknown_storage_backend_rejected(self, tmp_path):
        path = tmp_path / "wsd.toml"
        path.write_text('[storage]\nbackend = "gcs"\n')
        with pytest.raises(ConfigError, match="unknown_storage_backend"):
            load_wsd_config(path)
