"""Per-project compliance.yaml parser.

Schema is intentionally permissive — compliance.yaml is hand-edited by
humans tracking real posture, and may grow new fields ahead of this parser.
Unknown top-level keys log a warning rather than raising, in contrast to
project_config.py which rejects unknowns to catch typos in machine-edited
defaults.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from . import WsError

logger = logging.getLogger(__name__)

KNOWN_KEYS = {
    "sensitivity",
    "tradeoffs_accepted",
    "hosting",
    "secret_classes",
    "last_reviewed",
    "reviewed_by",
    "review_cadence_days",
}


@dataclass
class ComplianceConfig:
    sensitivity: str | None = None
    tradeoffs_accepted: list[dict] = field(default_factory=list)
    hosting: dict = field(default_factory=dict)
    secret_classes: list[str] = field(default_factory=list)
    last_reviewed: date | None = None
    reviewed_by: str | None = None
    review_cadence_days: int | None = None


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            logger.warning("compliance: unparseable last_reviewed value %r", value)
            return None
    return None


def load_compliance(drydock_root: Path) -> ComplianceConfig | None:
    path = Path(drydock_root) / "compliance.yaml"
    if not path.exists():
        return None

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise WsError(
            message=f"Invalid YAML in {path}: {e}",
            fix=f"Check {path} for syntax errors",
        )

    if raw is None:
        return ComplianceConfig()

    if not isinstance(raw, dict):
        raise WsError(
            message=f"compliance.yaml at {path} must be a YAML mapping, got {type(raw).__name__}",
            fix=f"Rewrite {path} as key-value pairs",
        )

    unknown = set(raw.keys()) - KNOWN_KEYS
    for key in sorted(unknown):
        logger.warning("compliance: unknown top-level key %r in %s (ignored)", key, path)

    return ComplianceConfig(
        sensitivity=raw.get("sensitivity"),
        tradeoffs_accepted=raw.get("tradeoffs_accepted") or [],
        hosting=raw.get("hosting") or {},
        secret_classes=raw.get("secret_classes") or [],
        last_reviewed=_coerce_date(raw.get("last_reviewed")),
        reviewed_by=raw.get("reviewed_by"),
        review_cadence_days=raw.get("review_cadence_days"),
    )


def days_until_review(cfg: ComplianceConfig, today: date | None = None) -> int | None:
    if cfg.last_reviewed is None or cfg.review_cadence_days is None:
        return None
    today = today or date.today()
    deadline_offset = (cfg.last_reviewed - today).days + cfg.review_cadence_days
    return deadline_offset


def is_stale(cfg: ComplianceConfig, today: date | None = None) -> bool:
    days = days_until_review(cfg, today)
    if days is None:
        return False
    return days < 0
