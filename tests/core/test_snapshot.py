"""Tests for `drydock.core.snapshot` — Phase 2a.4 M1.

Pin the contract:
- Manifest serialization round-trip (the wire/storage contract).
- snapshot_drydock captures registry row + secrets dir + overlay file.
- Worktree state read from a real git worktree; clean and dirty cases.
- SnapshotDirtyWorktreeError when refuse_dirty_worktree=True and worktree dirty.
- Volumes path is mocked (we don't run docker in unit tests); contract
  is "tar each named volume named in volume_names".
- restore reverses secrets + overlay; volume restore is mocked.
- Path-traversal guard rejects malicious tarballs.
- Cleanup removes the snapshot directory.
"""
from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from drydock.core.registry import Registry
from drydock.core.runtime import Drydock
from drydock.core.snapshot import (
    DEFAULT_MIGRATIONS_ROOT,
    SNAPSHOT_VERSION,
    SnapshotDirtyWorktreeError,
    SnapshotError,
    SnapshotManifest,
    VolumeRef,
    WorktreeState,
    cleanup_snapshot,
    load_manifest,
    restore_drydock,
    snapshot_drydock,
)


# ---------------------------------------------------------------------------
# Manifest round-trip
# ---------------------------------------------------------------------------


class TestManifestRoundTrip:
    def test_minimal_manifest(self):
        m = SnapshotManifest(
            version=1,
            migration_id="mig_x",
            drydock_id="dock_x",
            drydock_name="x",
            captured_at="2026-05-07T08:00:00Z",
            registry_row={"drydocks": {"id": "dock_x"}},
        )
        round_tripped = SnapshotManifest.from_dict(m.to_dict())
        assert round_tripped.migration_id == "mig_x"
        assert round_tripped.worktree is None
        assert round_tripped.volumes == []

    def test_full_manifest_with_worktree_and_volumes(self):
        m = SnapshotManifest(
            version=1,
            migration_id="mig_x",
            drydock_id="dock_x",
            drydock_name="x",
            captured_at="2026-05-07T08:00:00Z",
            registry_row={"drydocks": {}},
            secrets_path_in_tarball="secrets/dock_x",
            overlay_path_in_tarball="overlay.devcontainer.json",
            worktree=WorktreeState(
                branch="ws/x",
                commit_sha="abc123",
                repo_path="/r",
                is_dirty=False,
            ),
            volumes=[
                VolumeRef(name="claude-code-config", archive_path_in_tarball="volumes/claude-code-config.tar.gz", size_bytes=1024),
            ],
            bytes_total=2048,
        )
        rt = SnapshotManifest.from_dict(m.to_dict())
        assert rt.worktree.branch == "ws/x"
        assert rt.worktree.commit_sha == "abc123"
        assert rt.volumes[0].name == "claude-code-config"
        assert rt.volumes[0].size_bytes == 1024


# ---------------------------------------------------------------------------
# Snapshot — capture
# ---------------------------------------------------------------------------


def _seed_drydock(tmp_path):
    """Return (drydock, registry, paths) for a working snapshot test setup."""
    secrets_root = tmp_path / "secrets"
    overlays_root = tmp_path / "overlays"
    migrations_root = tmp_path / "migrations"
    secrets_root.mkdir(parents=True)
    overlays_root.mkdir(parents=True)
    migrations_root.mkdir(parents=True)

    r = Registry(db_path=tmp_path / "r.db")
    ws = Drydock(name="test", project="test", repo_path="/r")
    r.create_drydock(ws)

    # Seed a secrets dir
    sdir = secrets_root / ws.id
    sdir.mkdir(parents=True)
    (sdir / "drydock-token").write_text("token-bytes")
    (sdir / "drydock-token").chmod(0o400)

    # Seed an overlay file
    overlay_file = overlays_root / f"{ws.id}.devcontainer.json"
    overlay_file.write_text('{"name": "test"}')

    return {
        "ws": ws, "registry": r,
        "secrets_root": secrets_root, "overlays_root": overlays_root,
        "migrations_root": migrations_root,
    }


class TestSnapshotBasics:
    def test_captures_registry_row_secrets_overlay(self, tmp_path):
        env = _seed_drydock(tmp_path)
        snapshot_dir, manifest = snapshot_drydock(
            env["ws"], migration_id="mig_a", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )
        assert (snapshot_dir / "snapshot.tgz").is_file()
        assert (snapshot_dir / "manifest.json").is_file()
        assert manifest.version == SNAPSHOT_VERSION
        assert manifest.drydock_id == env["ws"].id
        assert manifest.secrets_path_in_tarball == f"secrets/{env['ws'].id}"
        assert manifest.overlay_path_in_tarball == "overlay.devcontainer.json"
        # Registry row contains the drydocks row
        assert manifest.registry_row["drydocks"]["name"] == "test"

    def test_manifest_is_loadable(self, tmp_path):
        env = _seed_drydock(tmp_path)
        snapshot_dir, manifest = snapshot_drydock(
            env["ws"], migration_id="mig_b", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )
        loaded = load_manifest(snapshot_dir)
        assert loaded.migration_id == manifest.migration_id
        assert loaded.drydock_id == manifest.drydock_id

    def test_no_secrets_dir_results_in_none_path(self, tmp_path):
        env = _seed_drydock(tmp_path)
        # Remove the secrets dir we seeded.
        import shutil
        shutil.rmtree(env["secrets_root"] / env["ws"].id)
        _, manifest = snapshot_drydock(
            env["ws"], migration_id="mig_c", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )
        assert manifest.secrets_path_in_tarball is None

    def test_secrets_mode_bits_preserved_in_tar(self, tmp_path):
        """0400 file modes matter (secret bind-mount semantic). Confirm
        the tar preserves them."""
        env = _seed_drydock(tmp_path)
        snapshot_dir, manifest = snapshot_drydock(
            env["ws"], migration_id="mig_d", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )
        # Read the tarball, find the secret file, check mode.
        with tarfile.open(snapshot_dir / "snapshot.tgz", "r:gz") as tf:
            members = {m.name: m for m in tf.getmembers()}
            secret_member = members.get(f"secrets/{env['ws'].id}/drydock-token")
            assert secret_member is not None
            # mode is the lower 9 bits (rwxrwxrwx) — 0o400
            assert secret_member.mode & 0o777 == 0o400

    def test_drydock_not_in_registry_raises(self, tmp_path):
        env = _seed_drydock(tmp_path)
        bogus = Drydock(name="ghost", project="ghost", repo_path="/r")
        # Don't insert into registry — capture should fail.
        with pytest.raises(SnapshotError) as exc:
            snapshot_drydock(
                bogus, migration_id="mig_e", registry=env["registry"],
                secrets_root=env["secrets_root"],
                overlays_root=env["overlays_root"],
                migrations_root=env["migrations_root"],
                capture_volumes=False,
            )
        assert "not found in registry" in str(exc.value)


# ---------------------------------------------------------------------------
# Worktree state
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> str:
    """Create a git repo with one commit and return the commit SHA."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


class TestWorktreeCapture:
    def test_clean_worktree_recorded(self, tmp_path):
        env = _seed_drydock(tmp_path)
        worktree = tmp_path / "worktree"
        sha = _init_repo(worktree)
        env["ws"] = Drydock(
            name="test2", project="test2", repo_path=str(worktree),
            worktree_path=str(worktree),
        )
        env["registry"].create_drydock(env["ws"])
        # Re-create the secrets dir for new desk
        (env["secrets_root"] / env["ws"].id).mkdir()

        _, manifest = snapshot_drydock(
            env["ws"], migration_id="mig_w1", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )
        assert manifest.worktree is not None
        assert manifest.worktree.commit_sha == sha
        assert manifest.worktree.is_dirty is False
        assert manifest.worktree.dirty_files == []

    def test_dirty_worktree_refused(self, tmp_path):
        env = _seed_drydock(tmp_path)
        worktree = tmp_path / "worktree"
        _init_repo(worktree)
        # Add an uncommitted edit
        (worktree / "README.md").write_text("modified")

        env["ws"] = Drydock(
            name="test3", project="test3", repo_path=str(worktree),
            worktree_path=str(worktree),
        )
        env["registry"].create_drydock(env["ws"])
        (env["secrets_root"] / env["ws"].id).mkdir()

        with pytest.raises(SnapshotDirtyWorktreeError) as exc:
            snapshot_drydock(
                env["ws"], migration_id="mig_w2", registry=env["registry"],
                secrets_root=env["secrets_root"],
                overlays_root=env["overlays_root"],
                migrations_root=env["migrations_root"],
                capture_volumes=False,
            )
        assert "uncommitted change" in str(exc.value)
        assert any("README.md" in f for f in exc.value.dirty_files)

    def test_dirty_worktree_captured_when_refuse_false(self, tmp_path):
        """Force-mode: dirty worktree captured with the dirty-flag set."""
        env = _seed_drydock(tmp_path)
        worktree = tmp_path / "worktree"
        _init_repo(worktree)
        (worktree / "README.md").write_text("modified")

        env["ws"] = Drydock(
            name="test4", project="test4", repo_path=str(worktree),
            worktree_path=str(worktree),
        )
        env["registry"].create_drydock(env["ws"])
        (env["secrets_root"] / env["ws"].id).mkdir()

        _, manifest = snapshot_drydock(
            env["ws"], migration_id="mig_w3", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
            refuse_dirty_worktree=False,
        )
        assert manifest.worktree.is_dirty is True
        assert len(manifest.worktree.dirty_files) >= 1


# ---------------------------------------------------------------------------
# Volumes (mocked docker)
# ---------------------------------------------------------------------------


class TestVolumeCapture:
    def test_volume_present_is_captured(self, tmp_path):
        env = _seed_drydock(tmp_path)
        # Mock subprocess.run for docker volume inspect (return ok)
        # AND for the docker run tar invocation.
        def _mock_run(cmd, **kw):
            res = MagicMock()
            res.returncode = 0
            res.stdout = "{}"
            res.stderr = ""
            # If this is the docker run tar invocation, write a real tar
            # to the host-mounted file so size > 0.
            if cmd[1:3] == ["run", "--rm"]:
                # Find the /host:... mount and the target tar name
                target_arg = next(a for a in cmd if "/host/" in a)
                tar_in_container = target_arg  # e.g. "/host/foo.tar.gz"
                tar_name = Path(tar_in_container).name
                # Find the host bind from "-v <host>:/host"
                host_mount = next(
                    a for i, a in enumerate(cmd)
                    if i > 0 and cmd[i-1] == "-v" and ":/host" in a
                )
                host_dir = host_mount.split(":")[0]
                # Write a tiny tar so the test assertion sees size > 0.
                target = Path(host_dir) / tar_name
                with tarfile.open(target, "w:gz") as tf:
                    info = tarfile.TarInfo(name="hello.txt")
                    info.size = 5
                    import io
                    tf.addfile(info, io.BytesIO(b"hello"))
            return res

        with patch("drydock.core.snapshot.subprocess.run", side_effect=_mock_run):
            _, manifest = snapshot_drydock(
                env["ws"], migration_id="mig_v", registry=env["registry"],
                secrets_root=env["secrets_root"],
                overlays_root=env["overlays_root"],
                migrations_root=env["migrations_root"],
                volume_names=["claude-code-config"],
            )
        assert len(manifest.volumes) == 1
        assert manifest.volumes[0].name == "claude-code-config"
        assert manifest.volumes[0].size_bytes > 0

    def test_missing_volume_silently_skipped(self, tmp_path):
        env = _seed_drydock(tmp_path)
        # Mock: inspect returns nonzero (volume absent)
        def _mock_run(cmd, **kw):
            res = MagicMock()
            if cmd[1:3] == ["volume", "inspect"]:
                res.returncode = 1
                res.stdout = ""
                res.stderr = "no such volume"
            else:
                res.returncode = 0
                res.stdout = ""
                res.stderr = ""
            return res

        with patch("drydock.core.snapshot.subprocess.run", side_effect=_mock_run):
            _, manifest = snapshot_drydock(
                env["ws"], migration_id="mig_v2", registry=env["registry"],
                secrets_root=env["secrets_root"],
                overlays_root=env["overlays_root"],
                migrations_root=env["migrations_root"],
                volume_names=["does-not-exist"],
            )
        # No volume captured, but snapshot succeeds.
        assert manifest.volumes == []


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restore_writes_back_secrets_and_overlay(self, tmp_path):
        env = _seed_drydock(tmp_path)
        snapshot_dir, manifest = snapshot_drydock(
            env["ws"], migration_id="mig_r1", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )

        # Wipe the live state to simulate a rollback.
        import shutil
        shutil.rmtree(env["secrets_root"] / env["ws"].id)
        (env["overlays_root"] / f"{env['ws'].id}.devcontainer.json").unlink()

        loaded = restore_drydock(
            snapshot_dir,
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            registry=env["registry"],
            restore_volumes=False,
        )
        assert loaded.migration_id == manifest.migration_id
        # State written back
        assert (env["secrets_root"] / env["ws"].id / "drydock-token").is_file()
        assert (env["overlays_root"] / f"{env['ws'].id}.devcontainer.json").is_file()

    def test_restore_idempotent(self, tmp_path):
        env = _seed_drydock(tmp_path)
        snapshot_dir, _ = snapshot_drydock(
            env["ws"], migration_id="mig_r2", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )
        # Two restores in a row don't error
        restore_drydock(
            snapshot_dir,
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            registry=env["registry"],
            restore_volumes=False,
        )
        restore_drydock(
            snapshot_dir,
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            registry=env["registry"],
            restore_volumes=False,
        )
        assert (env["secrets_root"] / env["ws"].id / "drydock-token").is_file()


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------


class TestSafeExtract:
    def test_path_traversal_rejected(self, tmp_path):
        """A snapshot tarball containing '../escape' must be refused."""
        env = _seed_drydock(tmp_path)
        # Build a malicious snapshot.tgz manually
        snapshot_dir = env["migrations_root"] / "mig_evil"
        snapshot_dir.mkdir()
        tgz = snapshot_dir / "snapshot.tgz"
        with tarfile.open(tgz, "w:gz") as tf:
            evil = tarfile.TarInfo(name="../escape.txt")
            evil.size = 4
            import io
            tf.addfile(evil, io.BytesIO(b"evil"))
        # Write a manifest pointing at it
        manifest = {
            "version": 1, "migration_id": "mig_evil",
            "drydock_id": env["ws"].id, "drydock_name": "test",
            "captured_at": "2026-05-07T00:00:00Z",
            "registry_row": {}, "volumes": [],
            "secrets_path_in_tarball": None,
            "overlay_path_in_tarball": None,
            "bytes_total": 0,
        }
        (snapshot_dir / "manifest.json").write_text(json.dumps(manifest))

        with pytest.raises(SnapshotError) as exc:
            restore_drydock(
                snapshot_dir,
                secrets_root=env["secrets_root"],
                overlays_root=env["overlays_root"],
                registry=env["registry"],
                restore_volumes=False,
            )
        assert "path-traversal" in str(exc.value)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_removes_directory(self, tmp_path):
        env = _seed_drydock(tmp_path)
        snapshot_dir, _ = snapshot_drydock(
            env["ws"], migration_id="mig_c1", registry=env["registry"],
            secrets_root=env["secrets_root"],
            overlays_root=env["overlays_root"],
            migrations_root=env["migrations_root"],
            capture_volumes=False,
        )
        assert snapshot_dir.is_dir()
        cleanup_snapshot(snapshot_dir)
        assert not snapshot_dir.exists()

    def test_cleanup_missing_dir_is_noop(self, tmp_path):
        cleanup_snapshot(tmp_path / "does-not-exist")  # no error
