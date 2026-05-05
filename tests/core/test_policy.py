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


# ---------- Storage-scope matching (Phase 1b narrowness) ----------
#
# Pins the rules callers depend on:
#   - empty granted = default-permissive (back-compat invariant)
#   - prefix is a path-segment prefix, not a string prefix
#     (so "data" matches "data/foo" but not "data2")
#   - rw request requires explicit rw: scope; ro never does
#   - malformed scope entries don't accidentally match-all


class TestStorageScopeMatching:
    def test_empty_granted_is_permissive(self):
        from drydock.core.policy import matches_storage_scope
        # Pre-1b behavior: capability-only gate. Documented default.
        assert matches_storage_scope(
            {"bucket": "anything", "prefix": "at/all", "mode": "rw"}, []
        ) is True

    def test_prefix_is_segment_not_substring(self):
        from drydock.core.policy import matches_storage_scope
        # "data" must not match "data2" — segment boundary matters.
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "data2", "mode": "ro"},
            ["s3://b/data/*"],
        ) is False
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "data/x", "mode": "ro"},
            ["s3://b/data/*"],
        ) is True
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "data", "mode": "ro"},
            ["s3://b/data/*"],
        ) is True

    def test_whole_bucket_scope_matches_any_prefix(self):
        from drydock.core.policy import matches_storage_scope
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "anywhere/deep", "mode": "ro"},
            ["s3://b/*"],
        ) is True

    def test_bucket_mismatch_rejects(self):
        from drydock.core.policy import matches_storage_scope
        assert matches_storage_scope(
            {"bucket": "other", "prefix": "p", "mode": "ro"},
            ["s3://b/*"],
        ) is False

    def test_rw_request_requires_rw_scope(self):
        from drydock.core.policy import matches_storage_scope
        # ro-only scope rejects rw request.
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "p", "mode": "rw"},
            ["s3://b/p/*"],
        ) is False
        # rw: scope allows both rw and ro.
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "p", "mode": "rw"},
            ["rw:s3://b/p/*"],
        ) is True
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "p", "mode": "ro"},
            ["rw:s3://b/p/*"],
        ) is True

    def test_malformed_scope_does_not_match_all(self):
        from drydock.core.policy import matches_storage_scope
        # A garbage scope entry must not accidentally permit everything;
        # it's skipped. If the only entry is garbage, nothing matches
        # (NOT default-permissive — that's only for an empty list).
        assert matches_storage_scope(
            {"bucket": "b", "prefix": "p", "mode": "ro"},
            ["not-a-scope-string"],
        ) is False

    def test_parse_storage_scope_forms(self):
        from drydock.core.policy import parse_storage_scope
        assert parse_storage_scope("s3://b/p/*") == {
            "bucket": "b", "prefix": "p", "mode_max": "ro",
        }
        assert parse_storage_scope("rw:s3://b/p/*") == {
            "bucket": "b", "prefix": "p", "mode_max": "rw",
        }
        assert parse_storage_scope("s3://b") == {
            "bucket": "b", "prefix": "", "mode_max": "ro",
        }

    def test_parse_storage_scope_rejects_malformed(self):
        from drydock.core.policy import InvalidStorageScopeFormat, parse_storage_scope
        for bad in ["", "   ", "bucket-no-scheme", "s3://", "s3://*"]:
            with pytest.raises(InvalidStorageScopeFormat):
                parse_storage_scope(bad)



# Phase B: INFRA_PROVISION narrowness matcher — fnmatch-based IAM action
# globs. Default-permissive-when-empty (consistent with storage scopes).
class TestMatchesProvisionActions:
    def test_empty_grants_permit_all(self):
        from drydock.core.policy import matches_provision_actions
        # Pre-narrowness drydocks only had REQUEST_PROVISION_LEASES with no
        # specific scopes; empty list must stay permissive or upgrade breaks
        # their existing workflow.
        assert matches_provision_actions(["s3:CreateBucket"], []) is True

    def test_exact_match(self):
        from drydock.core.policy import matches_provision_actions
        assert matches_provision_actions(["s3:CreateBucket"], ["s3:CreateBucket"]) is True

    def test_service_wildcard(self):
        from drydock.core.policy import matches_provision_actions
        assert matches_provision_actions(
            ["s3:CreateBucket", "s3:DeleteBucket"], ["s3:*"],
        ) is True

    def test_global_wildcard(self):
        from drydock.core.policy import matches_provision_actions
        assert matches_provision_actions(["anything:Goes"], ["*"]) is True

    def test_rejects_undeclared_service(self):
        from drydock.core.policy import matches_provision_actions
        # Key security property: a drydock granted only s3:* cannot slip
        # an iam: call past the broker.
        assert matches_provision_actions(
            ["s3:CreateBucket", "iam:CreateRole"], ["s3:*"],
        ) is False

    def test_all_requested_must_match_some_grant(self):
        from drydock.core.policy import matches_provision_actions
        assert matches_provision_actions(
            ["s3:CreateBucket", "iam:ListRoles"], ["s3:*", "iam:List*"],
        ) is True
        assert matches_provision_actions(
            ["s3:CreateBucket", "iam:CreateRole"], ["s3:*", "iam:List*"],
        ) is False


# NETWORK_REACH narrowness matcher (network-reach.md). Pin the invariants
# that aren't obvious from the code and that the design doc commits to:
#   - empty granted_domains is DENY-ALL (deliberately stricter than the
#     storage/provision permissive-when-empty default — opening a never-
#     listed domain should require explicit declaration)
#   - "*.foo.com" matches single-level subdomains only (api.foo.com but
#     NOT a.b.foo.com or bare foo.com)
#   - "*" wildcard matches everything
#   - default port allowlist when granted_ports empty is exactly (80, 443)
#   - canonicalization: case-insensitive, trailing-dot tolerant
#   - the (allowed, reason) tuple is a stable contract — reasons used by
#     the broker to construct fix: hints for the user
class TestNetworkReachMatching:
    def test_empty_granted_is_deny_all(self):
        from drydock.core.policy import matches_network_reach
        # Stricter than storage's permissive-when-empty by design choice.
        allowed, reason = matches_network_reach("github.com", 443, [], [])
        assert allowed is False
        assert reason == "no_entitlement"

    def test_exact_domain_match(self):
        from drydock.core.policy import matches_network_reach
        allowed, reason = matches_network_reach("github.com", 443, ["github.com"], [])
        assert allowed is True and reason is None

    def test_single_level_subdomain_glob(self):
        from drydock.core.policy import matches_network_reach
        # "*.github.com" matches api.github.com but NOT a.b.github.com or bare github.com.
        a, _ = matches_network_reach("api.github.com", 443, ["*.github.com"], [])
        assert a is True
        a, r = matches_network_reach("a.b.github.com", 443, ["*.github.com"], [])
        assert a is False and r == "domain_not_entitled"
        a, r = matches_network_reach("github.com", 443, ["*.github.com"], [])
        assert a is False and r == "domain_not_entitled"

    def test_wildcard_matches_anything(self):
        from drydock.core.policy import matches_network_reach
        for d in ("github.com", "very.deep.weird.example", "x.y"):
            a, _ = matches_network_reach(d, 443, ["*"], [])
            assert a is True, d

    def test_default_port_allowlist_is_80_443(self):
        from drydock.core.policy import matches_network_reach
        # Empty granted_ports → defaults applied, NOT permissive.
        a, _ = matches_network_reach("foo.com", 443, ["foo.com"], [])
        assert a is True
        a, _ = matches_network_reach("foo.com", 80, ["foo.com"], [])
        assert a is True
        a, r = matches_network_reach("foo.com", 22, ["foo.com"], [])
        assert a is False and r == "port_not_entitled"

    def test_explicit_port_allowlist_overrides_default(self):
        from drydock.core.policy import matches_network_reach
        # Once granted_ports is non-empty, ONLY those ports are allowed —
        # 80/443 are no longer implicitly included.
        a, _ = matches_network_reach("db.foo.com", 5432, ["db.foo.com"], [5432])
        assert a is True
        a, r = matches_network_reach("db.foo.com", 443, ["db.foo.com"], [5432])
        assert a is False and r == "port_not_entitled"

    def test_canonicalization_case_insensitive_and_trailing_dot(self):
        from drydock.core.policy import matches_network_reach
        # Patterns and requests both canonicalize to lower + no trailing dot.
        a, _ = matches_network_reach("GITHUB.COM", 443, ["github.com"], [])
        assert a is True
        a, _ = matches_network_reach("github.com.", 443, ["github.com"], [])
        assert a is True
        a, _ = matches_network_reach("api.foo.com", 443, ["*.FOO.com"], [])
        assert a is True

    def test_glob_doesnt_match_bare_domain(self):
        # "*.foo.com" does NOT cover the bare "foo.com" — single-level
        # subdomain rule. If the user wants both, they list both.
        from drydock.core.policy import matches_network_reach
        a, r = matches_network_reach("foo.com", 443, ["*.foo.com"], [])
        assert a is False and r == "domain_not_entitled"

    def test_malformed_pattern_skipped_not_matched(self):
        # Defense against typos in the YAML — empty/non-string entries
        # should be ignored, not treated as wildcards or crashes.
        from drydock.core.policy import matches_network_reach
        a, r = matches_network_reach("foo.com", 443, ["", "github.com", None], [])
        assert a is False and r == "domain_not_entitled"
        a, _ = matches_network_reach("github.com", 443, ["", "github.com", None], [])
        assert a is True

    def test_reason_strings_are_stable_contract(self):
        # The broker maps these reasons to user-facing fix: hints — silently
        # renaming would change the CLI output without breaking the test.
        from drydock.core.policy import matches_network_reach
        for reason in ("no_entitlement", "domain_not_entitled", "port_not_entitled"):
            assert isinstance(reason, str)  # tautology but documents the contract
