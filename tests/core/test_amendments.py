"""Tests for Phase A0 Amendment contract.

Pin the contracts:
- create_amendment generates am_<8-hex> id, persists envelope
- get_amendment round-trips (request_json → request dict)
- list_amendments respects status + drydock_id filters
- update_amendment_status validates state transitions
- expire_old_pending_amendments only touches pending/escalated past expiry
- Schema migration is additive — V5 lands without disturbing earlier tables
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from drydock.core import WsError
from drydock.core.registry import Registry


@pytest.fixture
def registry(tmp_path):
    db = tmp_path / "registry.db"
    r = Registry(db_path=db)
    yield r
    r.close()


class TestCreateAmendment:
    def test_basic(self, registry):
        a = registry.create_amendment(
            kind="network_reach",
            request={"domain": "github.com", "port": 443},
            proposed_by_type="dockworker",
            proposed_by_id="ws_auction_crawl",
            drydock_id="ws_auction_crawl",
            reason="fetching deps",
        )
        assert a["id"].startswith("am_")
        assert len(a["id"]) == 11  # 'am_' + 8 hex chars
        assert a["kind"] == "network_reach"
        assert a["request"] == {"domain": "github.com", "port": 443}
        assert a["status"] == "pending"
        assert a["reason"] == "fetching deps"

    def test_generates_unique_ids(self, registry):
        a1 = registry.create_amendment(
            kind="network_reach", request={},
            proposed_by_type="principal", proposed_by_id="principal",
        )
        a2 = registry.create_amendment(
            kind="network_reach", request={},
            proposed_by_type="principal", proposed_by_id="principal",
        )
        assert a1["id"] != a2["id"]

    def test_principal_can_initial_status_approved(self, registry):
        a = registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal", status="approved",
        )
        assert a["status"] == "approved"


class TestGetAmendment:
    def test_returns_none_for_missing(self, registry):
        assert registry.get_amendment("am_nonexistent") is None

    def test_request_field_is_parsed_dict(self, registry):
        a = registry.create_amendment(
            kind="x", request={"a": 1, "b": [2, 3]},
            proposed_by_type="principal", proposed_by_id="principal",
        )
        loaded = registry.get_amendment(a["id"])
        assert loaded["request"] == {"a": 1, "b": [2, 3]}
        assert "request_json" not in loaded


class TestListAmendments:
    def test_empty(self, registry):
        assert registry.list_amendments() == []

    def test_filter_by_status(self, registry):
        registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal", status="pending",
        )
        registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal", status="denied",
        )
        pending = registry.list_amendments(status="pending")
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"
        denied = registry.list_amendments(status="denied")
        assert len(denied) == 1

    def test_filter_by_drydock(self, registry):
        registry.create_amendment(
            kind="x", request={}, proposed_by_type="dockworker",
            proposed_by_id="ws_a", drydock_id="ws_a",
        )
        registry.create_amendment(
            kind="x", request={}, proposed_by_type="dockworker",
            proposed_by_id="ws_b", drydock_id="ws_b",
        )
        a_only = registry.list_amendments(drydock_id="ws_a")
        assert len(a_only) == 1
        assert a_only[0]["drydock_id"] == "ws_a"

    def test_newest_first(self, registry):
        ids = []
        for _ in range(3):
            a = registry.create_amendment(
                kind="x", request={}, proposed_by_type="principal",
                proposed_by_id="principal",
            )
            ids.append(a["id"])
        listed = registry.list_amendments()
        # Most recent first
        assert [a["id"] for a in listed] == ids[::-1]

    def test_limit_respected(self, registry):
        for _ in range(5):
            registry.create_amendment(
                kind="x", request={}, proposed_by_type="principal",
                proposed_by_id="principal",
            )
        result = registry.list_amendments(limit=2)
        assert len(result) == 2


class TestUpdateAmendmentStatus:
    def test_basic_status_change(self, registry):
        a = registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal",
        )
        updated = registry.update_amendment_status(
            a["id"], status="approved", reviewed_by="principal",
            review_note="looks good",
        )
        assert updated["status"] == "approved"
        assert updated["reviewed_by"] == "principal"
        assert updated["review_note"] == "looks good"
        assert updated["reviewed_at"] is not None

    def test_reviewed_at_only_set_once(self, registry):
        a = registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal",
        )
        updated1 = registry.update_amendment_status(
            a["id"], status="approved", reviewed_by="principal",
        )
        first_reviewed_at = updated1["reviewed_at"]
        # Second update — reviewed_at should be preserved (COALESCE in SQL)
        updated2 = registry.update_amendment_status(
            a["id"], status="applied", applied_at="2026-05-05T12:00:00Z",
        )
        assert updated2["reviewed_at"] == first_reviewed_at

    def test_missing_amendment_raises(self, registry):
        with pytest.raises(WsError, match="not found"):
            registry.update_amendment_status("am_nonexistent", status="approved")


class TestExpireOldPendingAmendments:
    def test_expires_pending_past_expires_at(self, registry):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        a = registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal", expires_at=past,
        )
        n = registry.expire_old_pending_amendments()
        assert n == 1
        loaded = registry.get_amendment(a["id"])
        assert loaded["status"] == "expired"

    def test_does_not_expire_future_amendments(self, registry):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        a = registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal", expires_at=future,
        )
        n = registry.expire_old_pending_amendments()
        assert n == 0
        loaded = registry.get_amendment(a["id"])
        assert loaded["status"] == "pending"

    def test_does_not_touch_approved_amendments(self, registry):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        a = registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal", expires_at=past, status="approved",
        )
        registry.expire_old_pending_amendments()
        loaded = registry.get_amendment(a["id"])
        # Approved stays approved despite expires_at being past
        assert loaded["status"] == "approved"

    def test_no_expires_at_never_expires(self, registry):
        a = registry.create_amendment(
            kind="x", request={}, proposed_by_type="principal",
            proposed_by_id="principal",
        )
        n = registry.expire_old_pending_amendments()
        assert n == 0
        assert registry.get_amendment(a["id"])["status"] == "pending"


class TestMigration:
    def test_v5_table_created(self, registry):
        # Migration ran on registry init; verify amendments table exists
        cols = {r["name"] for r in registry._conn.execute(
            "PRAGMA table_info('amendments')").fetchall()}
        assert "id" in cols
        assert "kind" in cols
        assert "status" in cols
        assert "request_json" in cols
        # Check constraint via insert that should fail
        with pytest.raises(Exception):  # CHECK constraint violation
            registry._conn.execute(
                "INSERT INTO amendments (id, proposed_by_type, proposed_by_id, "
                "proposed_at, kind, request_json, status) VALUES "
                "('am_x', 'invalid_type', 'p', '2026-01-01', 'x', '{}', 'pending')"
            )
