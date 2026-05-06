"""Auditor watch loop — single-tick classification (Phase PA1).

One call to a cheap-class LLM (Haiku) per tick. Reads recent snapshot,
classifies the situation as routine / anomaly_suspected / unsure. Does
NOT take action; does NOT escalate to principal. The classification is
the input to the (later) deep-analysis tier.

This is the cheapest piece of the Auditor architecture: ~1KB context per
call, Haiku class model, output a tiny JSON verdict. At 5-min default
cadence (288 calls/day), spend is well under $1/day.

The watch tick:
1. Take a fresh snapshot (or use a passed-in one for testing)
2. Format snapshot as JSON for the LLM
3. Load watch.md prompt from the prompts dir
4. Call the LLM with snapshot+prompt
5. Parse the verdict; record outcome to ~/.drydock/auditor/watch_log.jsonl
6. Return WatchVerdict to caller
7. Touch the heartbeat file (deadman feeds on this signal)

The watch_log accumulates verdicts over time. The (later) deep-analysis
tier reads it for trend signals — "is this Drydock flagged repeatedly
even though I wasn't sure each time?"
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .heartbeat import touch as touch_heartbeat
from .llm import AnthropicHttpClient, LLMClient, LLMResponse, LLMUnavailableError
from .measurement import HarborSnapshot, snapshot_harbor
from .storage import write_snapshot

logger = logging.getLogger(__name__)

DEFAULT_WATCH_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 400  # plenty for the JSON verdict + reason

PROMPTS_DIR = Path(__file__).parent / "prompts"
WATCH_LOG_PATH = Path.home() / ".drydock" / "auditor" / "watch_log.jsonl"

VALID_VERDICTS = ("routine", "anomaly_suspected", "unsure")


@dataclass
class WatchVerdict:
    """One watch tick's outcome."""
    verdict: str  # routine | anomaly_suspected | unsure | error
    reason: str
    drydocks_of_concern: list[str] = field(default_factory=list)
    snapshot_at: str = ""
    tick_at: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw_llm_text: str = ""  # for debugging if parsing failed
    error: str | None = None  # set when verdict == "error"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "drydocks_of_concern": self.drydocks_of_concern,
            "snapshot_at": self.snapshot_at,
            "tick_at": self.tick_at,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "raw_llm_text": self.raw_llm_text if self.verdict == "error" else "",
            "error": self.error,
        }


def load_watch_prompt(prompts_dir: Path | None = None) -> str:
    """Load watch.md from the prompts dir. Cached only via filesystem."""
    d = prompts_dir or PROMPTS_DIR
    return (d / "watch.md").read_text(encoding="utf-8")


def parse_verdict(llm_text: str) -> tuple[str, str, list[str]]:
    """Parse the LLM's JSON output into (verdict, reason, drydocks_of_concern).

    Tolerates:
    - Markdown code fences around the JSON (```json ... ```)
    - Leading/trailing whitespace
    - Extra prose before/after JSON (extracts the first JSON object)

    Returns ('error', reason, []) if parsing fails; raw text retained.
    """
    text = llm_text.strip()
    # Strip code fences if present
    if text.startswith("```"):
        # Remove first line and last ``` line
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Find first { ... } block (LLM may include prose despite instructions)
    if "{" in text and "}" in text:
        start = text.index("{")
        end = text.rindex("}") + 1
        json_str = text[start:end]
    else:
        return ("error", f"no JSON object in response: {text[:100]}", [])

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as exc:
        return ("error", f"JSON decode failed: {exc}", [])

    if not isinstance(parsed, dict):
        return ("error", f"expected JSON object, got {type(parsed).__name__}", [])

    verdict = parsed.get("verdict", "")
    if verdict not in VALID_VERDICTS:
        return ("error", f"invalid verdict '{verdict}'; valid: {VALID_VERDICTS}", [])

    reason = str(parsed.get("reason", ""))
    docs_raw = parsed.get("drydocks_of_concern", [])
    if not isinstance(docs_raw, list):
        docs = []
    else:
        docs = [str(d) for d in docs_raw if d]

    return (verdict, reason, docs)


def format_snapshot_for_llm(snap: HarborSnapshot) -> str:
    """Render a snapshot as JSON the LLM can read.

    Prunes verbose/irrelevant fields to keep tokens down.
    """
    docs_compact = []
    for d in snap.drydocks:
        compact = {
            "name": d["name"],
            "state": d["state"],
            "yard": d.get("yard_id"),
            "metrics": d.get("metrics"),
            "leases": d.get("leases"),
            "audit_recent_1h": d.get("audit_recent_1h"),
            "yaml_drift": d.get("yaml_drift"),
        }
        docs_compact.append(compact)
    return json.dumps({
        "snapshot_at": snap.snapshot_at,
        "harbor": snap.harbor_hostname,
        "drydocks": docs_compact,
    }, indent=2)


def watch_once(
    *,
    registry,
    llm_client: LLMClient | None = None,
    model: str = DEFAULT_WATCH_MODEL,
    write_to_log: bool = True,
    write_snapshot_to_disk: bool = True,
    prompts_dir: Path | None = None,
    log_path: Path | None = None,
    update_heartbeat: bool = True,
) -> WatchVerdict:
    """Run one watch tick. Returns the verdict.

    Failure modes:
    - LLM unavailable (no API key, network down): verdict='error',
      heartbeat NOT updated (deadman will eventually fire).
    - LLM call timeout / HTTP error: same as above.
    - LLM returned malformed JSON: verdict='error', heartbeat IS updated
      (the LLM was reachable, just confused — that's not a deadman case).

    Always writes the verdict to watch_log.jsonl (if write_to_log) so
    repeated errors are observable in the log even if alerting is silent.
    """
    snap = snapshot_harbor(registry)
    if write_snapshot_to_disk:
        try:
            write_snapshot(snap)
        except OSError as exc:
            logger.warning("watch_once: failed to persist snapshot: %s", exc)

    tick_at = datetime.now(timezone.utc).isoformat()
    verdict_obj = WatchVerdict(
        verdict="routine", reason="", snapshot_at=snap.snapshot_at,
        tick_at=tick_at, model=model,
    )

    client = llm_client or AnthropicHttpClient()
    system_prompt = load_watch_prompt(prompts_dir)
    user_content = format_snapshot_for_llm(snap)

    try:
        response = client.call(
            model=model, system=system_prompt, user=user_content,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
    except LLMUnavailableError as exc:
        # Don't update heartbeat — deadman will (eventually) fire because
        # the watch loop is effectively non-functional.
        verdict_obj.verdict = "error"
        verdict_obj.error = f"llm_unavailable: {exc}"
        verdict_obj.reason = "LLM unreachable; watch tick skipped"
        if write_to_log:
            _append_log(verdict_obj, log_path)
        return verdict_obj

    verdict_obj.input_tokens = response.input_tokens
    verdict_obj.output_tokens = response.output_tokens
    verdict_obj.model = response.model or model
    verdict_obj.raw_llm_text = response.text

    parsed_verdict, reason, docs = parse_verdict(response.text)
    verdict_obj.verdict = parsed_verdict
    verdict_obj.reason = reason
    verdict_obj.drydocks_of_concern = docs

    # Heartbeat: update whenever the LLM was reachable (even if confused).
    # The watch loop is alive; the deadman shouldn't fire.
    if update_heartbeat:
        try:
            touch_heartbeat()
        except OSError as exc:
            logger.warning("watch_once: failed to touch heartbeat: %s", exc)

    if write_to_log:
        _append_log(verdict_obj, log_path)

    return verdict_obj


def _append_log(verdict: WatchVerdict, log_path: Path | None = None) -> None:
    """Append one verdict to the watch log (JSONL)."""
    p = log_path or WATCH_LOG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(verdict.to_dict()) + "\n")
    except OSError as exc:
        logger.warning("_append_log: failed to write to %s: %s", p, exc)


def read_watch_log(
    *,
    limit: int | None = None,
    log_path: Path | None = None,
) -> list[dict]:
    """Read recent watch verdicts. Newest last (chronological)."""
    p = log_path or WATCH_LOG_PATH
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None and limit > 0:
        out = out[-limit:]
    return out
