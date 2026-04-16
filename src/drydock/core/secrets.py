"""Secrets backend Protocol + V2 file-backed implementation.

Per docs/v2-design-capability-broker.md §7: V2 ships only `FileBackend`
as a concrete `SecretsBackend`. The Protocol is reserved for additive
future backends (1Password via `op`, HashiCorp Vault, cloud SMs); the
RPC surface (`RequestCapability(type=SECRET, ...)`) is backend-independent
so adding a backend is a new class + a wsd.toml entry, no protocol churn.

Backend-fetch contract is bytes-not-path so network-sourced backends
return the same shape as file-backed.

Sync-first per the docstring on `SecretsBackend.fetch` — network backends
should add `fetch_async` additively when they ship.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class BackendMissingSecret(Exception):
    """The backend resolved the request but found no secret with that name.

    Distinct from `BackendUnavailable` — missing is a deterministic
    not-found; unavailable is a transient connection/permission failure
    that may succeed on retry.
    """


class BackendUnavailable(Exception):
    """The backend could not be queried (transient, retriable)."""


class BackendPermissionDenied(Exception):
    """The backend rejected the read (e.g. file ownership wrong)."""


@runtime_checkable
class SecretsBackend(Protocol):
    """Plugin interface for capability-broker secrets backends.

    See docs/v2-design-capability-broker.md §7. New backends are additive:
    a class implementing this Protocol + a wsd.toml `[secrets] backend`
    entry. The daemon dispatches via this Protocol; lease materialization
    and the RPC surface are backend-independent.
    """

    name: str

    def fetch(self, secret_name: str, desk_id: str) -> bytes | None:
        """Return secret bytes, or None if not found.

        Sync-first: V2 ships only the file-backed backend (stat + read,
        no I/O wait). Network-sourced backends (1Password via `op`, Vault,
        cloud SMs) should add `async def fetch_async` as an additive
        method when they ship; the daemon will prefer `fetch_async` when
        present and fall back to `fetch` otherwise. Wrapping a sync
        `fetch` in `run_in_executor` works as a transition but adds
        latency and noisier error semantics — not a long-term answer.
        """
        ...

    def supports_rotation(self) -> bool:
        ...

    def rotate(self, secret_name: str) -> bytes | None:
        ...


class FileBackend:
    """Read secrets from `~/.drydock/secrets/<desk_id>/<name>` (file-backed).

    Phase-1 convention shipped with `ws secret set/list/rm/push`. The
    daemon's lease handler reads bytes via `fetch()` and materializes them
    into the desk's `/run/secrets/<name>` (separate from this backend's
    storage path).

    Mode-0400 ownership belongs to the file write path (`ws secret set`),
    not this read path; we surface `BackendPermissionDenied` if read
    fails for permissions reasons so the daemon can produce a useful
    error to the caller.

    Rotation is not supported (Phase 1 has no rotation primitive).
    Future backends with rotation set `supports_rotation() = True`.
    """

    name = "file"

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path_for(self, secret_name: str, desk_id: str) -> Path:
        return self.root / desk_id / secret_name

    def fetch(self, secret_name: str, desk_id: str) -> bytes | None:
        path = self._path_for(secret_name, desk_id)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except PermissionError as exc:
            logger.warning(
                "file-backend: permission denied reading %s: %s", path, exc,
            )
            raise BackendPermissionDenied(str(path)) from exc
        except OSError as exc:
            logger.warning(
                "file-backend: I/O error reading %s: %s", path, exc,
            )
            raise BackendUnavailable(str(path)) from exc

    def supports_rotation(self) -> bool:
        return False

    def rotate(self, secret_name: str) -> bytes | None:
        del secret_name
        return None


def build_backend(name: str, *, secrets_root: Path) -> SecretsBackend:
    """Construct the configured backend.

    Raises ValueError for unknown backend names — the daemon translates
    this to an `unknown_secrets_backend` RPC error before serving any
    request.
    """
    if name == "file":
        return FileBackend(root=secrets_root)
    raise ValueError(f"unknown_secrets_backend: {name!r}")
