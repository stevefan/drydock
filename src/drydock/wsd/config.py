"""wsd.toml configuration loader (Slice 3d + V4 Phase 1).

Per docs/v2-design-protocol.md §6 the daemon reads `~/.drydock/wsd.toml`
at startup. Sections:

    [secrets]
    backend = "file"     # default; reserved future values: "1password", "vault", ...

    [storage]
    backend       = "stub"            # or "sts". Default: absent → STORAGE_MOUNT unavailable.
    role_arn      = "arn:aws:iam::..."  # required for "sts"
    source_profile = "drydock-runner"   # AWS profile on the Harbor with AssumeRole perms
    session_duration_seconds = 14400    # max, per role config

Unknown backend names are rejected with `unknown_secrets_backend` /
`unknown_storage_backend` BEFORE the daemon binds the socket. Failing
fast at startup beats failing mid-RequestCapability where the caller
might be a non-interactive worker that can't surface the error helpfully.

Missing config file → all defaults. Missing [storage] section → no
storage backend configured; STORAGE_MOUNT leases reject with a helpful
`storage_backend_not_configured`.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — package metadata pins requires-python >= 3.11
    raise RuntimeError("wsd requires Python 3.11+ for tomllib")

logger = logging.getLogger(__name__)

KNOWN_SECRETS_BACKENDS = {"file"}
KNOWN_STORAGE_BACKENDS = {"sts", "stub"}
DEFAULT_STORAGE_SOURCE_PROFILE = "drydock-runner"
DEFAULT_STORAGE_SESSION_DURATION = 14400


class ConfigError(ValueError):
    """Raised for malformed or rejected wsd.toml content."""


@dataclass(frozen=True)
class WsdConfig:
    secrets_backend: str = "file"
    # V4 Phase 1. None = storage backend not configured; STORAGE_MOUNT
    # leases reject with storage_backend_not_configured. "sts" = real
    # AWS STS AssumeRole flow; "stub" = in-memory test backend.
    storage_backend: str | None = None
    storage_role_arn: str | None = None
    storage_source_profile: str = DEFAULT_STORAGE_SOURCE_PROFILE
    storage_session_duration_seconds: int = DEFAULT_STORAGE_SESSION_DURATION


def load_wsd_config(path: Path) -> WsdConfig:
    """Load wsd.toml. Returns defaults if the file is absent.

    Raises ConfigError for:
    - malformed TOML
    - non-table [secrets] / [storage] section
    - non-string / missing backend names
    - unknown backend names
    - [storage] backend = "sts" without role_arn
    """
    if not path.exists():
        return WsdConfig()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    secrets = data.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ConfigError(f"{path}: [secrets] must be a TOML table")

    secrets_backend = secrets.get("backend", "file")
    if not isinstance(secrets_backend, str) or not secrets_backend:
        raise ConfigError(f"{path}: [secrets] backend must be a non-empty string")
    if secrets_backend not in KNOWN_SECRETS_BACKENDS:
        raise ConfigError(
            f"unknown_secrets_backend: {secrets_backend!r} (known: {sorted(KNOWN_SECRETS_BACKENDS)})"
        )

    storage = data.get("storage")
    storage_backend: str | None = None
    storage_role_arn: str | None = None
    storage_source_profile = DEFAULT_STORAGE_SOURCE_PROFILE
    storage_session_duration = DEFAULT_STORAGE_SESSION_DURATION
    if storage is not None:
        if not isinstance(storage, dict):
            raise ConfigError(f"{path}: [storage] must be a TOML table")
        raw_backend = storage.get("backend")
        if raw_backend is None:
            # [storage] present but no backend = treat as unconfigured.
            storage_backend = None
        elif not isinstance(raw_backend, str) or not raw_backend:
            raise ConfigError(f"{path}: [storage] backend must be a non-empty string")
        elif raw_backend not in KNOWN_STORAGE_BACKENDS:
            raise ConfigError(
                f"unknown_storage_backend: {raw_backend!r} (known: {sorted(KNOWN_STORAGE_BACKENDS)})"
            )
        else:
            storage_backend = raw_backend

        raw_role = storage.get("role_arn")
        if raw_role is not None:
            if not isinstance(raw_role, str) or not raw_role:
                raise ConfigError(f"{path}: [storage] role_arn must be a non-empty string")
            storage_role_arn = raw_role

        raw_profile = storage.get("source_profile", DEFAULT_STORAGE_SOURCE_PROFILE)
        if not isinstance(raw_profile, str) or not raw_profile:
            raise ConfigError(f"{path}: [storage] source_profile must be a non-empty string")
        storage_source_profile = raw_profile

        raw_duration = storage.get("session_duration_seconds", DEFAULT_STORAGE_SESSION_DURATION)
        if not isinstance(raw_duration, int) or raw_duration <= 0:
            raise ConfigError(f"{path}: [storage] session_duration_seconds must be a positive integer")
        storage_session_duration = raw_duration

        if storage_backend == "sts" and not storage_role_arn:
            raise ConfigError(
                f"{path}: [storage] backend = 'sts' requires role_arn (the drydock-agent role ARN)"
            )

    return WsdConfig(
        secrets_backend=secrets_backend,
        storage_backend=storage_backend,
        storage_role_arn=storage_role_arn,
        storage_source_profile=storage_source_profile,
        storage_session_duration_seconds=storage_session_duration,
    )
