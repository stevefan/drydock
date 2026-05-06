"""Host-side Telegram bot helper for Authority + (future) Auditor + ad-hoc.

Stdlib-only. Mirrors the in-Drydock ``tg.py`` helper from collab Drydock's
/workspace/telegram/tg.py (per AGENT_NOTES.md), but reads credentials from
Harbor-level daemon-secrets instead of in-container /run/secrets/.

Conventions:
- Bot token at ``~/.drydock/daemon-secrets/telegram_bot_token`` (mode 0400)
- Default chat_id at ``~/.drydock/daemon-secrets/telegram_chat_id`` (mode 0400)
- Both are principal-provisioned — `ws telegram setup` will install them; for
  now, principal places the files manually.

The send() function returns True on successful send, False on any failure.
NEVER raises into caller — telegram outage shouldn't crash the deadman
switch or the Auditor's escalation path. Callers can check the return
value if they want to know whether their message landed.

Design decision (2026-05-05): host-side telegram is necessary because
Authority + deadman switch run in `wsd` on the Harbor host, OUTSIDE any
Drydock container. The in-Drydock tg.py and shipped tg-send only work
INSIDE drydock-base containers; for host-side use, we need this module.
When the auth-broker work eventually moves principal-OAuth-credentials
to a daemon-managed location, the same pattern applies for OAuth-vs-API
credential lookup; this module's secret-discovery is the reference.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DAEMON_SECRETS_DIR = Path.home() / ".drydock" / "daemon-secrets"
DEFAULT_TOKEN_PATH = DAEMON_SECRETS_DIR / "telegram_bot_token"
DEFAULT_CHAT_ID_PATH = DAEMON_SECRETS_DIR / "telegram_chat_id"
API_TIMEOUT = 15


def _read_secret(path: Path) -> str | None:
    """Read a secret file; return stripped contents or None if absent/unreadable."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, PermissionError, OSError):
        return None


def is_configured(
    *,
    token_path: Path = DEFAULT_TOKEN_PATH,
    chat_id_path: Path = DEFAULT_CHAT_ID_PATH,
) -> bool:
    """True iff both bot token AND default chat_id are readable on this Harbor.

    Use this to check whether telegram-based alerting is available before
    falling back to audit-only / log-only signaling.
    """
    return _read_secret(token_path) is not None and _read_secret(chat_id_path) is not None


def send(
    text: str,
    *,
    chat_id: int | str | None = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = True,
    token_path: Path = DEFAULT_TOKEN_PATH,
    chat_id_path: Path = DEFAULT_CHAT_ID_PATH,
) -> bool:
    """Send a message via Telegram bot API. Returns True on success.

    Never raises. Returns False if bot token missing, chat_id missing, or
    the API call fails for any reason. Logs warnings on failure so failed
    sends are diagnosable from `wsd.log` / journald.

    `disable_web_page_preview` defaults True because tailnet URLs aren't
    reachable from Telegram's previewer (per the AGENT_NOTES.md learning),
    and ugly empty-card previews are noise in alert messages.
    """
    token = _read_secret(token_path)
    if token is None:
        logger.warning(
            "telegram.send: bot token not found at %s; message not sent",
            token_path,
        )
        return False

    if chat_id is None:
        chat_id = _read_secret(chat_id_path)
        if chat_id is None:
            logger.warning(
                "telegram.send: no chat_id given and %s not present; message not sent",
                chat_id_path,
            )
            return False

    payload: dict = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            if parsed.get("ok"):
                return True
            logger.warning(
                "telegram.send: API returned not-ok: %s",
                parsed.get("description", body[:200]),
            )
            return False
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "telegram.send: HTTP %s — %s",
                exc.code, err_body[:200],
            )
        except Exception:
            logger.warning("telegram.send: HTTP %s (no body readable)", exc.code)
        return False
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("telegram.send: %s: %s", type(exc).__name__, exc)
        return False


def send_with_fallback(text: str, **kwargs) -> tuple[bool, str]:
    """Try Telegram; on failure, fall back to logger.error + return.

    Useful for critical alerts where you want SOMETHING to record the event
    even if Telegram is down. Returns (sent_via_telegram, channel_used).
    """
    if send(text, **kwargs):
        return (True, "telegram")
    logger.error("ALERT (telegram unavailable): %s", text)
    return (False, "log_fallback")
