"""Tests for the pure spawn-policy validator and canonicalizers."""

import builtins
import socket
import subprocess
import urllib.request

import pytest

from drydock.core.policy import (
    Allow,
    CapabilityKind,
    DeskPolicy,
    DeskSpec,
    InvalidDomainFormat,
    InvalidMountFormat,
    Reject,
    canonicalize_domain,
    canonicalize_mount,
    validate_spawn,
)


def _parent_policy(
    *,
    domains: frozenset[str] = frozenset({"api.example.com", "example.com"}),
    secrets: frozenset[str] = frozenset({"db_password", "api_token"}),
    capabilities: frozenset[CapabilityKind] = frozenset(
        {CapabilityKind.SPAWN_CHILDREN, CapabilityKind.REQUEST_SECRET_LEASES}
    ),
    mounts: frozenset[tuple[str, str, str]] = frozenset({("foo", "/x", "volume"), ("/a", "/c", "bind")}),
) -> DeskPolicy:
    return DeskPolicy(
        delegatable_firewall_domains=domains,
        delegatable_secrets=secrets,
        capabilities=capabilities,
        extra_mounts=mounts,
    )


def _child_spec(
    *,
    domains: frozenset[str] = frozenset({"example.com"}),
    secrets: frozenset[str] = frozenset({"db_password"}),
    capabilities: frozenset[CapabilityKind] = frozenset({CapabilityKind.SPAWN_CHILDREN}),
    mounts: frozenset[tuple[str, str, str]] = frozenset({("foo", "/x", "volume")}),
) -> DeskSpec:
    return DeskSpec(
        firewall_extra_domains=domains,
        secret_entitlements=secrets,
        capabilities=capabilities,
        extra_mounts=mounts,
    )


def test_firewall_subset_allows_spawn():
    result = validate_spawn(_parent_policy(), _child_spec(domains=frozenset({"EXAMPLE.COM."})))
    assert result == Allow()


def test_firewall_superset_rejects_with_offending_domain():
    result = validate_spawn(
        _parent_policy(domains=frozenset({"example.com"})),
        _child_spec(domains=frozenset({"example.com", "api.example.com"})),
    )
    assert result == Reject(
        rule="firewall_narrowness",
        parent_value=frozenset({"example.com"}),
        requested_value=frozenset({"api.example.com", "example.com"}),
        offending_item="api.example.com",
        fix_hint="Request a subset of the parent's delegatable firewall domains.",
    )


def test_secret_subset_allows_spawn():
    result = validate_spawn(
        _parent_policy(secrets=frozenset({"db_password", "api_token"})),
        _child_spec(secrets=frozenset({"api_token"})),
    )
    assert result == Allow()


def test_secret_superset_rejects_with_offending_secret():
    result = validate_spawn(
        _parent_policy(secrets=frozenset({"db_password"})),
        _child_spec(secrets=frozenset({"db_password", "api_token"})),
    )
    assert isinstance(result, Reject)
    assert result.rule == "secret_narrowness"
    assert result.offending_item == "api_token"


def test_capability_subset_allows_spawn():
    result = validate_spawn(
        _parent_policy(capabilities=frozenset({CapabilityKind.SPAWN_CHILDREN})),
        _child_spec(capabilities=frozenset({CapabilityKind.SPAWN_CHILDREN})),
    )
    assert result == Allow()


def test_capability_superset_rejects_with_offending_capability():
    result = validate_spawn(
        _parent_policy(capabilities=frozenset({CapabilityKind.SPAWN_CHILDREN})),
        _child_spec(
            capabilities=frozenset(
                {CapabilityKind.SPAWN_CHILDREN, CapabilityKind.REQUEST_SECRET_LEASES}
            )
        ),
    )
    assert isinstance(result, Reject)
    assert result.rule == "capability_narrowness"
    assert result.offending_item == CapabilityKind.REQUEST_SECRET_LEASES


def test_mount_subset_allows_spawn():
    result = validate_spawn(
        _parent_policy(mounts=frozenset({("foo", "/x", "volume"), ("/a", "/c", "bind")})),
        _child_spec(mounts=frozenset({("foo/./", "/x/./", "volume")})),
    )
    assert result == Allow()


def test_mount_superset_rejects_with_offending_mount():
    result = validate_spawn(
        _parent_policy(mounts=frozenset({("foo", "/x", "volume")})),
        _child_spec(mounts=frozenset({("foo", "/x", "volume"), ("bar", "/y", "volume")})),
    )
    assert isinstance(result, Reject)
    assert result.rule == "mount_narrowness"
    assert result.offending_item == ("bar", "/y", "volume")


def test_domain_non_ascii_rejected():
    with pytest.raises(InvalidDomainFormat):
        canonicalize_domain("пример.рф")


def test_domain_trailing_dot_stripped():
    assert canonicalize_domain("example.com.") == "example.com"


def test_domain_uppercase_lowercased():
    assert canonicalize_domain("ExAmPle.COM") == "example.com"


def test_domain_wildcard_rejected():
    with pytest.raises(InvalidDomainFormat):
        canonicalize_domain("*.example.com")


def test_domain_port_rejected():
    with pytest.raises(InvalidDomainFormat):
        canonicalize_domain("example.com:443")


def test_mount_string_parses_to_canonical_tuple():
    assert canonicalize_mount("source=foo,target=/x,type=volume") == ("foo", "/x", "volume")


def test_mount_target_parent_reference_rejected():
    with pytest.raises(InvalidMountFormat):
        canonicalize_mount("source=foo,target=../x,type=volume")


def test_bind_mount_source_normalized():
    assert canonicalize_mount("type=bind,source=/a/b/..,target=/c") == ("/a", "/c", "bind")


def test_mount_redundant_segments_canonicalize_to_same_tuple():
    left = canonicalize_mount("source=foo,target=/x,type=volume")
    right = canonicalize_mount("source=foo/./,target=/x/./,type=volume")
    assert left == right


def test_mount_subset_uses_tuple_equality_not_string_prefix():
    result = validate_spawn(
        _parent_policy(mounts=frozenset({("foo-bar", "/x", "volume")})),
        _child_spec(mounts=frozenset({("foo", "/x", "volume")})),
    )
    assert isinstance(result, Reject)
    assert result.rule == "mount_narrowness"
    assert result.offending_item == ("foo", "/x", "volume")


def test_validate_spawn_pure_under_io_guards(monkeypatch):
    def _deny_io(*args, **kwargs):
        raise RuntimeError("no I/O allowed")

    monkeypatch.setattr(builtins, "open", _deny_io)
    monkeypatch.setattr(subprocess, "run", _deny_io)
    monkeypatch.setattr(urllib.request, "urlopen", _deny_io)
    monkeypatch.setattr(socket, "socket", _deny_io)

    passing = validate_spawn(_parent_policy(), _child_spec())
    failing = validate_spawn(
        _parent_policy(domains=frozenset({"example.com"})),
        _child_spec(domains=frozenset({"other.example.com"})),
    )

    assert passing == Allow()
    assert isinstance(failing, Reject)
    assert failing.rule == "firewall_narrowness"


def test_validate_spawn_is_deterministic():
    parent = _parent_policy(
        domains=frozenset({"example.com", "api.example.com"}),
        mounts=frozenset({("foo/./", "/x/./", "volume"), ("/a/b/..", "/c", "bind")}),
    )
    child = _child_spec(
        domains=frozenset({"EXAMPLE.COM."}),
        mounts=frozenset({("foo", "/x", "volume")}),
    )

    first = validate_spawn(parent, child)
    for _ in range(100):
        assert validate_spawn(parent, child) == first
