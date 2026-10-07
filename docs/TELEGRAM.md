# Telegram integration

The bot has two independent Telegram features, both using the same
bot token:

1. **Notifications** (one-way): startup, buy, and error alerts pushed to your
   chat. Implemented in `bot/notifier.py`.
2. **Command bot** (interactive): answer commands like `/status` or `/buy`
   directly in chat. Implemented in `bot/telegram_commands.py`.

Both are optional. Everything below is written for the public repository and
contains no instance-specific values.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather):
   `/newbot`, pick a name and username, copy the **token**.
2. Get your chat ID: message [@userinfobot](https://t.me/userinfobot) or any
   bot and read the `chat.id` from a `getUpdates` call.
3. Enter both values in the dashboard under **Settings → Telegram
   Notifications** (stored in `data/secrets.json`, mode 0600, excluded from
   backups) — or set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` as environment
   variables (env vars take precedence).
4. Restart the bot. On startup the log shows either
   `Telegram command bot enabled (allowed chats: N).` or
   `Telegram command bot disabled (no bot token / allowed chat ID, or explicitly off).`

## Command reference

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/status` | Portfolio size, P/L, planned buys, next cycle, last error |
| `/price` | Current price vs. dynamic-DCA reference, matched tier |
| `/budget` | Monthly budget usage and remaining amount |
| `/last [n]` | Last n transactions (default 3, max 10), incl. dynamic tier |
| `/alerts` | Open (unacknowledged) alerts |
| `/buy` | Request a manual buy — asks for inline confirmation; warns and offers a one-time over-budget approval when the monthly budget is spent |
| `/pause` | Pause the bot — asks for inline confirmation |
| `/resume` | Resume the bot — asks for inline confirmation |

`/buy` does not place an order by itself: it sends the same manual-cycle
request as the dashboard's **Buy Now** button, so every guard (budget,
max price, live-trading switch) still applies and the order is executed by
the normal trading loop. If the monthly budget is used up, the confirmation
says so explicitly — confirming it approves exceeding the budget **once**.
A buy that is skipped for any reason (budget, max price, …) is never
silent: it creates an alert in the dashboard and, for manual requests, a
Telegram notification with the reason.

## Security model

Deliberately strict, because a chat command can move money:

- **Allow-list.** Only chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS`
  (comma-separated) are served. If unset, the allow-list defaults to the
  notification chat ID, so command access is never broader than who already
  receives all notifications. Unknown chats are **silently ignored** — the
  bot does not even confirm its existence.
- **One-time confirmations.** `/buy`, `/pause`, and `/resume` reply with
  inline ✅ / ❌ buttons backed by a random token that is single-use and
  expires after 60 seconds. Confirmations cannot be replayed or reused
  across chats.
- **Rate limiting.** Per-chat: minimum 2 s between commands and at most
  20 commands per minute.
- **No inbound ports.** The command bot uses long polling — outbound HTTPS
  to `api.telegram.org` only. Nothing needs to be exposed to the internet.
- **Secrets are never handled.** No command accepts, prints, or echoes the
  bot token, API keys, or chat IDs in full.
- **Audit log.** Every command and every executed action is appended to
  `data/audit_log.json` with source `telegram` and a masked chat identity
  (`telegram:...1234`).
- **Fail-safe.** An invalid or revoked token (HTTP 401) stops the poller
  permanently instead of retrying forever; transient poll errors back off
  exponentially up to 60 s.
- **Kill switch.** Set `TELEGRAM_COMMANDS_ENABLED=false` to disable the
  command bot while keeping notifications running.

## Multiple operators

To let additional chat IDs control the bot, set:

```env
TELEGRAM_ALLOWED_CHAT_IDS=111111,222222,-333333
```

Group chats have negative IDs. Each operator is rate-limited independently
and all of them appear in the audit log.

## Running several instances

Do **not** reuse the same bot token on two running instances (e.g. staging
and production): both would race on `getUpdates` and commands would be
answered by a random one. Give each instance its own bot via @BotFather.

## Troubleshooting

| Symptom | Check |
|---|---|
| Bot does not answer | Startup log line shown? Token/chat ID configured (Settings or env)? |
| "That confirmation expired" | Confirm within 60 s; re-run the command |
| Bot answers but actions don't happen | Live-trading switch, monthly budget, max-price guard |
| Stopped after "Telegram command bot stopping" | Token invalid/revoked — create a new one with @BotFather |
| Notifications work, commands don't | `TELEGRAM_COMMANDS_ENABLED` accidentally `false`, or chat ID not in allow-list |
