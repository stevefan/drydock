"""Wire-boundary sanitizer for the Auditor clarification channel.

Per docs/design/port-auditor.md + memory/project_auditor_isolation_principles
(2026-05-07):

    There is exactly one sanctioned channel from worker drydocks to
    the Auditor's judgment context. Sanitization happens at the wire
    boundary (here), not at the LLM. The Auditor receives only the
    sanitized record; pre-sanitized input never reaches its prompt.

    The mere act of using the channel is a high-weight audit event.
    Workers should rarely need it — the contract (lease + project YAML
    + workload registration) is the primary surface. Clarification is
    for the rare case the contract doesn't cover.

The four guardrails this module enforces:

1. Structural `kind` enum — no free-form category labels. Adding a
   new kind requires a code change here.
2. Summary length ≤ 200 chars; ASCII printable only; English-word
   shape (latin alphanumerics + common punctuation).
3. Prompt-injection blacklist — case-insensitive substring match
   against known patterns. Reject on any hit.
4. Schema'd evidence dict — keys limited to a known allowlist,
   values are scalar (str/int/float/bool); depth bounded to 1.

Loosening any guardrail requires editing this module — same
"deliberate principal-side change" stance as role_validator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# Recognized clarification kinds. Each one names a real situation
# where a desk's contract genuinely doesn't speak to a context the
# Auditor would otherwise misread.
KINDS = frozenset({
    # "I am about to do X for reason Y; the heavy lift / unusual metric
    # you're about to see is intentional."
    "workload_intent",
    # "My configuration just changed and I'm in a transient state until
    # the next stable tick."
    "config_change",
    # "I know this metric looks anomalous; here's the explanation."
    "metric_explanation",
    # "I encountered something my contract doesn't cover; flagging for
    # your awareness rather than as an action request."
    "contract_gap",
})

MAX_SUMMARY_CHARS = 200

# Evidence dict allowlist. Keys must be in this set; values must be
# scalars (str/int/float/bool); strings are length-bounded too.
EVIDENCE_KEYS = frozenset({
    "workload_id",
    "lease_id",
    "metric_name",
    "expected_value",
    "observed_value",
    "until",            # iso8601 timestamp string for transient states
    "next_tick_at",     # iso8601
    "ticket_url",       # the principal's tracking URL if any
    "rationale_hash",   # opaque pointer; up to caller
})
MAX_EVIDENCE_VALUE_CHARS = 200


# Prompt-injection patterns to refuse outright. Case-insensitive
# substring match. List grows as we observe new attack shapes —
# editing here is the deliberate-broadening path.
INJECTION_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "you are now",
    "system prompt",
    "forget instructions",
    "forget your instructions",
    "override",
    "disregard",
    "pretend you are",
    "act as",
    "from now on, you",
    "new instructions",
    "</system>",
    "<system>",
    "[system]",
    "you must",
    "do not refuse",
    "answer the following",
    "developer mode",
    "jailbreak",
)

# Allowed character class: ASCII printable + space, plus common
# whitespace. Reject anything else to prevent unicode lookalike
# attacks and zero-width injection.
_ALLOWED_SUMMARY_RE = re.compile(r"^[\x20-\x7E]+$")


@dataclass(frozen=True)
class Violation:
    code: str
    message: str


@dataclass(frozen=True)
class SanitizedClarification:
    """The sanitized payload that the Auditor sees. The kind enum +
    summary string + evidence dict are the only data points; nothing
    else from the wire reaches the LLM."""
    kind: str
    summary: str
    evidence: dict


@dataclass(frozen=True)
class SanitizationResult:
    ok: bool
    violations: tuple[Violation, ...]
    sanitized: SanitizedClarification | None = None

    @classmethod
    def passing(cls, c: SanitizedClarification) -> "SanitizationResult":
        return cls(ok=True, violations=(), sanitized=c)

    @classmethod
    def failing(cls, vs: list[Violation]) -> "SanitizationResult":
        return cls(ok=False, violations=tuple(vs))


def sanitize(
    *,
    kind: Any,
    summary: Any,
    evidence: Any = None,
) -> SanitizationResult:
    """Validate caller-supplied clarification fields. Returns either
    a SanitizedClarification ready to be persisted + shown to the
    Auditor, or a list of structured violations. Collect-all (not
    fail-fast) so callers can see every problem at once."""
    violations: list[Violation] = []

    # 1. kind
    if not isinstance(kind, str):
        violations.append(Violation(
            code="kind-not-string",
            message=f"kind must be a string; got {type(kind).__name__}",
        ))
        kind_norm = ""
    else:
        kind_norm = kind
        if kind not in KINDS:
            violations.append(Violation(
                code="kind-not-recognized",
                message=(f"kind {kind!r} not recognized; valid: "
                         f"{sorted(KINDS)}"),
            ))

    # 2. summary
    summary_norm = ""
    if not isinstance(summary, str):
        violations.append(Violation(
            code="summary-not-string",
            message=f"summary must be a string; got {type(summary).__name__}",
        ))
    else:
        summary_norm = summary.strip()
        if not summary_norm:
            violations.append(Violation(
                code="summary-empty",
                message="summary must not be empty after trim",
            ))
        elif len(summary_norm) > MAX_SUMMARY_CHARS:
            violations.append(Violation(
                code="summary-too-long",
                message=(f"summary {len(summary_norm)} chars exceeds "
                         f"max {MAX_SUMMARY_CHARS}"),
            ))
        elif not _ALLOWED_SUMMARY_RE.match(summary_norm):
            violations.append(Violation(
                code="summary-not-ascii-printable",
                message=("summary contains non-ASCII-printable characters; "
                         "use plain English text only"),
            ))
        else:
            # 3. injection blacklist (only if charset passed)
            lowered = summary_norm.lower()
            for pattern in INJECTION_PATTERNS:
                if pattern in lowered:
                    violations.append(Violation(
                        code="summary-injection-pattern",
                        message=(f"summary contains a prompt-injection "
                                 f"pattern ({pattern!r}); refused"),
                    ))
                    break

    # 4. evidence
    evidence_norm: dict = {}
    if evidence is not None:
        if not isinstance(evidence, dict):
            violations.append(Violation(
                code="evidence-not-dict",
                message=f"evidence must be a dict or null; got {type(evidence).__name__}",
            ))
        else:
            for k, v in evidence.items():
                if not isinstance(k, str):
                    violations.append(Violation(
                        code="evidence-key-not-string",
                        message=f"evidence key must be a string; got {k!r}",
                    ))
                    continue
                if k not in EVIDENCE_KEYS:
                    violations.append(Violation(
                        code="evidence-key-not-allowed",
                        message=(f"evidence key {k!r} not in allowlist; "
                                 f"valid: {sorted(EVIDENCE_KEYS)}"),
                    ))
                    continue
                if not isinstance(v, (str, int, float, bool)):
                    violations.append(Violation(
                        code="evidence-value-not-scalar",
                        message=(f"evidence[{k!r}] must be str/int/float/"
                                 f"bool; got {type(v).__name__}"),
                    ))
                    continue
                if isinstance(v, str):
                    if len(v) > MAX_EVIDENCE_VALUE_CHARS:
                        violations.append(Violation(
                            code="evidence-value-too-long",
                            message=(f"evidence[{k!r}] {len(v)} chars "
                                     f"exceeds max {MAX_EVIDENCE_VALUE_CHARS}"),
                        ))
                        continue
                    if not _ALLOWED_SUMMARY_RE.match(v):
                        violations.append(Violation(
                            code="evidence-value-not-ascii-printable",
                            message=(f"evidence[{k!r}] contains non-ASCII-"
                                     f"printable characters"),
                        ))
                        continue
                evidence_norm[k] = v

    if violations:
        return SanitizationResult.failing(violations)

    return SanitizationResult.passing(SanitizedClarification(
        kind=kind_norm,
        summary=summary_norm,
        evidence=evidence_norm,
    ))
