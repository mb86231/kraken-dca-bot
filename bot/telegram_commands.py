"""Interactive Telegram command bot (read-and-control).

Long-polls the Telegram Bot API for commands and lets the allowed operator(s)
query status and trigger the same actions as the web dashboard (pause, resume,
manual buy). One-way notifications live in ``bot.notifier``; this module adds
the inbound side without exposing any port — all traffic is outbound HTTPS to
api.telegram.org.

Security model (deliberately strict):
- Only chat IDs in the allow-list are served; everyone else is silently
  ignored (no reply, so the bot's existence is not confirmed).
- Destructive actions (buy, pause, resume) require a one-time inline
  confirmation token, single-use and expiring after 60 seconds.
- Per-chat rate limiting guards against spam/flood.
- Secrets are never echoed; no command accepts or prints tokens/keys.
- Every executed action is appended to the shared audit log.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from bot.secrets_store import SecretsStore
from bot.utils import atomic_write_json, redact_sensitive, safe_load_json, utc_now

if TYPE_CHECKING:
    from bot.core import KrakenDCA

AUDIT_LOG_PATH = Path("data/audit_log.json")
ALERTS_PATH = Path("data/alerts.json")

POLL_TIMEOUT_SECONDS = 25
CONFIRM_TTL_SECONDS = 60
MIN_COMMAND_INTERVAL_SECONDS = 2.0
MAX_COMMANDS_PER_MINUTE = 20
MAX_BACKOFF_SECONDS = 60.0

HELP_TEXT = (
    "🤖 DCA-Bot commands\n"
    "/status — portfolio, P/L, next cycle\n"
    "/price — price vs reference, active tier\n"
    "/budget — monthly budget usage\n"
    "/last [n] — recent transactions (max 10)\n"
    "/alerts — open alerts\n"
    "/buy — manual buy (asks to confirm)\n"
    "/pause — pause the bot (asks to confirm)\n"
    "/resume — resume the bot (asks to confirm)\n"
    "/help — this list"
)


class TelegramApiError(Exception):
    """Telegram API failure with secrets redacted."""


class TelegramApiClient(Protocol):
    """Structural type for the transport the command bot drives.

    ``TelegramApi`` is the production implementation; tests inject a fake with
    the same methods. Declared as a Protocol so mypy accepts both.
    """

    def get_updates(self, offset: int, timeout: int = POLL_TIMEOUT_SECONDS) -> list[dict[str, Any]]: ...

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None: ...

    def answer_callback(self, callback_id: str) -> None: ...


class TelegramApi:
    """Minimal Telegram Bot API client (stdlib only, like the notifier)."""

    BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str, opener: Callable[..., Any] | None = None):
        self._token = bot_token
        # ``opener`` is injectable in tests; production uses urllib.
        self._opener = opener or urllib.request.urlopen

    def _call(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        url = f"{self.BASE}/bot{self._token}/{method}"
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with self._opener(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except Exception as e:
            raise TelegramApiError(redact_sensitive(str(e))) from e
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise TelegramApiError(f"invalid Telegram response: {e}") from e
        if not payload.get("ok"):
            description = redact_sensitive(str(payload.get("description", "unknown error")))
            raise TelegramApiError(f"Telegram API error: {description} ({payload.get('error_code')})")
        return payload

    def get_updates(self, offset: int, timeout: int = POLL_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
        payload = self._call("getUpdates", {"offset": offset, "timeout": timeout}, timeout=timeout + 10)
        result = payload.get("result")
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        return self._call("sendMessage", params)

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})


def _load_allowed_chat_ids() -> list[str]:
    """Chat IDs allowed to issue commands.

    ``TELEGRAM_ALLOWED_CHAT_IDS`` (comma separated) wins; otherwise the
    notifier's configured chat ID is the allow-list, so command access is
    never broader than who already receives all notifications.
    """
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if raw:
        return [c.strip() for c in raw.split(",") if c.strip().lstrip("-").isdigit()]
    values = SecretsStore().resolve("telegram")
    chat_id = (values.get("chat_id") or "").strip()
    return [chat_id] if chat_id.lstrip("-").isdigit() else []


def _fmt_fiat(value: float | None, currency: str = "") -> str:
    if value is None:
        return "—"
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def _fmt_dt(value: Any) -> str:
    if not value:
        return "—"
    return str(value)[:19]


class TelegramCommandBot:
    """Long-polling command bot bound to a running ``KrakenDCA`` instance."""

    # Rate-limit knobs are instance attributes so tests can relax them.
    min_command_interval_seconds = MIN_COMMAND_INTERVAL_SECONDS
    max_commands_per_minute = MAX_COMMANDS_PER_MINUTE

    def __init__(
        self,
        app: "KrakenDCA",
        api: TelegramApiClient | None = None,
        audit_path: Path = AUDIT_LOG_PATH,
        alerts_path: Path = ALERTS_PATH,
    ):
        self.app = app
        values = SecretsStore().resolve("telegram")
        self.bot_token = (values.get("bot_token") or "").strip()
        self.allowed_chat_ids = _load_allowed_chat_ids()
        self._explicitly_disabled = os.environ.get("TELEGRAM_COMMANDS_ENABLED", "").lower() in (
            "false", "0", "no", "off",
        )
        self.enabled = bool(self.bot_token and self.allowed_chat_ids) and not self._explicitly_disabled
        self._api = api or (TelegramApi(self.bot_token) if self.bot_token else None)
        self._audit_path = audit_path
        self._alerts_path = alerts_path
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_command_at: dict[str, deque[float]] = defaultdict(deque)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Spawn the polling thread. Returns True when the bot is running."""
        if not self.enabled:
            print("Telegram command bot disabled (no bot token / allowed chat ID, or explicitly off).")
            return False
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-command-bot",
            daemon=True,
        )
        self._thread.start()
        print(f"Telegram command bot enabled (allowed chats: {len(self.allowed_chat_ids)}).")
        return True

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------ polling

    def _poll_loop(self) -> None:
        offset = 0
        backoff = 5.0
        while not self._stop_event.is_set():
            try:
                assert self._api is not None
                updates = self._api.get_updates(offset=offset)
                backoff = 5.0
            except TelegramApiError as e:
                # A persistent 401 means the token is bad/revoked: do not retry forever.
                if "401" in str(e) or "unauthorized" in str(e).lower():
                    print(f"Telegram command bot stopping: {e}")
                    return
                print(f"Telegram command bot poll error (retry in {backoff:.0f}s): {e}")
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    self._handle_update(update)
                except Exception as e:  # never let a bad update kill the loop
                    print(f"Telegram command bot update error: {redact_sensitive(str(e))}")

    # ------------------------------------------------------------------ updates

    def _handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if chat_id not in self.allowed_chat_ids:
            return  # silently ignore unknown chats
        text = (message.get("text") or "").strip()
        if not text:
            return
        self._handle_command(chat_id, text)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
        if chat_id not in self.allowed_chat_ids:
            return
        if self._api is not None:
            try:
                self._api.answer_callback(str(callback.get("id", "")))
            except TelegramApiError:
                pass
        data = str(callback.get("data") or "")
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "tg":
            return
        decision, token = parts[1], parts[2]
        pending = self._pending.pop(token, None)
        if (
            pending is None
            or pending["chat_id"] != chat_id
            or pending["expires"] < time.monotonic()
        ):
            self._send(chat_id, "⌛ That confirmation expired. Please run the command again.")
            return
        if decision != "ok":
            self._audit("telegram_action_cancelled", {"action": pending["action"]}, chat_id)
            self._send(chat_id, "❌ Cancelled.")
            return
        self._execute_confirmed(chat_id, pending)

    # ------------------------------------------------------------------ commands

    def _rate_limited(self, chat_id: str) -> bool:
        now = time.monotonic()
        window = self._last_command_at[chat_id]
        while window and now - window[0] > 60.0:
            window.popleft()
        if window and now - window[-1] < self.min_command_interval_seconds:
            return True
        window.append(now)
        return len(window) > self.max_commands_per_minute

    def _handle_command(self, chat_id: str, text: str) -> None:
        if self._rate_limited(chat_id):
            self._send(chat_id, "⏳ Slow down — one command at a time.")
            return
        # Strip a "@BotName" suffix (commands typed in a chat field).
        command, _, arg_string = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        args = arg_string.split()
        handler = {
            "/help": self._cmd_help,
            "/start": self._cmd_help,
            "/status": self._cmd_status,
            "/price": self._cmd_price,
            "/budget": self._cmd_budget,
            "/last": self._cmd_last,
            "/alerts": self._cmd_alerts,
            "/buy": self._cmd_buy,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
        }.get(command)
        if handler is None:
            self._send(chat_id, "Unknown command. Try /help.")
            return
        self._audit("telegram_command", {"command": command}, chat_id)
        try:
            text_out, keyboard = handler(chat_id, args)
        except Exception as e:
            self._send(chat_id, f"⚠️ Error: {redact_sensitive(str(e))[:300]}")
            return
        self._send(chat_id, text_out, keyboard)

    # ------------------------------------------------------------- command impls

    def _cmd_help(self, chat_id: str, args: list[str]) -> tuple[str, None]:
        return HELP_TEXT, None

    def _cmd_status(self, chat_id: str, args: list[str]) -> tuple[str, None]:
        app = self.app
        pair = app.config.trading_pair
        total_amount, avg_price, last_price, total_spent = app.store.get_statistics(pair)
        current_price = app.state.last_price or last_price or 0.0
        pl = total_amount * current_price - total_spent
        pl_pct = (pl / total_spent * 100) if total_spent > 0 else 0.0
        status = "⏸ paused" if (app.state.paused or app.overrides.paused) else app.state.status
        live = "LIVE" if app.config.live_trading_enabled else "dry-run"
        lines = [
            f"📊 Status: {status} ({live})",
            f"Portfolio: {total_amount:.8f} {pair[:3].lstrip('X')} (~{_fmt_fiat(total_amount * current_price, app.get_fiat_currency())})",
            f"P/L: {_fmt_fiat(pl, app.get_fiat_currency())} ({pl_pct:+.2f}%)",
            f"Planned buys: {app.state.estimated_buys}",
            f"Next cycle: {_fmt_dt(app.state.next_cycle_at)}",
        ]
        if app.state.last_error:
            lines.append(f"⚠️ Last error: {app.state.last_error[:120]}")
        return "\n".join(lines), None

    def _cmd_price(self, chat_id: str, args: list[str]) -> tuple[str, None]:
        app = self.app
        current = app.api.get_ticker(app.config.trading_pair)
        reference = app.get_reference_price()
        if reference and reference > 0:
            change = (current - reference) / reference * 100.0
            threshold, tier_amount = app._match_tier(change)
            resolved = app.resolve_buy_amount(current)
            if threshold is not None:
                tier_line = f"Active tier: {threshold:g}% → buy {resolved:.8f}"
            else:
                tier_line = f"No tier matched → base amount {resolved:.8f}"
            lines = [
                f"💹 Price: {current:,.2f} {app.get_fiat_currency()}",
                f"Reference ({app.config.dynamic_dca_reference}): {reference:,.2f} ({change:+.2f}%)",
                tier_line,
            ]
        else:
            lines = [
                f"💹 Price: {current:,.2f} {app.get_fiat_currency()}",
                "No reference price yet (no previous buys).",
            ]
        return "\n".join(lines), None

    def _cmd_budget(self, chat_id: str, args: list[str]) -> tuple[str, None]:
        app = self.app
        limit = app.config.max_monthly_amount
        currency = app.get_fiat_currency()
        if limit is None:
            return f"Monthly budget: not configured ({currency}).", None
        spent = app.store.get_monthly_spent(
            app.config.trading_pair, app.config.deposit_day, app.config.buy_hour
        )
        remaining = max(0.0, limit - spent)
        pct = min(100.0, spent / limit * 100) if limit > 0 else 0.0
        return (
            f"💰 Monthly budget: {_fmt_fiat(spent, currency)} / {_fmt_fiat(limit, currency)} ({pct:.0f}%)\n"
            f"Remaining: {_fmt_fiat(remaining, currency)}",
            None,
        )

    def _cmd_last(self, chat_id: str, args: list[str]) -> tuple[str, None]:
        app = self.app
        try:
            count = min(int(args[0]), 10) if args else 3
        except ValueError:
            count = 3
        count = max(1, count)
        txns = app.store.get_transactions(app.config.trading_pair)[-count:]
        if not txns:
            return "No transactions yet.", None
        lines = ["🧾 Last transactions:"]
        for t in txns:
            label = t.strategy
            if t.strategy == "dynamic" and t.dynamic_tier is not None:
                label = f"dynamic {t.dynamic_tier:g}%"
            when = (t.date or "")[:16].replace("T", " ")
            lines.append(f"{when} — {t.amount:.8f} @ {t.price:,.2f} ({label})")
        return "\n".join(lines), None

    def _cmd_alerts(self, chat_id: str, args: list[str]) -> tuple[str, None]:
        alerts = safe_load_json(self._alerts_path)
        if not isinstance(alerts, list):
            alerts = []
        active = [a for a in alerts if not a.get("acknowledged")]
        if not active:
            return "✅ No open alerts.", None
        lines = [f"⚠️ {len(active)} open alert(s):"]
        for a in active[:5]:
            message = str(a.get("message", "alert"))[:100]
            lines.append(f"• {message}")
        if len(active) > 5:
            lines.append(f"… and {len(active) - 5} more (see dashboard).")
        return "\n".join(lines), None

    def _confirm_keyboard(self, chat_id: str, action: str, description: str, over_budget: bool = False) -> dict[str, Any]:
        token = uuid.uuid4().hex
        self._pending[token] = {
            "action": action,
            "chat_id": chat_id,
            "expires": time.monotonic() + CONFIRM_TTL_SECONDS,
            "over_budget": over_budget,
        }
        # Opportunistic cleanup of expired tokens.
        now = time.monotonic()
        for stale in [t for t, p in self._pending.items() if p["expires"] < now]:
            self._pending.pop(stale, None)
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm", "callback_data": f"tg:ok:{token}"},
                    {"text": "❌ Cancel", "callback_data": f"tg:no:{token}"},
                ]
            ]
        }

    def _cmd_buy(self, chat_id: str, args: list[str]) -> tuple[str, dict[str, Any]]:
        app = self.app
        currency = app.get_fiat_currency()
        try:
            price = app.api.get_ticker(app.config.trading_pair)
            amount = app.resolve_buy_amount(price)
            cost = amount * price
        except Exception:
            amount, cost = app.config.crypto_amount, 0.0
        spent = 0.0
        over_budget = False
        if app.config.max_monthly_amount is not None:
            spent = app.store.get_monthly_spent(
                app.config.trading_pair, app.config.deposit_day, app.config.buy_hour
            )
            over_budget = spent + cost > app.config.max_monthly_amount
        keyboard = self._confirm_keyboard(chat_id, "buy", f"manual buy ~{amount:.8f}", over_budget=over_budget)
        warning = ""
        if over_budget:
            warning = (
                f"⚠️ This exceeds the monthly budget "
                f"({_fmt_fiat(spent, currency)} / {_fmt_fiat(app.config.max_monthly_amount, currency)} used).\n"
            )
        return (
            f"🛒 Manual buy request: {amount:.8f} (~{_fmt_fiat(cost, currency)})\n"
            f"{warning}Confirm to place the order.",
            keyboard,
        )

    def _cmd_pause(self, chat_id: str, args: list[str]) -> tuple[str, dict[str, Any]]:
        keyboard = self._confirm_keyboard(chat_id, "pause", "pause the bot")
        return "⏸ Pause the bot? No buys will run while paused.", keyboard

    def _cmd_resume(self, chat_id: str, args: list[str]) -> tuple[str, dict[str, Any]]:
        keyboard = self._confirm_keyboard(chat_id, "resume", "resume the bot")
        return "▶️ Resume the bot?", keyboard

    # ------------------------------------------------------- confirmed actions

    def _execute_confirmed(self, chat_id: str, pending: dict[str, Any]) -> None:
        app = self.app
        action = pending["action"]
        if action == "buy":
            over_budget = bool(pending.get("over_budget"))
            app.overrides.request_manual_cycle(over_budget=over_budget)
            app.state.wake()
            self._audit("telegram_manual_buy", {"over_budget": over_budget}, chat_id)
            note = " (approved over monthly budget)" if over_budget else ""
            self._send(chat_id, f"🛒 Buy request sent{note} — the bot will execute it on its next wake.")
        elif action == "pause":
            app.overrides.set_paused(True, reason="Paused via Telegram")
            app.state.update(paused=True, status="paused")
            app.state.wake()
            self._audit("telegram_bot_paused", {}, chat_id)
            self._send(chat_id, "⏸ Bot paused.")
        elif action == "resume":
            app.overrides.set_paused(False)
            app.state.update(paused=False, status="waiting")
            app.state.wake()
            self._audit("telegram_bot_resumed", {}, chat_id)
            self._send(chat_id, "▶️ Bot resumed.")

    # ------------------------------------------------------------------ helpers

    def _send(self, chat_id: str, text: str, keyboard: dict[str, Any] | None = None) -> None:
        if self._api is None:
            return
        try:
            self._api.send_message(chat_id, text, reply_markup=keyboard)
        except TelegramApiError as e:
            print(f"Telegram command bot send failed: {e}")

    def _audit(self, action: str, details: dict[str, Any], chat_id: str) -> None:
        try:
            entry = {
                "id": str(uuid.uuid4()),
                "timestamp": utc_now().isoformat(),
                "action": action,
                "details": details,
                "source_ip": "telegram",
                "user": f"telegram:...{chat_id[-4:]}",
            }
            entries = safe_load_json(self._audit_path)
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            atomic_write_json(self._audit_path, entries[-500:], indent=2)
        except Exception as e:
            print(f"Telegram command bot audit write failed: {redact_sensitive(str(e))}")
