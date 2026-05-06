"""Filesystem-side V1 → drydock vocab migration.

The schema migration in ``registry.py`` handles the SQLite side. This
module handles per-Harbor filesystem artifacts that share the same
``ws_<slug>`` naming convention:

- ``~/.drydock/secrets/ws_<slug>/``  → ``~/.drydock/secrets/dock_<slug>/``
- ``~/.drydock/overlays/ws_<slug>.devcontainer.json`` → same with ``dock_``
- ``~/.drydock/worktrees/ws_<slug>/`` → same with ``dock_``

Idempotent. Safe to run before or after the schema migration; the
calling order in practice is daemon-startup → Registry init (schema
migration) → migrate_v1_artifacts() → continue.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


_PARENT_DIRS = ("secrets", "worktrees")  # contain ws_<slug>/ subdirs
_PARENT_FILES = ("overlays",)             # contain ws_<slug>.<suffix> files

# Top-level files renamed in the V1 → drydock vocab pass.
_TOP_LEVEL_RENAMES = (
    ("wsd.toml", "daemon.toml"),
    ("wsd.log", "daemon.log"),
    ("logs/wsd-systemd.log", "logs/daemon-systemd.log"),
    ("logs/wsd-launchd.log", "logs/daemon-launchd.log"),
)


def migrate_v1_artifacts(drydock_home: Path | None = None) -> dict:
    """Rename filesystem artifacts using ws_ prefix to dock_ prefix.

    Returns a summary dict ``{"renamed": [...], "skipped": [...]}``.
    """
    home = drydock_home or (Path.home() / ".drydock")
    summary: dict = {"renamed": [], "skipped": []}
    if not home.exists():
        return summary

    for parent in _PARENT_DIRS:
        parent_dir = home / parent
        if not parent_dir.is_dir():
            continue
        for child in parent_dir.iterdir():
            if not child.name.startswith("ws_"):
                continue
            new_name = "dock_" + child.name[3:]
            target = parent_dir / new_name
            if target.exists():
                summary["skipped"].append(f"{child} (target exists)")
                continue
            child.rename(target)
            summary["renamed"].append(f"{child} → {target}")
            logger.info("renamed %s -> %s", child, target)

    for parent in _PARENT_FILES:
        parent_dir = home / parent
        if not parent_dir.is_dir():
            continue
        for child in parent_dir.iterdir():
            if not child.name.startswith("ws_"):
                continue
            new_name = "dock_" + child.name[3:]
            target = parent_dir / new_name
            if target.exists():
                summary["skipped"].append(f"{child} (target exists)")
                continue
            child.rename(target)
            summary["renamed"].append(f"{child} → {target}")
            logger.info("renamed %s -> %s", child, target)

    # Top-level files: wsd.toml → daemon.toml, etc.
    for old_rel, new_rel in _TOP_LEVEL_RENAMES:
        old_path = home / old_rel
        new_path = home / new_rel
        if not old_path.exists():
            continue
        if new_path.exists():
            summary["skipped"].append(f"{old_path} (target exists)")
            continue
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)
        summary["renamed"].append(f"{old_path} → {new_path}")
        logger.info("renamed %s -> %s", old_path, new_path)

    return summary
