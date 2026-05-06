"""Bearer-token helpers for `drydock daemon` per `docs/v2-design-protocol.md` §5."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from drydock.core import chown_to_container
from drydock.core.registry import Registry

TOKEN_FILENAME = "drydock-token"
DEFAULT_SECRETS_ROOT = Path.home() / ".drydock" / "secrets"


def generate_token() -> str:
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    return token.rstrip("=")


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_token_for_desk(
    drydock_id: str,
    secrets_root: Path = DEFAULT_SECRETS_ROOT,
    registry: Registry | None = None,
) -> str:
    if registry is None:
        raise ValueError("registry is required")

    token_info = registry.get_token_info(drydock_id)
    token_path = Path(secrets_root) / drydock_id / TOKEN_FILENAME
    if token_info is not None:
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            from drydock.daemon.server import _RpcError

            raise _RpcError(code=-32603, message="token_exists_no_plaintext") from exc

    plaintext = generate_token()
    _write_secret_atomic(token_path, plaintext)
    registry.insert_token(
        drydock_id=drydock_id,
        token_sha256=hash_token(plaintext),
        issued_at=datetime.now(timezone.utc),
    )
    return plaintext


def validate_token(plaintext: str | None, registry: Registry | None = None) -> str | None:
    if plaintext is None or registry is None or not plaintext:
        return None
    return registry.find_desk_by_token_hash(hash_token(plaintext))


def _write_secret_atomic(path: Path, plaintext: str) -> None:
    # drydock-token ships here even when a drydock has no user-set secrets,
    # so the dir must be readable by the container user regardless.
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    chown_to_container(path.parent)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(plaintext)
        handle.flush()
        os.fchmod(handle.fileno(), 0o400)
    chown_to_container(tmp_path)
    os.replace(tmp_path, path)
