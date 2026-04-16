"""wsd.toml configuration loader (Slice 3d).

Per docs/v2-design-protocol.md §6 the daemon reads `~/.drydock/wsd.toml`
at startup. V2.0 cares about one section:

    [secrets]
    backend = "file"     # default; reserved future values: "1password", "vault", ...

Unknown backend names are rejected with `unknown_secrets_backend` BEFORE
the daemon binds the socket. Failing fast at startup beats failing
mid-RequestCapability where the caller might be a non-interactive
agent that can't surface the error helpfully.

Missing config file → all defaults. Missing [secrets] section → "file".
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


class ConfigError(ValueError):
    """Raised for malformed or rejected wsd.toml content."""


@dataclass(frozen=True)
class WsdConfig:
    secrets_backend: str = "file"


def load_wsd_config(path: Path) -> WsdConfig:
    """Load wsd.toml. Returns defaults if the file is absent.

    Raises ConfigError for:
    - malformed TOML
    - non-table [secrets] section
    - non-string [secrets] backend
    - unknown backend names
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

    backend = secrets.get("backend", "file")
    if not isinstance(backend, str) or not backend:
        raise ConfigError(f"{path}: [secrets] backend must be a non-empty string")

    if backend not in KNOWN_SECRETS_BACKENDS:
        raise ConfigError(
            f"unknown_secrets_backend: {backend!r} (known: {sorted(KNOWN_SECRETS_BACKENDS)})"
        )

    return WsdConfig(secrets_backend=backend)
