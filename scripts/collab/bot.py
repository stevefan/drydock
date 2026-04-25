#!/opt/telegram-bot-venv/bin/python3
"""Drydock collab Telegram bot.

Long-running daemon. Logs every chat message to a JSONL file the desk's
Claude can read. Slash commands toggle "collab mode" and trigger Claude
explicitly. Uses Claude Code's headless `claude -p` so calls bill to
the user's claude.ai subscription via delegated OAuth — no Anthropic
API key required.

State files (under /workspace/telegram, named-volume backed):
- inbox.jsonl    one event per line: {ts, chat_id, user, text, kind}
- mode           single line: "idle" or "collab"
- chat_id        single line: numeric chat id of the active conversation
                 (set on first message; bot only responds in this chat
                 to keep behavior predictable)

Slash commands:
- /status        post current mode + recent message count
- /collab        toggle on/off; with arg `on`/`off` set explicitly
- /ai <prompt>   one-shot: pass <prompt> + recent context to claude -p
- /wake          flip to collab + invoke claude with last 5 messages

Reference messages (anything not a slash command) are logged but not
auto-acted-upon. In `collab` mode, the bot replies to plain messages
the same way `/ai` does — agentic mode without per-message slash.

Token + chat allowlist:
- TELEGRAM_BOT_TOKEN — read from /run/secrets/telegram_bot_token
                      (drydock-managed file secret)
- TELEGRAM_ALLOWED_CHATS — optional, comma-separated chat ids; if set
                          the bot ignores messages from other chats.
                          If unset, the bot pins to the FIRST chat it
                          sees and refuses others (TOFU pinning).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters,
)

STATE_DIR = Path("/workspace/telegram")
STATE_DIR.mkdir(parents=True, exist_ok=True)
INBOX = STATE_DIR / "inbox.jsonl"
MODE_FILE = STATE_DIR / "mode"
CHAT_ID_FILE = STATE_DIR / "chat_id"

# How many recent messages to feed Claude as context for /ai and /wake.
CONTEXT_WINDOW = 20

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("collab-bot")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_event(event: dict) -> None:
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    with INBOX.open("a") as f:
        f.write(json.dumps(event) + "\n")


def read_recent(n: int = CONTEXT_WINDOW) -> list[dict]:
    if not INBOX.exists():
        return []
    with INBOX.open() as f:
        lines = f.readlines()[-n:]
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_mode() -> str:
    if not MODE_FILE.exists():
        return "idle"
    return MODE_FILE.read_text().strip() or "idle"


def write_mode(mode: str) -> None:
    MODE_FILE.write_text(mode + "\n")


def allowed_chats() -> set[int] | None:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHATS", "").strip()
    if not raw:
        return None
    out: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if piece:
            try:
                out.add(int(piece))
            except ValueError:
                logger.warning("ignoring non-integer chat id in allowlist: %r", piece)
    return out or None


def chat_pin_check(chat_id: int) -> bool:
    """TOFU-pin to the first chat we see if no env allowlist is set."""
    allow = allowed_chats()
    if allow is not None:
        return chat_id in allow
    if CHAT_ID_FILE.exists():
        return CHAT_ID_FILE.read_text().strip() == str(chat_id)
    CHAT_ID_FILE.write_text(str(chat_id) + "\n")
    logger.info("pinned to chat_id=%s on first contact", chat_id)
    return True


# ---------------------------------------------------------------------------
# Claude bridge via `claude -p`
# ---------------------------------------------------------------------------

CLAUDE_SYSTEM = (
    "You are a coding collaborator participating in a Telegram chat. "
    "Be concise and useful. The user prompt below is preceded by recent "
    "chat context for grounding. Respond as a peer, not a help-desk: "
    "short, on-point, and without preamble."
)


def call_claude(prompt: str, context: list[dict]) -> str:
    """One-shot Claude call. Returns its stdout (trimmed) or an error
    string suitable for posting back to Telegram."""
    ctx_lines = []
    for ev in context[-CONTEXT_WINDOW:]:
        who = ev.get("user", "?")
        text = ev.get("text", "")
        if text:
            ctx_lines.append(f"[{who}]: {text}")
    full_prompt = (
        "Recent chat context (most recent last):\n"
        + "\n".join(ctx_lines)
        + f"\n\nUser prompt: {prompt}"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", full_prompt, "--system-prompt", CLAUDE_SYSTEM],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"_(claude invocation failed: {exc})_"
    if result.returncode != 0:
        return f"_(claude exit {result.returncode}: {result.stderr.strip()[:200]})_"
    out = result.stdout.strip()
    return out or "_(claude returned empty)_"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def reject_or_log(update: Update) -> bool:
    """Common gate. Returns True if the chat is allowed."""
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if not chat_pin_check(chat_id):
        logger.warning("rejected chat_id=%s (not in allowlist)", chat_id)
        return False
    return True


async def handle_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await reject_or_log(update): return
    mode = read_mode()
    recent = read_recent(50)
    await update.message.reply_text(
        f"mode: *{mode}*\nrecent messages: {len(recent)}",
        parse_mode="Markdown",
    )


async def handle_collab(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await reject_or_log(update): return
    arg = (ctx.args[0].lower() if ctx.args else "").strip()
    if arg == "on":
        write_mode("collab")
    elif arg == "off":
        write_mode("idle")
    else:
        write_mode("idle" if read_mode() == "collab" else "collab")
    await update.message.reply_text(f"collab mode: *{read_mode()}*", parse_mode="Markdown")


async def handle_ai(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await reject_or_log(update): return
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text("usage: `/ai <prompt>`", parse_mode="Markdown")
        return
    await update.message.chat.send_action("typing")
    recent = read_recent()
    response = await asyncio.to_thread(call_claude, prompt, recent)
    await update.message.reply_text(response[:4000])


async def handle_wake(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await reject_or_log(update): return
    write_mode("collab")
    recent = read_recent(5)
    await update.message.chat.send_action("typing")
    response = await asyncio.to_thread(
        call_claude,
        "Read the last few messages and respond as if you just joined the conversation.",
        recent,
    )
    await update.message.reply_text(
        f"_(collab mode on)_\n\n{response[:3900]}",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await reject_or_log(update): return
    msg = update.message
    text = msg.text or ""
    user = (msg.from_user.username or msg.from_user.first_name or "?") if msg.from_user else "?"
    append_event({
        "ts": _now(),
        "chat_id": msg.chat.id,
        "user": user,
        "text": text,
        "kind": "text",
    })
    if read_mode() == "collab" and text.strip():
        await msg.chat.send_action("typing")
        recent = read_recent()
        response = await asyncio.to_thread(call_claude, text, recent)
        await msg.reply_text(response[:4000])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def load_token() -> str:
    secret = Path("/run/secrets/telegram_bot_token")
    if secret.exists():
        return secret.read_text().strip()
    env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env:
        return env
    raise SystemExit(
        "missing telegram bot token: expected /run/secrets/telegram_bot_token "
        "or env TELEGRAM_BOT_TOKEN"
    )


def main() -> None:
    token = load_token()
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("collab", handle_collab))
    app.add_handler(CommandHandler("ai", handle_ai))
    app.add_handler(CommandHandler("wake", handle_wake))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("collab bot starting; mode=%s, inbox=%s", read_mode(), INBOX)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
