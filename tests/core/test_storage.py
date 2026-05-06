"""Tests for the storage-lease backends (V4 Phase 1)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from drydock.core.storage import (
    StorageBackendConfigError,
    StorageBackendPermissionDenied,
    StorageBackendUnavailable,
    StorageCredential,
    StsAssumeRoleBackend,
    StubStorageBackend,
    build_provision_session_policy,
    build_session_policy,
    build_storage_backend,
)


# build_session_policy — pure function; pin the rendered shape so future
# changes to statement structure are visible in tests.
class TestBuildSessionPolicy:
    def test_ro_with_prefix(self):
        policy = build_session_policy("mybucket", "data/scraped", "ro")
        assert policy["Version"] == "2012-10-17"
        statements = policy["Statement"]
        assert len(statements) == 2

        obj_stmt = next(s for s in statements if s["Sid"] == "ScopedObjectAccess")
        assert set(obj_stmt["Action"]) == {"s3:GetObject"}
        assert obj_stmt["Resource"] == ["arn:aws:s3:::mybucket/data/scraped/*"]

        list_stmt = next(s for s in statements if s["Sid"] == "ScopedListBucket")
        assert list_stmt["Action"] == ["s3:ListBucket"]
        assert list_stmt["Resource"] == ["arn:aws:s3:::mybucket"]
        assert list_stmt["Condition"]["StringLike"]["s3:prefix"] == [
            "data/scraped/*", "data/scraped",
        ]

    def test_rw_no_prefix(self):
        policy = build_session_policy("mybucket", "", "rw")
        obj_stmt = next(s for s in policy["Statement"] if s["Sid"] == "ScopedObjectAccess")
        assert set(obj_stmt["Action"]) == {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}
        assert obj_stmt["Resource"] == ["arn:aws:s3:::mybucket/*"]
        list_stmt = next(s for s in policy["Statement"] if s["Sid"] == "ScopedListBucket")
        assert "Condition" not in list_stmt  # no prefix -> unconditional ListBucket

    def test_invalid_mode_raises(self):
        with pytest.raises(StorageBackendConfigError, match="unknown mode"):
            build_session_policy("mybucket", "", "admin")


# Phase B: provision session policy is the minimal STS statement — grants
# caller's declared action list on Resource:*. Pin the shape so future
# callers can't accidentally widen it (e.g., adding a second statement with
# broader resources).
class TestBuildProvisionSessionPolicy:
    def test_renders_exact_shape(self):
        policy = build_provision_session_policy(["s3:CreateBucket", "iam:PutRolePolicy"])
        assert policy == {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "ScopedProvisionAccess",
                "Effect": "Allow",
                "Action": ["s3:CreateBucket", "iam:PutRolePolicy"],
                "Resource": ["*"],
            }],
        }

    def test_empty_actions_raises(self):
        with pytest.raises(StorageBackendConfigError):
            build_provision_session_policy([])

    def test_invalid_action_type_raises(self):
        with pytest.raises(StorageBackendConfigError):
            build_provision_session_policy(["s3:CreateBucket", ""])


class TestStubStorageBackend:
    def test_returns_deterministic_creds(self):
        backend = StubStorageBackend()
        cred = backend.mint(drydock_id="dock_x", bucket="b", prefix="p", mode="ro")
        assert cred.access_key_id == "STUB-dock_x-AKID"
        assert cred.secret_access_key == "STUB-dock_x-SECRET"
        assert cred.session_token == "STUB-dock_x-TOKEN"
        assert isinstance(cred.expiration, datetime)

    def test_records_calls(self):
        backend = StubStorageBackend()
        backend.mint(drydock_id="dock_a", bucket="b", prefix="p", mode="rw")
        backend.mint(drydock_id="dock_b", bucket="c", prefix="", mode="ro")
        assert len(backend.calls) == 2
        assert backend.calls[0] == {"drydock_id": "dock_a", "bucket": "b", "prefix": "p", "mode": "rw"}

    def test_invalid_mode_rejected_even_in_stub(self):
        backend = StubStorageBackend()
        with pytest.raises(StorageBackendConfigError):
            backend.mint(drydock_id="dock_x", bucket="b", prefix="", mode="admin")


# StsAssumeRoleBackend contract: shells out to `aws sts assume-role` with
# the correct args + session policy, parses the Credentials block.
# Tests patch subprocess.run so no network calls happen.
class TestStsAssumeRoleBackend:
    def test_requires_role_arn(self):
        with pytest.raises(StorageBackendConfigError, match="role_arn"):
            StsAssumeRoleBackend(role_arn="")

    def test_happy_path(self):
        backend = StsAssumeRoleBackend(
            role_arn="arn:aws:iam::123:role/drydock-agent",
            source_profile="drydock-runner",
        )
        fake_output = json.dumps({
            "Credentials": {
                "AccessKeyId": "AKID123",
                "SecretAccessKey": "SECRET456",
                "SessionToken": "TOKEN789",
                "Expiration": "2026-04-18T04:00:00Z",
            }
        })
        mock_result = MagicMock(returncode=0, stdout=fake_output, stderr="")
        with patch("drydock.core.storage.subprocess.run", return_value=mock_result) as mock_run:
            cred = backend.mint(drydock_id="dock_foo", bucket="mybucket", prefix="data", mode="ro")
        argv = mock_run.call_args[0][0]
        assert argv[:3] == ["aws", "sts", "assume-role"]
        assert "--role-arn" in argv
        assert "arn:aws:iam::123:role/drydock-agent" in argv
        assert "--role-session-name" in argv
        # Session name follows the trust-policy prefix.
        session_idx = argv.index("--role-session-name")
        assert argv[session_idx + 1].startswith("drydock-")
        # Policy JSON is passed inline.
        assert "--policy" in argv
        policy_idx = argv.index("--policy")
        parsed_policy = json.loads(argv[policy_idx + 1])
        assert parsed_policy["Version"] == "2012-10-17"

        assert cred.access_key_id == "AKID123"
        assert cred.secret_access_key == "SECRET456"
        assert cred.session_token == "TOKEN789"
        assert cred.expiration == datetime(2026, 4, 18, 4, 0, 0, tzinfo=timezone.utc)

    def test_access_denied_maps_to_permission_denied(self):
        backend = StsAssumeRoleBackend(role_arn="arn:aws:iam::123:role/x")
        mock_result = MagicMock(
            returncode=254,
            stdout="",
            stderr="An error occurred (AccessDenied) when calling AssumeRole",
        )
        with patch("drydock.core.storage.subprocess.run", return_value=mock_result):
            with pytest.raises(StorageBackendPermissionDenied):
                backend.mint(drydock_id="dock_x", bucket="b", prefix="", mode="ro")

    def test_other_sts_errors_map_to_unavailable(self):
        backend = StsAssumeRoleBackend(role_arn="arn:aws:iam::123:role/x")
        mock_result = MagicMock(
            returncode=1, stdout="", stderr="Could not connect to the endpoint URL",
        )
        with patch("drydock.core.storage.subprocess.run", return_value=mock_result):
            with pytest.raises(StorageBackendUnavailable):
                backend.mint(drydock_id="dock_x", bucket="b", prefix="", mode="ro")

    def test_malformed_response_maps_to_unavailable(self):
        backend = StsAssumeRoleBackend(role_arn="arn:aws:iam::123:role/x")
        mock_result = MagicMock(returncode=0, stdout="not json", stderr="")
        with patch("drydock.core.storage.subprocess.run", return_value=mock_result):
            with pytest.raises(StorageBackendUnavailable):
                backend.mint(drydock_id="dock_x", bucket="b", prefix="", mode="ro")


class TestBuildStorageBackend:
    def test_stub(self):
        backend = build_storage_backend("stub")
        assert isinstance(backend, StubStorageBackend)

    def test_sts_requires_role_arn(self):
        with pytest.raises(StorageBackendConfigError):
            build_storage_backend("sts")

    def test_unknown_raises_valueerror(self):
        with pytest.raises(ValueError, match="unknown_storage_backend"):
            build_storage_backend("whatever")


class TestStorageCredentialFiles:
    def test_to_files_shape(self):
        cred = StorageCredential(
            access_key_id="AKID",
            secret_access_key="SECRET",
            session_token="TOKEN",
            expiration=datetime(2026, 4, 18, 4, 0, tzinfo=timezone.utc),
        )
        files = cred.to_files()
        assert set(files.keys()) == {
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "aws_session_expiration",
        }
        assert files["aws_access_key_id"] == b"AKID"
        assert files["aws_session_expiration"].decode() == "2026-04-18T04:00:00+00:00"
