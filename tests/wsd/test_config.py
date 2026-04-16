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
