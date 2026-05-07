"""Snapshot + restore — capture a Drydock's portable state into one
addressable artifact and restore it atomically.

Phase 2a.4 M1 of make-the-harness-live.md. Tonight's hetzner deploy
was an ad-hoc shell sequence: `cp registry.db ...; tar czf state.tgz
secrets/ worktrees/ overlays/`. This module turns that into a
reusable primitive the migration state machine consumes.

Captured components:

- **Registry row** for this drydock (and its FK rows in tokens, leases,
  amendments, deskwatch_events, workload_leases, migrations) → JSON dump.
- **Secrets dir** (``~/.drydock/secrets/<id>/``) → tar with mode bits
  preserved. The 0400 file modes matter — restoring 0644 would break
  the secret-bind-mount semantic.
- **Overlay JSON** (``~/.drydock/overlays/<id>.devcontainer.json``) →
  straight file copy.
- **Worktree git state** — branch name + commit ref. We do NOT tar the
  whole worktree; the git history is the storage. Restore re-checks
  out the branch from the (preserved) `repo_path`. Uncommitted edits
  surface as a warning at snapshot time; this is the design's
  "uncommitted work blocks migration" gate.
- **Named volumes** that the desk owns (claude-code-config-*,
  drydock-vscode-server, etc.) — each tarred via a transient
  `docker run --rm -v <vol>:/data -v $SNAP:/host alpine tar` step.
  M1 captures volumes the desk explicitly bind-mounts; ${devcontainerId}-
  derived volumes (bash history, tailscale-state) get captured too if
  they exist locally.

What's NOT captured:

- The container's ephemeral filesystem. By design — container is the
  security boundary; recreating it is the trust operation.
- The image itself. Image bumps capture the *target* tag in the plan;
  the registry pulls if needed at restore.
- Anything outside the components listed above. State written to
  random paths in the container is lost on recreate. (See the table in
  the design doc's "How state survives container recreate" section.)

The snapshot is a single addressable artifact at
``~/.drydock/migrations/<migration_id>/snapshot.tgz`` plus a sibling
``manifest.json`` describing what's inside. Manifest is what the
restore path drives off; the tarball is the bytes.

The atomic-write pattern: stage the snapshot in a sibling temp dir,
finalize via os.replace. A daemon crash during snapshot leaves the
temp dir; the next migration reuses or cleans it.

Cross-host (M5) plugs in by replacing the local-tar StateBackend with
an S3 (or other shared-substrate) backend. The interface is
intentionally minimal — capture(components) → blob_handle,
restore(blob_handle) → components — so the state machine doesn't care
which backend is in play.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


SNAPSHOT_VERSION = 1
DEFAULT_MIGRATIONS_ROOT = Path.home() / ".drydock" / "migrations"


# ---------------------------------------------------------------------------
# Manifest — what's captured, what tools to use to restore
# ---------------------------------------------------------------------------


@dataclass
class WorktreeState:
    """Captured worktree pointer — branch + commit + dirty-flag.

    We don't tar the worktree contents. The git history (in the
    parent repo) is the storage; the working tree is just a checkout.
    Restore re-checks out the branch at the recorded commit.
    """
    branch: str
    commit_sha: str
    repo_path: str
    is_dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)


@dataclass
class VolumeRef:
    """One captured docker named volume.

    `archive_path_in_tarball` is the relative path inside the snapshot
    tarball where the volume's tar lives (e.g., `volumes/claude-code-config.tar.gz`).
    """
    name: str
    archive_path_in_tarball: str
    size_bytes: int = 0


@dataclass
class SnapshotManifest:
    """The structured description of a snapshot tarball's contents."""
    version: int
    migration_id: str
    drydock_id: str
    drydock_name: str
    captured_at: str             # ISO-8601 UTC
    registry_row: dict           # the drydocks-row dump
    overlay_path_in_tarball: Optional[str] = None
    secrets_path_in_tarball: Optional[str] = None
    worktree: Optional[WorktreeState] = None
    volumes: list[VolumeRef] = field(default_factory=list)
    bytes_total: int = 0

    def to_dict(self) -> dict:
        d = {
            "version": self.version,
            "migration_id": self.migration_id,
            "drydock_id": self.drydock_id,
            "drydock_name": self.drydock_name,
            "captured_at": self.captured_at,
            "registry_row": self.registry_row,
            "overlay_path_in_tarball": self.overlay_path_in_tarball,
            "secrets_path_in_tarball": self.secrets_path_in_tarball,
            "volumes": [
                {"name": v.name, "archive_path_in_tarball": v.archive_path_in_tarball,
                 "size_bytes": v.size_bytes}
                for v in self.volumes
            ],
            "bytes_total": self.bytes_total,
        }
        if self.worktree is not None:
            d["worktree"] = {
                "branch": self.worktree.branch,
                "commit_sha": self.worktree.commit_sha,
                "repo_path": self.worktree.repo_path,
                "is_dirty": self.worktree.is_dirty,
                "dirty_files": list(self.worktree.dirty_files),
            }
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "SnapshotManifest":
        wt_raw = raw.get("worktree")
        worktree = None
        if wt_raw is not None:
            worktree = WorktreeState(
                branch=wt_raw["branch"],
                commit_sha=wt_raw["commit_sha"],
                repo_path=wt_raw["repo_path"],
                is_dirty=wt_raw.get("is_dirty", False),
                dirty_files=list(wt_raw.get("dirty_files", [])),
            )
        volumes = [
            VolumeRef(
                name=v["name"],
                archive_path_in_tarball=v["archive_path_in_tarball"],
                size_bytes=v.get("size_bytes", 0),
            )
            for v in raw.get("volumes", [])
        ]
        return cls(
            version=raw["version"],
            migration_id=raw["migration_id"],
            drydock_id=raw["drydock_id"],
            drydock_name=raw["drydock_name"],
            captured_at=raw["captured_at"],
            registry_row=raw["registry_row"],
            overlay_path_in_tarball=raw.get("overlay_path_in_tarball"),
            secrets_path_in_tarball=raw.get("secrets_path_in_tarball"),
            worktree=worktree,
            volumes=volumes,
            bytes_total=raw.get("bytes_total", 0),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SnapshotError(RuntimeError):
    """Snapshot or restore failed at a structural level."""


class SnapshotDirtyWorktreeError(SnapshotError):
    """Refused: worktree has uncommitted changes the migration can't preserve.

    Carries the list of dirty paths so the operator can commit, stash,
    or pass --force-dirty (a future flag) to migrate anyway.
    """
    def __init__(self, message: str, *, dirty_files: list[str]):
        super().__init__(message)
        self.dirty_files = dirty_files


# ---------------------------------------------------------------------------
# Snapshot — capture
# ---------------------------------------------------------------------------


def snapshot_drydock(
    drydock,                              # core.runtime.Drydock
    *,
    migration_id: str,
    registry,                             # core.registry.Registry
    secrets_root: Path,
    overlays_root: Path,
    migrations_root: Optional[Path] = None,
    volume_names: Optional[list[str]] = None,
    refuse_dirty_worktree: bool = True,
    docker_bin: Optional[str] = None,
    capture_volumes: bool = True,
) -> tuple[Path, SnapshotManifest]:
    """Capture this drydock's portable state into a snapshot tarball.

    Returns ``(snapshot_dir, manifest)``. snapshot_dir is the directory
    under migrations_root/<migration_id>/ holding ``snapshot.tgz``
    + ``manifest.json``.

    Stages:
    1. Capture registry row (drydocks + FK rows).
    2. Inspect worktree git state. If dirty AND refuse_dirty_worktree,
       raises SnapshotDirtyWorktreeError before any I/O.
    3. Stage secrets dir + overlay file into a temp dir.
    4. Tar each requested named volume into the temp dir.
    5. Roll the temp dir into snapshot.tgz, write manifest.json,
       atomic-replace into the migration's directory.
    """
    migrations_root = migrations_root or DEFAULT_MIGRATIONS_ROOT
    docker = docker_bin or shutil.which("docker") or "docker"

    target_dir = migrations_root / migration_id
    target_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{migration_id}-stage-",
                                      dir=str(migrations_root)) as stage_dir_str:
        stage_dir = Path(stage_dir_str)

        # 1. Registry row.
        registry_row = _capture_registry_row(registry, drydock.id)

        # 2. Worktree git state.
        worktree = _capture_worktree(drydock)
        if worktree and worktree.is_dirty and refuse_dirty_worktree:
            raise SnapshotDirtyWorktreeError(
                f"worktree at {worktree.repo_path} has {len(worktree.dirty_files)} "
                f"uncommitted change(s); commit, stash, or pass --force-dirty",
                dirty_files=worktree.dirty_files,
            )

        # 3. Secrets dir + overlay file.
        secrets_path_in_tarball: Optional[str] = None
        secrets_src = Path(secrets_root) / drydock.id
        if secrets_src.is_dir():
            secrets_dst = stage_dir / "secrets" / drydock.id
            secrets_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(secrets_src, secrets_dst, symlinks=False)
            secrets_path_in_tarball = f"secrets/{drydock.id}"

        overlay_path_in_tarball: Optional[str] = None
        overlay_src = Path(overlays_root) / f"{drydock.id}.devcontainer.json"
        if overlay_src.is_file():
            overlay_dst = stage_dir / "overlay.devcontainer.json"
            shutil.copy2(overlay_src, overlay_dst)
            overlay_path_in_tarball = "overlay.devcontainer.json"

        # 4. Named volumes.
        volumes_dir = stage_dir / "volumes"
        captured_volumes: list[VolumeRef] = []
        if capture_volumes and volume_names:
            volumes_dir.mkdir(exist_ok=True)
            for vol_name in volume_names:
                size = _capture_named_volume(
                    docker, vol_name, volumes_dir / f"{vol_name}.tar.gz",
                )
                if size is not None:
                    captured_volumes.append(VolumeRef(
                        name=vol_name,
                        archive_path_in_tarball=f"volumes/{vol_name}.tar.gz",
                        size_bytes=size,
                    ))

        # 5. Roll up the staged content.
        snapshot_tgz_tmp = target_dir / "snapshot.tgz.tmp"
        with tarfile.open(snapshot_tgz_tmp, "w:gz") as tf:
            for entry in stage_dir.iterdir():
                tf.add(entry, arcname=entry.name)
        snapshot_tgz = target_dir / "snapshot.tgz"
        os.replace(snapshot_tgz_tmp, snapshot_tgz)

        manifest = SnapshotManifest(
            version=SNAPSHOT_VERSION,
            migration_id=migration_id,
            drydock_id=drydock.id,
            drydock_name=drydock.name,
            captured_at=_utc_now(),
            registry_row=registry_row,
            overlay_path_in_tarball=overlay_path_in_tarball,
            secrets_path_in_tarball=secrets_path_in_tarball,
            worktree=worktree,
            volumes=captured_volumes,
            bytes_total=snapshot_tgz.stat().st_size,
        )
        manifest_path_tmp = target_dir / "manifest.json.tmp"
        manifest_path_tmp.write_text(json.dumps(manifest.to_dict(), indent=2))
        manifest_path = target_dir / "manifest.json"
        os.replace(manifest_path_tmp, manifest_path)

        logger.info(
            "snapshot: drydock=%s migration_id=%s bytes=%d volumes=%d",
            drydock.id, migration_id, manifest.bytes_total, len(captured_volumes),
        )
        return target_dir, manifest


def load_manifest(snapshot_dir: Path) -> SnapshotManifest:
    """Read and parse the manifest from a snapshot directory."""
    manifest_path = Path(snapshot_dir) / "manifest.json"
    return SnapshotManifest.from_dict(json.loads(manifest_path.read_text()))


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------


def _capture_registry_row(registry, drydock_id: str) -> dict:
    """Dump the drydock + its FK references as a structured JSON blob."""
    row = registry._conn.execute(
        "SELECT * FROM drydocks WHERE id = ?", (drydock_id,),
    ).fetchone()
    if row is None:
        raise SnapshotError(f"drydock {drydock_id!r} not found in registry")
    drydocks_row = dict(row)

    out: dict = {"drydocks": drydocks_row, "tokens": [], "leases": [],
                 "events": [], "amendments": [], "workload_leases": []}

    # tokens, leases, events, amendments — each FK'd by drydock_id
    for table in ("tokens", "leases", "events", "amendments", "workload_leases"):
        try:
            cur = registry._conn.execute(
                f"SELECT * FROM {table} WHERE drydock_id = ?", (drydock_id,),
            )
            out[table] = [dict(r) for r in cur.fetchall()]
        except Exception as exc:
            # Table may not exist (very old registry); skip cleanly.
            logger.debug("snapshot: skip table %s: %s", table, exc)
            out[table] = []
    return out


def _capture_worktree(drydock) -> Optional[WorktreeState]:
    """Read the drydock's worktree branch + commit + dirty status."""
    if not drydock.worktree_path:
        return None
    repo_path = Path(drydock.worktree_path)
    if not repo_path.is_dir():
        return None
    if not (repo_path / ".git").exists() and not _looks_like_worktree(repo_path):
        return None

    branch = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD") or drydock.branch
    commit = _git(repo_path, "rev-parse", "HEAD") or ""
    status_lines = _git(repo_path, "status", "--porcelain") or ""
    dirty_files = [
        line.strip() for line in status_lines.splitlines() if line.strip()
    ]
    return WorktreeState(
        branch=branch,
        commit_sha=commit,
        repo_path=str(repo_path),
        is_dirty=bool(dirty_files),
        dirty_files=dirty_files,
    )


def _looks_like_worktree(path: Path) -> bool:
    """git worktree add creates a .git FILE (not directory) pointing
    at the parent repo; treat that as a worktree."""
    git_marker = path / ".git"
    return git_marker.is_file()


def _git(cwd: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _capture_named_volume(
    docker_bin: str,
    volume_name: str,
    target_tar: Path,
) -> Optional[int]:
    """Tar a docker named volume's contents to `target_tar`.

    Returns the resulting tarball's byte size, or None if the volume
    doesn't exist (silently skipped — desks may declare volumes that
    haven't been created yet).
    """
    # Probe existence first; docker run on a missing volume creates one.
    inspect = subprocess.run(
        [docker_bin, "volume", "inspect", volume_name],
        capture_output=True, text=True, timeout=5,
    )
    if inspect.returncode != 0:
        logger.debug("snapshot: volume %s absent, skipping", volume_name)
        return None

    # `docker run --rm -v <vol>:/data -v <hostdir>:/host alpine tar czf /host/<file> -C /data .`
    target_tar.parent.mkdir(parents=True, exist_ok=True)
    host_dir = target_tar.parent.resolve()
    out_name = target_tar.name
    cmd = [
        docker_bin, "run", "--rm",
        "-v", f"{volume_name}:/data:ro",
        "-v", f"{host_dir}:/host",
        "alpine",
        "tar", "czf", f"/host/{out_name}", "-C", "/data", ".",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise SnapshotError(
            f"failed to capture volume {volume_name!r}: "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )
    return target_tar.stat().st_size if target_tar.exists() else 0


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Restore — undo
# ---------------------------------------------------------------------------


def restore_drydock(
    snapshot_dir: Path,
    *,
    secrets_root: Path,
    overlays_root: Path,
    registry,
    docker_bin: Optional[str] = None,
    restore_volumes: bool = True,
) -> SnapshotManifest:
    """Reverse of snapshot_drydock — best-effort, idempotent.

    Reads manifest.json + snapshot.tgz from `snapshot_dir`, then:
    - Restores secrets dir contents (overwrites existing).
    - Restores overlay JSON file.
    - Restores each named volume from its tar (creates volume if missing).
    - Worktree branch is NOT git-checked-out by this module — caller
      handles that via the runtime's checkout logic. We just record the
      target branch + commit in the manifest.

    Registry row is NOT restored automatically — that's the migration
    state machine's choice (e.g., on rollback after a partial mutate,
    we want to restore; on routine completion we don't). The manifest
    carries the row data for the caller.
    """
    docker = docker_bin or shutil.which("docker") or "docker"
    manifest = load_manifest(snapshot_dir)

    # Extract the snapshot tarball into a temp dir.
    with tempfile.TemporaryDirectory(prefix=f"{manifest.migration_id}-restore-") as t:
        extract_dir = Path(t)
        with tarfile.open(snapshot_dir / "snapshot.tgz", "r:gz") as tf:
            _safe_extract(tf, extract_dir)

        # Secrets.
        if manifest.secrets_path_in_tarball:
            src = extract_dir / manifest.secrets_path_in_tarball
            dst = Path(secrets_root) / manifest.drydock_id
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, symlinks=False)

        # Overlay.
        if manifest.overlay_path_in_tarball:
            src = extract_dir / manifest.overlay_path_in_tarball
            dst = Path(overlays_root) / f"{manifest.drydock_id}.devcontainer.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        # Named volumes.
        if restore_volumes:
            for vol in manifest.volumes:
                tar_path = extract_dir / vol.archive_path_in_tarball
                if not tar_path.is_file():
                    logger.warning("restore: volume tar missing: %s", vol.archive_path_in_tarball)
                    continue
                _restore_named_volume(docker, vol.name, tar_path)

    return manifest


def _restore_named_volume(
    docker_bin: str,
    volume_name: str,
    tar_path: Path,
) -> None:
    """Recreate the volume (if absent) and untar contents into it.

    The volume isn't deleted first — restore is idempotent in the
    sense that running it twice is fine. If you need a clean restore,
    `docker volume rm` first.
    """
    # Ensure the volume exists.
    create = subprocess.run(
        [docker_bin, "volume", "create", volume_name],
        capture_output=True, text=True, timeout=10,
    )
    if create.returncode != 0:
        raise SnapshotError(
            f"failed to create volume {volume_name!r}: {create.stderr.strip()}"
        )

    # Untar via a transient alpine container.
    host_dir = tar_path.parent.resolve()
    tar_name = tar_path.name
    cmd = [
        docker_bin, "run", "--rm",
        "-v", f"{volume_name}:/data",
        "-v", f"{host_dir}:/host:ro",
        "alpine",
        "tar", "xzf", f"/host/{tar_name}", "-C", "/data",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise SnapshotError(
            f"failed to restore volume {volume_name!r}: "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """tarfile.extractall + path-traversal guard.

    Python 3.12 made the unsafe-extractall behavior emit a deprecation
    warning. Use the same explicit-filter pattern recommended by the
    docs without relying on the new ``filter='data'`` flag (3.12+).
    """
    dest = dest.resolve()
    for member in tf.getmembers():
        member_dest = (dest / member.name).resolve()
        if not str(member_dest).startswith(str(dest)):
            raise SnapshotError(
                f"snapshot tarball contains path-traversal entry: {member.name!r}"
            )
    tf.extractall(dest)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_snapshot(snapshot_dir: Path) -> None:
    """Delete the snapshot directory. Used by stage 11 (Cleanup) after
    a configured retention window, or immediately on demand."""
    snapshot_dir = Path(snapshot_dir)
    if snapshot_dir.is_dir():
        shutil.rmtree(snapshot_dir)
