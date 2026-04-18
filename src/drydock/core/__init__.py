"""Drydock core — workspace model, registry, and orchestration primitives."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# drydock-base's remoteUser is `node` (uid 1000). When `ws` runs as root on a
# Linux host, bind-mounts preserve real uid/gid, so secrets and worktrees must
# be owned by uid 1000 for the container's `node` user to read/write them. On
# macOS, Docker Desktop's mount layer does uid translation transparently and
# the host-side chown is unnecessary (and would fail when ws runs as a normal
# user). Callers should gate chown calls on `os.geteuid() == 0`.
CONTAINER_REMOTE_UID = 1000
CONTAINER_REMOTE_GID = 1000

_logger = logging.getLogger(__name__)


def chown_to_container(path: Path | str) -> None:
    """Chown a path to the container node uid/gid; no-op without permission.

    Silently skipped on non-root Harbors (Mac dev) where same-uid perms
    already suffice. Raising would force every caller site to wrap in
    try/except, which is what we want to avoid.
    """
    try:
        os.chown(path, CONTAINER_REMOTE_UID, CONTAINER_REMOTE_GID)
    except PermissionError:
        _logger.debug("chown_to_container skipped (not root): %s", path)


@dataclass
class WsError(Exception):
    """An error that includes what to do about it.

    Every error carries a human/LLM-readable `fix` field so the caller
    never has to guess the corrective action.
    """

    message: str
    fix: str | None = None
    context: dict = field(default_factory=dict)
    code: str | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict:
        if self.code:
            d: dict = {"error": self.code, "message": self.message}
        else:
            d = {"error": self.message}
        if self.fix:
            d["fix"] = self.fix
        if self.context:
            d["context"] = self.context
        return d
