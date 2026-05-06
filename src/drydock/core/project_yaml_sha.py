"""Compute SHA-256 of a project YAML file's content.

Phase 0 of project-dock-ontology.md: the daemon stores `pinned_yaml_sha256`
on each Drydock at create + reload time. `ws host audit` compares it to
the current YAML's SHA — divergence = silent drift between what's on disk
and what's pinned to the running Dock.

The hash is over RAW YAML BYTES, not the parsed/expanded ProjectConfig.
This catches edits that change semantics (entitlements, ceilings) AND
edits that don't (whitespace, comments). Both are "the file changed";
operationally the principal probably wants to know.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .project_config import default_projects_dir


def project_yaml_path(project: str, base_dir: Path | None = None) -> Path:
    """Resolved YAML path for a project (matching load_project_config)."""
    if base_dir is None:
        base_dir = default_projects_dir()
    return base_dir / f"{project}.yaml"


def compute_project_yaml_sha(
    project: str, base_dir: Path | None = None,
) -> str:
    """Return SHA-256 hex of the project YAML's content.

    Returns empty string if the file is missing or unreadable. Empty-vs-
    populated is the signal — empty means "couldn't compute, treat as
    unknown rather than asserting equality."
    """
    path = project_yaml_path(project, base_dir)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def yaml_drift_status(pinned: str, current: str) -> str:
    """Classify the relationship between a pinned SHA and current SHA.

    Returns one of:
      - 'in_sync'     — pinned == current (and both populated)
      - 'drifted'     — pinned != current, both populated
      - 'yaml_missing' — pinned populated, current empty (file gone)
      - 'unpinned'    — pinned empty (legacy row, never pinned)
      - 'unknown'     — both empty (degenerate)
    """
    if not pinned and not current:
        return "unknown"
    if not pinned:
        return "unpinned"
    if not current:
        return "yaml_missing"
    return "in_sync" if pinned == current else "drifted"
