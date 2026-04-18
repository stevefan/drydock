"""Storage-lease backends for `RequestCapability(type=STORAGE_MOUNT)`.

Parallel to `secrets.py` but for time-bounded cloud storage credentials.
V4 Phase 1: the capability broker accepts STORAGE_MOUNT leases and
returns scoped AWS STS credentials. Further backends (GCS via workload
identity, Azure, etc.) are additive — new classes implementing
`StorageBackend`, a `wsd.toml [storage] backend = ...` entry, no RPC
churn.

Design choices anchored in docs/v2-design-capability-broker.md §7 and
the scripts/aws/ identity stack:

- Daemon holds long-lived `drydock-runner` AWS keys (via profile).
- Every STORAGE_MOUNT request calls `sts:AssumeRole` against
  `drydock-agent` with an INLINE SESSION POLICY that narrows permissions
  to exactly the bucket/prefix/mode the caller asked for. The existing
  permission boundary is the ceiling; session policy narrows below it.
- Default session duration 4h (matches `drydock-agent` max) — can be
  shorter when scopes become more granular.
- Mode vocabulary today: `ro` (GetObject + ListBucket), `rw` (+ Put +
  Delete). Extending to other S3 actions is additive by scope shape.

Not in scope for Phase 1:
- delegatable_storage_scopes narrowness at the policy-validator level
  (today only the coarse `REQUEST_STORAGE_LEASES` capability gates the
  call). Phase 1b.
- Non-S3 object stores (GCS, R2, Azure).
- Active FUSE mount at drydock-up time (today: credentials delivered
  into /run/secrets/, worker uses them with rclone/aws-cli/boto3).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

KNOWN_STORAGE_BACKENDS = {"sts", "stub"}
DEFAULT_SESSION_DURATION_SECONDS = 14400  # 4h — matches drydock-agent max_session_duration
DEFAULT_SESSION_NAME_PREFIX = "drydock-"


class StorageBackendError(Exception):
    """Base class for storage-backend failures."""


class StorageBackendUnavailable(StorageBackendError):
    """Transient failure (network, STS throttling) — retriable."""


class StorageBackendPermissionDenied(StorageBackendError):
    """STS rejected the request (bad role ARN, missing permission, etc.)."""


class StorageBackendConfigError(StorageBackendError):
    """Backend misconfigured — role ARN missing, profile absent, etc."""


@dataclass(frozen=True)
class StorageCredential:
    """Time-bounded scoped cloud credential issued by a StorageBackend.

    Matches the AWS STS shape today; the same fields generalize to other
    cloud providers (GCS short-lived service-account tokens, Azure SAS).
    """

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime

    def to_files(self) -> dict[str, bytes]:
        """Render as files for materialization into /run/secrets/.

        Filenames follow the drydock-base `sync-aws-auth.sh` convention so
        a worker can `export AWS_ACCESS_KEY_ID=$(cat /run/secrets/aws_access_key_id)`
        or point the AWS SDK at environment variables directly.
        """
        return {
            "aws_access_key_id": self.access_key_id.encode("utf-8"),
            "aws_secret_access_key": self.secret_access_key.encode("utf-8"),
            "aws_session_token": self.session_token.encode("utf-8"),
            "aws_session_expiration": self.expiration.isoformat().encode("utf-8"),
        }


@runtime_checkable
class StorageBackend(Protocol):
    """Plugin interface for STORAGE_MOUNT lease issuance."""

    name: str

    def mint(
        self,
        *,
        desk_id: str,
        bucket: str,
        prefix: str,
        mode: str,
    ) -> StorageCredential:
        """Issue a scoped credential for `desk_id` against `bucket`/`prefix`.

        `mode` is one of {"ro", "rw"}. Backends validate and raise
        StorageBackendConfigError for unknown modes.

        Raises:
            StorageBackendConfigError: misconfigured backend (missing role).
            StorageBackendPermissionDenied: auth rejected the request.
            StorageBackendUnavailable: transient, retriable.
        """
        ...

    def mint_provision(
        self,
        *,
        desk_id: str,
        actions: list[str] | tuple[str, ...],
    ) -> StorageCredential:
        """Issue a credential narrowed to an IAM action list on `*`.

        Used by INFRA_PROVISION leases. Same error contract as `mint`.
        """
        ...


def build_session_policy(bucket: str, prefix: str, mode: str) -> dict:
    """Render an inline STS session policy narrowing to bucket/prefix/mode.

    Pure function — tested separately so the rendering stays stable.
    """
    prefix = prefix.strip("/").rstrip("/")
    mode = mode.lower()
    if mode == "ro":
        actions = ["s3:GetObject", "s3:ListBucket"]
    elif mode == "rw":
        actions = [
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:ListBucket",
        ]
    else:
        raise StorageBackendConfigError(f"unknown mode: {mode!r} (expected 'ro' or 'rw')")

    object_arn = (
        f"arn:aws:s3:::{bucket}/{prefix}/*" if prefix else f"arn:aws:s3:::{bucket}/*"
    )
    bucket_arn = f"arn:aws:s3:::{bucket}"

    statements = [{
        "Sid": "ScopedObjectAccess",
        "Effect": "Allow",
        "Action": [a for a in actions if a != "s3:ListBucket"],
        "Resource": [object_arn],
    }]
    if "s3:ListBucket" in actions:
        list_statement: dict = {
            "Sid": "ScopedListBucket",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [bucket_arn],
        }
        if prefix:
            list_statement["Condition"] = {
                "StringLike": {"s3:prefix": [f"{prefix}/*", prefix]},
            }
        statements.append(list_statement)

    return {"Version": "2012-10-17", "Statement": statements}


def build_provision_session_policy(actions: list[str] | tuple[str, ...]) -> dict:
    """Render an inline STS session policy granting IAM actions on `*`.

    Scoped by the caller-declared action list; resources are `*` because
    provisioner drydocks create resources that don't exist yet. The
    permission-boundary on drydock-agent is the ceiling — session policy
    cannot exceed it, regardless of what's in `actions`.
    """
    cleaned: list[str] = []
    for a in actions:
        if not isinstance(a, str) or not a.strip():
            raise StorageBackendConfigError(f"invalid IAM action: {a!r}")
        cleaned.append(a.strip())
    if not cleaned:
        raise StorageBackendConfigError("provision scope must list at least one IAM action")
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "ScopedProvisionAccess",
            "Effect": "Allow",
            "Action": cleaned,
            "Resource": ["*"],
        }],
    }


def _session_name(desk_id: str, prefix: str = DEFAULT_SESSION_NAME_PREFIX) -> str:
    """Construct an AWS STS RoleSessionName matching the trust-policy condition.

    `scripts/aws/trust-policy.json` requires sts:RoleSessionName to match
    `drydock-*`. STS limit is 64 chars.
    """
    raw = f"{prefix}{desk_id}"
    return raw[:64]


class StsAssumeRoleBackend:
    """Mint scoped session credentials via AWS STS AssumeRole + session policy.

    The Harbor is expected to have the `drydock-runner` long-lived IAM
    keys wired into `~/.aws/credentials` under `source_profile`. See
    scripts/aws/README.md.
    """

    name = "sts"

    def __init__(
        self,
        *,
        role_arn: str,
        source_profile: str = "drydock-runner",
        session_duration_seconds: int = DEFAULT_SESSION_DURATION_SECONDS,
        aws_bin: str = "aws",
    ):
        if not role_arn:
            raise StorageBackendConfigError(
                "StsAssumeRoleBackend requires role_arn — set [storage] role_arn in wsd.toml"
            )
        self.role_arn = role_arn
        self.source_profile = source_profile
        self.session_duration_seconds = int(session_duration_seconds)
        self.aws_bin = aws_bin

    def mint(
        self,
        *,
        desk_id: str,
        bucket: str,
        prefix: str,
        mode: str,
    ) -> StorageCredential:
        return self._assume_role(desk_id, build_session_policy(bucket, prefix, mode))

    def mint_provision(
        self,
        *,
        desk_id: str,
        actions: list[str] | tuple[str, ...],
    ) -> StorageCredential:
        return self._assume_role(desk_id, build_provision_session_policy(actions))

    def _assume_role(self, desk_id: str, policy: dict) -> StorageCredential:
        argv = [
            self.aws_bin, "sts", "assume-role",
            "--profile", self.source_profile,
            "--role-arn", self.role_arn,
            "--role-session-name", _session_name(desk_id),
            "--policy", json.dumps(policy, separators=(",", ":")),
            "--duration-seconds", str(self.session_duration_seconds),
            "--output", "json",
        ]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError as exc:
            raise StorageBackendConfigError(
                f"aws CLI not found at {self.aws_bin!r} — install awscli on the Harbor"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise StorageBackendUnavailable(f"aws sts assume-role timed out: {exc}") from exc

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if "AccessDenied" in stderr or "invalid" in stderr.lower():
                raise StorageBackendPermissionDenied(stderr[:400])
            raise StorageBackendUnavailable(stderr[:400] or f"aws exit {proc.returncode}")

        try:
            data = json.loads(proc.stdout)
            creds = data["Credentials"]
            expiration = datetime.fromisoformat(creds["Expiration"].replace("Z", "+00:00"))
            return StorageCredential(
                access_key_id=creds["AccessKeyId"],
                secret_access_key=creds["SecretAccessKey"],
                session_token=creds["SessionToken"],
                expiration=expiration,
            )
        except (KeyError, json.JSONDecodeError, ValueError) as exc:
            raise StorageBackendUnavailable(f"malformed STS response: {exc}") from exc


class StubStorageBackend:
    """In-memory stub used by tests and by development harbors without AWS wired up.

    Returns deterministic fake credentials so handlers + materialization
    can be exercised without touching the network.
    """

    name = "stub"

    def __init__(self, *, fixed_expiration: datetime | None = None):
        self.fixed_expiration = fixed_expiration or datetime.fromtimestamp(
            4102444800, tz=None,
        )
        self.calls: list[dict] = []

    def mint(
        self,
        *,
        desk_id: str,
        bucket: str,
        prefix: str,
        mode: str,
    ) -> StorageCredential:
        # Validate mode shape even in the stub so tests catch bad inputs.
        build_session_policy(bucket, prefix, mode)
        self.calls.append({"desk_id": desk_id, "bucket": bucket, "prefix": prefix, "mode": mode})
        return self._fake(desk_id)

    def mint_provision(
        self,
        *,
        desk_id: str,
        actions: list[str] | tuple[str, ...],
    ) -> StorageCredential:
        build_provision_session_policy(actions)
        self.calls.append({"desk_id": desk_id, "actions": list(actions)})
        return self._fake(desk_id)

    def _fake(self, desk_id: str) -> StorageCredential:
        return StorageCredential(
            access_key_id=f"STUB-{desk_id}-AKID",
            secret_access_key=f"STUB-{desk_id}-SECRET",
            session_token=f"STUB-{desk_id}-TOKEN",
            expiration=self.fixed_expiration,
        )


def build_storage_backend(
    name: str,
    *,
    role_arn: str | None = None,
    source_profile: str = "drydock-runner",
    session_duration_seconds: int = DEFAULT_SESSION_DURATION_SECONDS,
) -> StorageBackend:
    """Construct the configured storage backend.

    Raises ValueError for unknown backend names — the daemon surfaces
    this as `unknown_storage_backend` at startup (see wsd/config.py).
    """
    if name == "sts":
        return StsAssumeRoleBackend(
            role_arn=role_arn or "",
            source_profile=source_profile,
            session_duration_seconds=session_duration_seconds,
        )
    if name == "stub":
        return StubStorageBackend()
    raise ValueError(f"unknown_storage_backend: {name!r}")
