# Collab desk — shared coding + Telegram side-channel

A long-running drydock for greenfield collab projects. New collaborators
get tailnet SSH access; chat happens in Telegram with a bot that can
hand specific messages to Claude (`claude -p`) on demand.

## What's in the desk

- **Long-running container**, on the Hetzner Harbor (always-on fits
  inbound Telegram).
- **Empty greenfield repo** at `/workspace` (= `/root/src/collab`).
  Ready to push to GitHub the moment the project has a name.
- **Telegram bot** (`/usr/local/bin/bot.py`), launched by
  `start-telegram-bot.sh` from `postStartCommand`.
- **Helper CLIs** for an in-desk Claude session:
  `tg-recent`, `tg-send`, `tg-mode`.

## Slash commands

| command | effect |
|---|---|
| `/status` | post current mode + recent message count |
| `/collab on` / `/collab off` / `/collab` | set or toggle "AI collab mode" |
| `/ai <prompt>` | one-shot: pass `<prompt>` + last 20 messages of context to `claude -p`, post response |
| `/wake` | flip mode to collab + invoke Claude on the last 5 messages, post response |

While in `collab` mode, every plain text message also gets passed to
`claude -p`. In `idle` mode (default), plain messages are logged to
`/workspace/telegram/inbox.jsonl` for reference but not auto-acted on.

## Trust model

- **Tailnet-only**. No public internet exposure on this desk; firewall
  egress allowlist covers Telegram + Anthropic + GitHub + pip.
- **TOFU chat pinning**. The bot pins to the first Telegram chat that
  messages it; later messages from other chats are rejected. To use a
  specific allowlist, set `TELEGRAM_ALLOWED_CHATS` (comma-separated
  chat IDs).
- **Tailscale SSH ACL** governs who can `tailscale ssh node@collab`.
  Tag the desk `tag:collab` and allow your collaborators' tailnet
  identities to SSH it. This is your one-time admin step in the
  Tailscale admin panel — drydock does not automate ACL changes.

## Userland setup

```bash
# 1. Create a Telegram bot, capture the token.
#    Open Telegram → @BotFather → /newbot → answer prompts.
#    BotFather replies with: "Use this token to access the HTTP API: 12345:ABC..."

# 2. Push the token into the desk as a drydock secret.
ssh root@<harbor> 'ws secret set collab telegram_bot_token' <<<"<paste token>"

# 3. Restart so the secret mounts.
ssh root@<harbor> 'ws stop collab && ws create collab'

# 4. Add the bot to a Telegram chat (DM with the bot, or a group).
#    Send any message — the bot pins to that chat.

# 5. Verify.
#    Telegram: /status
#    expected: "mode: idle, recent messages: 1"
```

After this, anyone can `/ai <prompt>` from inside the chat and Claude
will respond. To bring a collaborator in, add them to the Tailscale ACL
allowlist for `tag:collab` SSH access — they then `tailscale ssh
node@collab` and work in the same desk.

## How an in-desk Claude session uses this

A Claude Code session running inside the desk (e.g. attached via
`tailscale ssh node@collab && claude`) can read or post:

```bash
tg-recent --since 1h        # last hour's messages, brief format
tg-recent --since 24h --json # full JSON for programmatic parsing
tg-mode                     # current mode
tg-mode collab              # set mode (so plain messages auto-route to Claude)
tg-send "build complete; PR opened against main"
```

Reading is the reference pattern; sending is the proactive one. The
bot itself is the turn-by-turn handler; the in-desk Claude can be the
asynchronous reasoner.

## Operational notes

- **Bot state survives `ws stop && ws create`** via named volume
  `collab-telegram-state`. To wipe (drop chat pin, clear inbox):
  `docker volume rm collab-telegram-state` then recreate.
- **Deskwatch** probes the bot's pidfile every 5min — surfaces if the
  Python process dies. Inbox freshness is intentionally *not* a
  health signal: a quiet bot is fine.
- **Token rotation**: revoke at @BotFather, get a new one,
  `ws secret set collab telegram_bot_token` again, recreate the desk.
- **Cost**: `/ai` and `/wake` and collab-mode-replies use `claude -p`
  with the desk's delegated `claude_credentials` (your claude.ai
  subscription). No Anthropic API key needed; budget == subscription.

## V1 scope guardrails

What this desk **doesn't** do (intentionally — V2+ if needed):
- Auto-create GitHub repo / push initial scaffolding
- Handle Telegram media (photos, files, voice)
- Multi-chat support beyond TOFU pinning
- Per-collaborator memory (Claude sees chat-wide context, not
  threaded-per-user)
- Transcript persistence beyond the inbox JSONL
- Rate limiting on `/ai` calls (you'll notice in subscription usage)
