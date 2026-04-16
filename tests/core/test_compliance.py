"""Tests for compliance.yaml parser."""

from datetime import date, timedelta

import logging
import pytest

from drydock.core import WsError
from drydock.core.compliance import (
    ComplianceConfig,
    days_until_review,
    is_stale,
    load_compliance,
)


class TestLoadCompliance:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_compliance(tmp_path) is None

    def test_empty_yaml_returns_default(self, tmp_path):
        (tmp_path / "compliance.yaml").write_text("")
        cfg = load_compliance(tmp_path)
        assert cfg is not None
        assert cfg.sensitivity is None
        assert cfg.tradeoffs_accepted == []

    def test_parses_full_schema(self, tmp_path):
        (tmp_path / "compliance.yaml").write_text(
            "sensitivity: hipaa-adjacent\n"
            "tradeoffs_accepted:\n"
            "  - id: t1\n"
            "    reference: docs/x.md\n"
            "    rationale: because\n"
            "hosting:\n"
            "  primary_cloud: aws\n"
            "  primary_region: us-west-2\n"
            "secret_classes: [aws_access_key_id]\n"
            "last_reviewed: 2026-04-15\n"
            "reviewed_by: stevenfan\n"
            "review_cadence_days: 90\n"
        )
        cfg = load_compliance(tmp_path)
        assert cfg.sensitivity == "hipaa-adjacent"
        assert cfg.last_reviewed == date(2026, 4, 15)
        assert cfg.review_cadence_days == 90
        assert cfg.hosting["primary_cloud"] == "aws"
        assert len(cfg.tradeoffs_accepted) == 1

    def test_invalid_yaml_raises_wserror(self, tmp_path):
        (tmp_path / "compliance.yaml").write_text(":\n  - :\n  bad: [")
        with pytest.raises(WsError) as exc_info:
            load_compliance(tmp_path)
        assert "Invalid YAML" in exc_info.value.message

    def test_non_mapping_raises_wserror(self, tmp_path):
        (tmp_path / "compliance.yaml").write_text("- one\n- two\n")
        with pytest.raises(WsError) as exc_info:
            load_compliance(tmp_path)
        assert "mapping" in exc_info.value.message.lower()

    # Compliance.yaml is human-edited and may evolve ahead of the parser.
    # Unknown keys must NOT raise — that's the documented divergence from
    # project_config.py which DOES reject unknowns.
    def test_unknown_keys_log_warning_not_raise(self, tmp_path, caplog):
        (tmp_path / "compliance.yaml").write_text(
            "sensitivity: internal\n"
            "future_field: someday\n"
        )
        with caplog.at_level(logging.WARNING, logger="drydock.core.compliance"):
            cfg = load_compliance(tmp_path)
        assert cfg.sensitivity == "internal"
        assert any("future_field" in rec.message for rec in caplog.records)


class TestStaleness:
    def _cfg(self, last: date | None, cadence: int | None) -> ComplianceConfig:
        return ComplianceConfig(last_reviewed=last, review_cadence_days=cadence)

    def test_inside_window_not_stale(self):
        today = date(2026, 4, 16)
        cfg = self._cfg(today - timedelta(days=30), 90)
        assert is_stale(cfg, today) is False
        assert days_until_review(cfg, today) == 60

    def test_overdue_is_stale(self):
        today = date(2026, 4, 16)
        cfg = self._cfg(today - timedelta(days=120), 90)
        assert is_stale(cfg, today) is True
        assert days_until_review(cfg, today) == -30

    def test_missing_fields_undetermined(self):
        assert is_stale(self._cfg(None, 90)) is False
        assert is_stale(self._cfg(date.today(), None)) is False
        assert days_until_review(self._cfg(None, 90)) is None
