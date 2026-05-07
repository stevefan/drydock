"""Auditor deep analysis (Phase PA2).

Event-triggered Sonnet-class call invoked by the daemon when watch_once
returns 'anomaly_suspected' or 'unsure'. More context, more capable
model, structured action recommendation + telegram-ready escalation
message.

Output is RECOMMENDATIONAL only in PA2. PA3 wires Bucket-2 action
authority where the Auditor can call Authority's enforcement RPCs.
For PA2, recommendations are logged + (optionally) sent to Telegram
for principal action.

Cost characteristics:
- Event-triggered (not periodic) — runs only when watch flags
- Sonnet 4.6 class model
- Context size: ~5-15KB (snapshot + watch verdict + audit + prior verdicts)
- Output: ~200-500 tokens
- Probably <10 calls/day on a calm Harbor; up to ~50/day on busy fleet
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from drydock.core.audit import DEFAULT_LOG_PATH as AUDIT_LOG_PATH

from .llm import AnthropicHttpClient, LLMClient, LLMUnavailableError
from .measurement import HarborSnapshot, count_recent_audit_events
from .watch import WatchVerdict, read_watch_log

logger = logging.getLogger(__name__)

DEFAULT_DEEP_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1000  # ~3-4 paragraphs of structured output

PROMPTS_DIR = Path(__file__).parent / "prompts"
DEEP_LOG_PATH = Path.home() / ".drydock" / "auditor" / "deep_log.jsonl"
TELEGRAM_PROXY_PATH = Path.home() / ".drydock" / "auditor" / "last_telegram_send"

VALID_VERDICTS = ("action_recommended", "escalate_only", "informational", "false_alarm")
VALID_ACTIONS = ("throttle_egress", "stop_dock", "revoke_lease", "freeze_storage", None)


@dataclass
class DeepAnalysis:
    """Structured output of one deep-analysis call."""
    verdict: str  # action_recommended | escalate_only | informational | false_alarm | error
    confidence: str = ""  # high | medium | low
    reasoning: str = ""
    recommended_action: str | None = None
    target_drydock: str | None = None
    target_lease_id: str | None = None
    should_send_telegram: bool = False
    escalation_message: str = ""
    triggered_by_verdict: dict | None = None
    snapshot_at: str = ""
    analyzed_at: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    raw_llm_text: str = ""
    telegram_sent: bool = False  # set after attempting to send

    def to_dict(self) -> dict:
        d = {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "recommended_action": self.recommended_action,
            "target_drydock": self.target_drydock,
            "target_lease_id": self.target_lease_id,
            "should_send_telegram": self.should_send_telegram,
            "escalation_message": self.escalation_message,
            "triggered_by_verdict": self.triggered_by_verdict,
            "snapshot_at": self.snapshot_at,
            "analyzed_at": self.analyzed_at,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
            "telegram_sent": self.telegram_sent,
        }
        if self.verdict == "error":
            d["raw_llm_text"] = self.raw_llm_text
        return d


def load_deep_prompt(prompts_dir: Path | None = None) -> str:
    """Load deep_analysis.md from prompts dir."""
    d = prompts_dir or PROMPTS_DIR
    return (d / "deep_analysis.md").read_text(encoding="utf-8")


def build_user_context(
    *,
    watch_verdict: WatchVerdict,
    snapshot: HarborSnapshot,
    audit_path: Path | None = None,
    prior_verdicts_limit: int = 10,
) -> str:
    """Build the user-message context for the deep analysis call.

    Includes (per deep_analysis.md prompt):
    - The watch verdict that triggered this analysis
    - Current snapshot
    - Recent audit events for the flagged Drydocks
    - Prior watch verdicts (for trend signal)
    """
    sections = []

    sections.append("WATCH VERDICT (the flag that brought us here):")
    sections.append(json.dumps(watch_verdict.to_dict(), indent=2))
    sections.append("")

    sections.append("SNAPSHOT (current Harbor state):")
    sections.append(json.dumps({
        "snapshot_at": snapshot.snapshot_at,
        "harbor": snapshot.harbor_hostname,
        "drydocks": snapshot.drydocks,
    }, indent=2))
    sections.append("")

    sections.append(
        f"RECENT AUDIT for flagged Drydocks "
        f"(targets: {watch_verdict.drydocks_of_concern or 'all'}):"
    )
    audit_lines = []
    for d in (watch_verdict.drydocks_of_concern or []):
        # Resolve dock name → drydock_id
        dock_id = None
        for d_data in snapshot.drydocks:
            if d_data["name"] == d:
                dock_id = d_data["id"]
                break
        if dock_id is None:
            audit_lines.append(f"(no dock found for name={d!r})")
            continue
        audit = count_recent_audit_events(dock_id, audit_path=audit_path)
        if audit is None:
            audit_lines.append(f"{d}: audit log unreadable")
        elif audit["events_total"] == 0:
            audit_lines.append(f"{d}: 0 events in last hour")
        else:
            audit_lines.append(f"{d}: {audit['events_total']} events: {audit['by_event_class']}")
    if not audit_lines:
        audit_lines.append("(no specific drydocks flagged; see snapshot for breadth)")
    sections.extend(audit_lines)
    sections.append("")

    sections.append(f"RECENT WATCH VERDICTS (last {prior_verdicts_limit} ticks for trend):")
    prior = read_watch_log(limit=prior_verdicts_limit)
    if not prior:
        sections.append("(no prior verdicts)")
    else:
        for v in prior:
            tick_at = v.get("tick_at", "?")[:19]
            ver = v.get("verdict", "?")
            reason = v.get("reason", "")[:80]
            sections.append(f"  {tick_at}  {ver:<22} {reason}")
    sections.append("")

    return "\n".join(sections)


def parse_deep_verdict(llm_text: str) -> DeepAnalysis:
    """Parse LLM JSON output into a DeepAnalysis. Returns DeepAnalysis with
    verdict='error' if parsing fails (never raises)."""
    text = llm_text.strip()

    # Strip code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Find first { ... } block
    if "{" in text and "}" in text:
        start = text.index("{")
        end = text.rindex("}") + 1
        json_str = text[start:end]
    else:
        return DeepAnalysis(
            verdict="error",
            reasoning=f"No JSON object in response: {text[:200]}",
            raw_llm_text=text,
        )

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as exc:
        return DeepAnalysis(
            verdict="error",
            reasoning=f"JSON decode failed: {exc}",
            raw_llm_text=text,
        )

    if not isinstance(parsed, dict):
        return DeepAnalysis(
            verdict="error",
            reasoning=f"Expected JSON object, got {type(parsed).__name__}",
            raw_llm_text=text,
        )

    verdict = parsed.get("verdict", "")
    if verdict not in VALID_VERDICTS:
        return DeepAnalysis(
            verdict="error",
            reasoning=f"Invalid verdict {verdict!r}; valid: {VALID_VERDICTS}",
            raw_llm_text=text,
        )

    action = parsed.get("recommended_action")
    if action not in VALID_ACTIONS:
        # Allow LLM to use slightly different wording — best-effort match
        if isinstance(action, str):
            action_lower = action.lower().replace(" ", "_")
            for valid in VALID_ACTIONS:
                if valid and valid in action_lower:
                    action = valid
                    break
            else:
                action = None

    return DeepAnalysis(
        verdict=verdict,
        confidence=str(parsed.get("confidence", "")),
        reasoning=str(parsed.get("reasoning", "")),
        recommended_action=action,
        target_drydock=parsed.get("target_drydock"),
        target_lease_id=parsed.get("target_lease_id"),
        should_send_telegram=bool(parsed.get("should_send_telegram", False)),
        escalation_message=str(parsed.get("escalation_message", "")),
    )


def deep_analyze(
    *,
    watch_verdict: WatchVerdict,
    snapshot: HarborSnapshot,
    llm_client: LLMClient | None = None,
    model: str = DEFAULT_DEEP_MODEL,
    write_to_log: bool = True,
    send_telegram: bool = True,
    prompts_dir: Path | None = None,
    audit_path: Path | None = None,
    log_path: Path | None = None,
    telegram_send_fn=None,
) -> DeepAnalysis:
    """Run deep analysis on a flagged watch verdict.

    Returns DeepAnalysis with the structured recommendation. If
    `should_send_telegram` is True AND `send_telegram` parameter is True
    AND telegram is configured, attempts to send the escalation_message;
    sets `telegram_sent` accordingly.

    Failure modes:
    - LLM unavailable: returns verdict='error' with error field set
    - LLM returns malformed JSON: verdict='error', raw text preserved
    - Telegram send failure: verdict is preserved; telegram_sent=False
      (logged; deep result still useful)
    """
    analyzed_at = datetime.now(timezone.utc).isoformat()
    result = DeepAnalysis(
        verdict="error",
        triggered_by_verdict=watch_verdict.to_dict(),
        snapshot_at=snapshot.snapshot_at,
        analyzed_at=analyzed_at,
        model=model,
    )

    client = llm_client or AnthropicHttpClient()
    system_prompt = load_deep_prompt(prompts_dir)
    user_content = build_user_context(
        watch_verdict=watch_verdict,
        snapshot=snapshot,
        audit_path=audit_path,
    )

    try:
        response = client.call(
            model=model, system=system_prompt, user=user_content,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
    except LLMUnavailableError as exc:
        result.error = f"llm_unavailable: {exc}"
        result.reasoning = "LLM unreachable; deep analysis skipped"
        if write_to_log:
            _append_log(result, log_path)
        return result

    parsed = parse_deep_verdict(response.text)
    parsed.triggered_by_verdict = watch_verdict.to_dict()
    parsed.snapshot_at = snapshot.snapshot_at
    parsed.analyzed_at = analyzed_at
    parsed.model = response.model or model
    parsed.input_tokens = response.input_tokens
    parsed.output_tokens = response.output_tokens
    if parsed.verdict == "error":
        parsed.raw_llm_text = response.text

    # Send Telegram if recommended + enabled
    if (parsed.should_send_telegram and parsed.escalation_message
            and send_telegram and parsed.verdict != "error"):
        send_fn = telegram_send_fn
        if send_fn is None:
            from drydock.core.telegram import is_configured, send
            if is_configured():
                send_fn = send
        if send_fn is not None:
            try:
                sent = send_fn(parsed.escalation_message)
                parsed.telegram_sent = bool(sent)
                if sent:
                    # Touch the proxy file so scheduler treats this as
                    # an open Telegram thread (responsive cadence).
                    try:
                        TELEGRAM_PROXY_PATH.parent.mkdir(parents=True, exist_ok=True)
                        TELEGRAM_PROXY_PATH.touch()
                    except OSError as exc:
                        logger.warning("deep: telegram proxy touch failed: %s", exc)
            except Exception as exc:
                logger.warning("deep: telegram send raised: %s", exc)
                parsed.telegram_sent = False

    # Phase PA3: when the deep analysis recommended a Bucket-2 action,
    # invoke it via the daemon's AuditorAction RPC. Defaults to dry-run
    # mode (the daemon checks AUDITOR_LIVE_ACTIONS at call time); flip
    # the env var to enable live execution.
    if (parsed.verdict == "action_recommended" and parsed.recommended_action
            and (parsed.target_drydock or parsed.target_lease_id)):
        try:
            _invoke_auditor_action(parsed)
        except Exception as exc:
            # Action failure must not break the deep-analysis path —
            # the recommendation is logged regardless. The audit-event
            # emitted by the daemon (or its absence) tells the principal
            # whether the action ran.
            logger.warning("deep: auditor action invocation failed: %s", exc)

    if write_to_log:
        _append_log(parsed, log_path)

    return parsed


def _invoke_auditor_action(analysis: DeepAnalysis) -> None:
    """Best-effort: call the daemon's AuditorAction RPC for the
    recommended action.

    Uses the in-container drydock-rpc shape (Unix socket + bearer
    token at /run/secrets/auditor-token). The token must be
    auditor-scoped — set via ``drydock auditor designate <name>``.

    For now this is sketched against the local daemon socket; in a
    container deployment, the daemon socket is bind-mounted at
    /run/drydock/daemon.sock per the existing in-desk-rpc design.
    """
    import json as _json
    import os as _os
    import socket as _socket

    sock_path = _os.environ.get(
        "DRYDOCK_DAEMON_SOCKET",
        "/run/drydock/daemon.sock",
    )
    if not _os.path.exists(sock_path):
        sock_path = _os.path.expanduser("~/.drydock/run/daemon.sock")

    # Token: prefer auditor-token if present (post-designate), fall
    # back to drydock-token. The daemon enforces scope; if the token
    # isn't auditor-scoped, the call returns -32020.
    token: str | None = None
    for candidate in (
        "/run/secrets/auditor-token",
        "/run/secrets/drydock-token",
    ):
        try:
            token = Path(candidate).read_text(encoding="utf-8").strip()
            break
        except (FileNotFoundError, PermissionError):
            continue
    if not token:
        logger.info("deep: no bearer token available; skipping auditor action invocation")
        return

    params: dict = {
        "kind": analysis.recommended_action,
        "reason": analysis.reasoning or "deep-analysis recommendation",
        "evidence": {
            "watch_verdict": analysis.triggered_by_verdict,
            "model": analysis.model,
            "snapshot_at": analysis.snapshot_at,
        },
    }
    # revoke_lease takes lease_id; other actions take target_drydock_id.
    if analysis.recommended_action == "revoke_lease" and analysis.target_lease_id:
        params["lease_id"] = analysis.target_lease_id
    elif analysis.target_drydock:
        params["target_drydock_id"] = analysis.target_drydock
    else:
        logger.info("deep: skipping auditor action — neither target nor lease_id set")
        return
    request = _json.dumps({
        "jsonrpc": "2.0",
        "method": "AuditorAction",
        "params": params,
        "id": f"auditor-{analysis.analyzed_at}",
        "auth": token,
    }).encode("utf-8")

    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.connect(sock_path)
        s.sendall(request)
        s.shutdown(_socket.SHUT_WR)
        chunks = []
        while True:
            buf = s.recv(4096)
            if not buf:
                break
            chunks.append(buf)
        s.close()
    except OSError as exc:
        logger.warning("deep: AuditorAction socket call failed: %s", exc)
        return

    response_text = b"".join(chunks).decode("utf-8")
    try:
        response = _json.loads(response_text)
    except _json.JSONDecodeError:
        logger.warning("deep: AuditorAction response not JSON: %r", response_text[:200])
        return
    if "error" in response:
        logger.info(
            "deep: AuditorAction returned error code=%s message=%s",
            response["error"].get("code"), response["error"].get("message"),
        )
    else:
        result = response.get("result", {})
        logger.info(
            "deep: AuditorAction kind=%s mode=%s executed=%s",
            result.get("kind"),
            result.get("execution_mode"),
            result.get("executed"),
        )


def _append_log(analysis: DeepAnalysis, log_path: Path | None = None) -> None:
    """Append a deep-analysis result to the JSONL log."""
    p = log_path or DEEP_LOG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(analysis.to_dict()) + "\n")
    except OSError as exc:
        logger.warning("deep._append_log: failed to write to %s: %s", p, exc)


def read_deep_log(
    *,
    limit: int | None = None,
    log_path: Path | None = None,
) -> list[dict]:
    """Read recent deep-analysis results. Newest last."""
    p = log_path or DEEP_LOG_PATH
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
