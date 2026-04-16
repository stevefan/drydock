"""Tests for the file-backed SecretsBackend (Slice 3a)."""

import os
import pytest

from drydock.core.secrets import (
    BackendPermissionDenied,
    FileBackend,
    SecretsBackend,
    build_backend,
)


class TestFileBackend:
    def test_protocol_compliance(self, tmp_path):
        backend = FileBackend(root=tmp_path)
        # Runtime Protocol check — catches signature regressions in V2.0
        # if a future refactor drops or renames a method.
        assert isinstance(backend, SecretsBackend)
        assert backend.name == "file"
        assert backend.supports_rotation() is False
        assert backend.rotate("anything") is None

    def test_fetch_returns_bytes_when_present(self, tmp_path):
        desk_dir = tmp_path / "ws_alpha"
        desk_dir.mkdir()
        (desk_dir / "anthropic_api_key").write_bytes(b"sk-ant-test\n")
        backend = FileBackend(root=tmp_path)

        assert backend.fetch("anthropic_api_key", "ws_alpha") == b"sk-ant-test\n"

    def test_fetch_returns_none_when_missing(self, tmp_path):
        backend = FileBackend(root=tmp_path)
        assert backend.fetch("missing", "ws_alpha") is None

    # 0o000 perms simulate the case where ws secret set ran as a
    # different user; ensures the backend distinguishes "missing" from
    # "unreadable" so the RPC layer can return a useful error.
    def test_fetch_permission_denied_raises(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root bypasses POSIX file mode checks")
        desk_dir = tmp_path / "ws_alpha"
        desk_dir.mkdir()
        path = desk_dir / "secret"
        path.write_bytes(b"hidden")
        os.chmod(path, 0o000)
        try:
            backend = FileBackend(root=tmp_path)
            with pytest.raises(BackendPermissionDenied):
                backend.fetch("secret", "ws_alpha")
        finally:
            os.chmod(path, 0o600)


class TestBuildBackend:
    def test_file_backend(self, tmp_path):
        backend = build_backend("file", secrets_root=tmp_path)
        assert isinstance(backend, FileBackend)
        assert backend.root == tmp_path

    # The wsd.toml loader translates this ValueError to an
    # `unknown_secrets_backend` RPC error per capability-broker.md §7.
    def test_unknown_backend_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unknown_secrets_backend"):
            build_backend("1password", secrets_root=tmp_path)
