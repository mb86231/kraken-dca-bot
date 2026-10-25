"""Morning briefing (story A2).

Daily Telegram briefing. The bot supplies every number deterministically
from a state snapshot; the AI only formulates the narrative. When the AI
is unavailable, a compact deterministic template briefing is sent
instead — the briefing never depends on the provider.

Guarantees:
- Schedule in Europe/Zurich with correct DST behaviour (zoneinfo).
- At-most-once per day: a persistent slot marker is written before the
  Telegram send and only removed again when the send fails, so restarts
  and retries never produce duplicates.
- Computed values are always shown separately; model text is never
  parsed for numbers.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from bot.advisor.news import Headline, NewsFetcher, NewsSource, filter_pair
from bot.advisor.service import Advisor
from bot.advisor.snapshot import build_state_snapshot
from bot.advisor.validate import Advice
from bot.config import (
    CUSTOM_PROMPT_MAX_CHARS,
    DEFAULT_NARRATIVE_FOCUS,
    NARRATIVE_FOCUS_OPTIONS,
)
from bot.utils import atomic_write_json, now_tz, safe_load_json, utc_now

logger = logging.getLogger("dca_bot.advisor.briefing")

BRIEFING_TZ = ZoneInfo("Europe/Zurich")
SNAPSHOT_SCHEMA_VERSION = "state-snapshot/v1"
# A briefing narrates a snapshot; 15 minutes is generous for build+send.
SNAPSHOT_MAX_AGE_SECONDS = 15 * 60
MAX_HISTORY = 30

BRIEFING_FREQUENCIES = ("daily", "weekly", "monthly")
# Content blocks of the deterministic briefing message. The AI narrative
# block and both news blocks are opt-in: by default the message is pure
# bot-computed values and consumes no tokens at all. Keep in sync with
# bot.config.DEFAULT_BRIEFING_SECTIONS.
DEFAULT_BRIEFING_SECTIONS: dict[str, bool] = {
    "market": True,
    "portfolio": True,
    "budget": True,
    "bot": True,
    "narrative": False,
    "news_general": False,
    "news_pair": False,
}

PROMPT_VERSION = "briefing/v2"
CUSTOM_PROMPT_VERSION = "briefing/custom"

# Manual test sends run inside an HTTP request behind a reverse proxy.
# Bound the provider call (one attempt, capped timeout) so the dashboard
# gets a real error message instead of a gateway 504 from the proxy. The
# cap must stay below the proxy timeout and above the realistic latency of
# reasoning models (kimi-k2.6 needs 40-50s for briefing prompts).
TEST_NARRATIVE_TIMEOUT_SECONDS = 90.0


def _system_prompt(max_words: int) -> str:
    return (
        "You are the concise briefing writer for a Kraken DCA bot. You receive "
        "a JSON payload with exactly the current numbers and, when available, "
        "news headlines fetched by the bot. Rules: narrate only the provided "
        "numbers and headlines, never invent or estimate values, never give "
        "financial advice or trading recommendations, keep the briefing under "
        f"{max_words} words, plain text, no markdown."
    )


def render_custom_prompt(template: str, values: dict[str, Any]) -> str:
    """Replace {placeholders} in a user-defined briefing prompt.

    Unknown placeholders are left untouched so the user sees the typo
    instead of silently losing text.
    """
    pnl_pct = values.get("pnl_pct")
    mapping = {
        "pair": values.get("trading_pair"),
        "price": values.get("last_price"),
        "invested": values.get("total_spent"),
        "holdings": values.get("total_amount"),
        "value_now": values.get("value_now"),
        "pnl_pct": round(float(pnl_pct), 1) if pnl_pct is not None else None,
        "budget_spent": values.get("budget_spent"),
        "budget_limit": values.get("budget_limit"),
        "next_run": format_local(values.get("next_cycle_at")),
        "buys": values.get("purchase_count"),
        "bot_status": values.get("bot_status"),
    }
    rendered = template[:CUSTOM_PROMPT_MAX_CHARS]
    for key, val in mapping.items():
        rendered = rendered.replace("{" + key + "}", "n/a" if val is None else str(val))
    return rendered


def parse_briefing_time(value: str) -> tuple[int, int]:
    """Parse "HH:MM" into (hour, minute); raises ValueError otherwise."""
    try:
        hour_str, minute_str = value.strip().split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        raise ValueError(f'advisor.briefing_time must look like "07:00", got {value!r}')
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'advisor.briefing_time must look like "07:00", got {value!r}')
    return hour, minute


def slot_for(now: datetime, hour: int, minute: int) -> datetime:
    """Today's briefing slot in Europe/Zurich local time."""
    local = now.astimezone(BRIEFING_TZ)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def is_briefing_day(local: datetime, frequency: str, weekday: int, month_day: int) -> bool:
    """True when ``local`` (Europe/Zurich) is a scheduled briefing day.

    ``weekday`` is 0=Monday … 6=Sunday (weekly); ``month_day`` is 1–28
    (monthly, capped so every month has the slot).
    """
    if frequency == "daily":
        return True
    if frequency == "weekly":
        return local.weekday() == weekday
    if frequency == "monthly":
        return local.day == month_day
    return False


def display_values(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The deterministic value set for the briefing message, including the
    bot-computed portfolio performance (value now vs invested)."""
    portfolio = snapshot.get("portfolio") or {}
    budget = snapshot.get("monthly_budget") or {}
    total_amount = portfolio.get("total_amount")
    total_spent = portfolio.get("total_spent")
    last_price = snapshot.get("last_price")
    value_now = pnl = pnl_pct = None
    if total_amount is not None and last_price is not None:
        value_now = float(total_amount) * float(last_price)
    if value_now is not None and total_spent:
        pnl = value_now - float(total_spent)
        pnl_pct = pnl / float(total_spent) * 100
    return {
        "trading_pair": snapshot.get("trading_pair"),
        "base": (snapshot.get("trading_pair") or "")[-3:].lstrip("Z") or "",
        "last_price": last_price,
        "last_price_at": snapshot.get("last_price_at"),
        "next_cycle_at": snapshot.get("next_cycle_at"),
        "estimated_buys": snapshot.get("estimated_buys"),
        "total_spent": total_spent,
        "total_amount": total_amount,
        "purchase_count": portfolio.get("purchase_count"),
        "value_now": value_now,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "budget_limit": budget.get("limit"),
        "budget_spent": budget.get("spent"),
        "bot_status": snapshot.get("bot_status"),
        "live_trading_enabled": snapshot.get("live_trading_enabled"),
    }


def _fmt_amount(value: Any, digits: int | None = None) -> str:
    """Thousands-separated number; floats trim trailing zeros by default."""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits is None:
        return f"{number:,.8f}".rstrip("0").rstrip(".")
    return f"{number:,.{digits}f}"


def _signed(value: float, digits: int) -> str:
    """Always-signed number for P/L display (+1.23 / −4.50)."""
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value):,.{digits}f}"


def format_local(value: Any, fmt: str = "%d %b, %H:%M") -> str:
    """Human-readable Europe/Zurich rendering of an ISO datetime (or 'n/a')."""
    if not value:
        return "n/a"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRIEFING_TZ)
    return parsed.astimezone(BRIEFING_TZ).strftime(fmt)


def _pnl_text(values: dict[str, Any]) -> str:
    pnl, pct = values.get("pnl"), values.get("pnl_pct")
    if pnl is None or pct is None:
        return "n/a"
    return f"{_signed(pnl, 2)} ({_signed(pct, 1)}%)"


def _headline_lines(headlines: list[Any], limit: int = 6) -> list[str]:
    """Render Headline objects or plain dicts as '- title (source)' lines."""
    lines = []
    for h in headlines[:limit]:
        if isinstance(h, dict):
            title, source = str(h.get("title", "")), str(h.get("source", ""))
        else:
            title, source = str(getattr(h, "title", "")), str(getattr(h, "source", ""))
        if title:
            lines.append(f"- {title}" + (f" ({source})" if source else ""))
    return lines


def build_briefing_message(
    *,
    values: dict[str, Any],
    slot_label: str,
    sections: dict[str, bool] | None = None,
    ai_text: str | None = None,
    news_general: list[Any] | None = None,
    news_pair: list[Any] | None = None,
) -> str:
    """Telegram briefing: deterministic blocks per enabled section, plus an
    optional AI narrative paragraph. Every number is bot-computed."""
    enabled = {**DEFAULT_BRIEFING_SECTIONS, **(sections or {})}
    blocks = [f"📊 DCA-Bot — Briefing\n{slot_label}"]
    if ai_text:
        blocks.append(ai_text.strip())
    if enabled.get("market"):
        blocks.append(
            f"💹 Market — {values.get('trading_pair') or 'n/a'}\n"
            f"Price: {_fmt_amount(values.get('last_price'))}\n"
            f"as of {format_local(values.get('last_price_at'))}"
        )
    if enabled.get("portfolio"):
        holdings = _fmt_amount(values.get("total_amount"))
        base = values.get("base") or ""
        blocks.append(
            "💼 Portfolio\n"
            f"Invested: {_fmt_amount(values.get('total_spent'))}\n"
            f"Holdings: {holdings} {base}".rstrip() + "\n"
            f"Value now: {_fmt_amount(values.get('value_now'))}\n"
            f"P/L: {_pnl_text(values)}"
        )
    if enabled.get("budget"):
        blocks.append(
            "📅 Budget & Plan\n"
            f"Monthly: {_fmt_amount(values.get('budget_spent'))}"
            f" / {_fmt_amount(values.get('budget_limit'))}\n"
            f"Next cycle: {format_local(values.get('next_cycle_at'))}\n"
            f"Buys left (est.): {_fmt_amount(values.get('estimated_buys'))}"
        )
    if enabled.get("bot"):
        blocks.append(
            "🤖 Bot\n"
            f"Status: {values.get('bot_status') or 'n/a'}\n"
            f"Live trading: {'ON' if values.get('live_trading_enabled') else 'OFF'}"
        )
    if enabled.get("news_general") and news_general:
        blocks.append("📰 Crypto News\n" + "\n".join(_headline_lines(news_general)))
    if enabled.get("news_pair") and news_pair:
        blocks.append(
            f"🎯 {values.get('trading_pair') or 'Pair'} News\n"
            + "\n".join(_headline_lines(news_pair))
        )
    return "\n\n".join(blocks)


@dataclass
class BriefingResult:
    sent: bool
    slot: str | None = None
    source: str | None = None
    reason: str | None = None


@dataclass
class TestBriefingResult:
    """Outcome of the manual dashboard test send (story A9)."""

    ok: bool
    source: str | None = None
    text: str | None = None
    telegram_sent: bool = False
    latency_ms: float | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "text": self.text,
            "telegram_sent": self.telegram_sent,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
        }


class BriefingService:
    """Briefing scheduler. All state lives in two JSON files so the
    at-most-once guarantee survives restarts."""

    def __init__(
        self,
        advisor: Advisor,
        notifier: Any,
        snapshot_fn: Callable[[], dict[str, Any]],
        *,
        schedule: str = "07:00",
        frequency: str = "daily",
        weekday: int = 0,
        month_day: int = 1,
        sections: dict[str, bool] | None = None,
        narrative_focus: tuple[str, ...] | list[str] = DEFAULT_NARRATIVE_FOCUS,
        custom_prompt: str = "",
        narrative_max_words: int = 120,
        news_feeds: tuple[str, ...] | list[str] | None = None,
        news_fetcher: NewsSource | None = None,
        state_file: str | Path = "data/briefing_state.json",
        history_file: str | Path = "data/briefing_history.json",
        max_history: int = MAX_HISTORY,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.advisor = advisor
        self.notifier = notifier
        self.snapshot_fn = snapshot_fn
        self.schedule_hour, self.schedule_minute = parse_briefing_time(schedule)
        self.frequency = frequency if frequency in BRIEFING_FREQUENCIES else "daily"
        self.weekday = max(0, min(6, int(weekday)))
        self.month_day = max(1, min(28, int(month_day)))
        self.sections = {**DEFAULT_BRIEFING_SECTIONS, **(sections or {})}
        focus = tuple(f for f in narrative_focus if f in NARRATIVE_FOCUS_OPTIONS)
        self.narrative_focus = focus or DEFAULT_NARRATIVE_FOCUS
        self.custom_prompt = str(custom_prompt or "")[:CUSTOM_PROMPT_MAX_CHARS]
        self.narrative_max_words = max(40, min(400, int(narrative_max_words)))
        # News is fetched by the bot itself (RSS/Atom); injectable for tests.
        self.news_fetcher = news_fetcher or (
            NewsFetcher(news_feeds) if news_feeds else None
        )
        self.state_file = Path(state_file)
        self.history_file = Path(history_file)
        self.max_history = max_history
        self._now = now_provider or (lambda: now_tz())
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, tick_seconds: float = 60.0) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            args=(max(5.0, tick_seconds),),
            name="advisor-briefing",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self, tick_seconds: float) -> None:
        while not self._stop_event.wait(tick_seconds):
            try:
                self.check_and_send()
            except Exception as exc:
                # The briefing must never disturb the bot.
                logger.warning("Briefing tick failed: %s", exc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_marker(self) -> str | None:
        data = safe_load_json(self.state_file)
        if isinstance(data, dict):
            slot = data.get("last_sent_slot")
            return str(slot) if slot else None
        return None

    def _write_marker(self, slot: str) -> None:
        atomic_write_json(self.state_file, {"last_sent_slot": slot})

    def _clear_marker(self) -> None:
        atomic_write_json(self.state_file, {"last_sent_slot": None})

    def load_history(self) -> list[dict[str, Any]]:
        data = safe_load_json(self.history_file)
        return data if isinstance(data, list) else []

    def _append_history(self, entry: dict[str, Any]) -> None:
        history = self.load_history()
        history.append(entry)
        atomic_write_json(self.history_file, history[-self.max_history:])

    # ------------------------------------------------------------------
    # Core send logic (synchronous; also used directly by tests)
    # ------------------------------------------------------------------

    def _slot_label(self, slot_dt: datetime) -> str:
        return slot_dt.strftime("%A, %d %B %Y · %H:%M (Zurich)")

    def _fetch_news(
        self, snapshot: dict[str, Any]
    ) -> tuple[list[Headline], list[Headline]]:
        """General and pair-specific headlines for this briefing.

        Best-effort: a failing feed yields no headlines for that block,
        never an exception into the briefing.
        """
        pair = snapshot.get("trading_pair") or ""
        want_general = self.sections.get("news_general")
        want_pair = self.sections.get("news_pair") and bool(pair)
        if not (want_general or want_pair) or self.news_fetcher is None:
            return [], []
        try:
            headlines = self.news_fetcher.fetch_all()
        except Exception as exc:
            logger.warning("News fetch failed; briefing continues without news: %s", exc)
            return [], []
        general = headlines if want_general else []
        pair_news = filter_pair(headlines, pair) if want_pair else []
        return list(general), list(pair_news)

    def _request_narrative(
        self,
        snapshot: dict[str, Any],
        values: dict[str, Any],
        news: list[Headline],
        *,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> "Advice | None":
        """Optional AI paragraph. Returns None when the narrative section is
        disabled (no provider call, no tokens); otherwise returns the advice
        for the caller to inspect — ok or not. ``timeout_seconds`` and
        ``max_retries`` bound the provider call for latency-sensitive paths
        (manual test sends); the scheduled briefing uses provider defaults."""
        if not self.sections.get("narrative"):
            return None
        data_timestamp = snapshot.get("generated_at")
        custom = bool(self.custom_prompt.strip())
        task = (
            render_custom_prompt(self.custom_prompt, values)
            if custom
            else "Write the morning briefing for this snapshot."
        )
        payload: dict[str, Any] = {"task": task, "snapshot": snapshot}
        if self.narrative_focus:
            payload["focus"] = list(self.narrative_focus)
        if "news" in self.narrative_focus and news:
            payload["news_headlines"] = [
                f"{h.title} ({h.source})" if h.source else h.title for h in news[:8]
            ]
        advice = self.advisor.ask(
            "briefing",
            system_prompt=_system_prompt(self.narrative_max_words),
            user_prompt=json.dumps(payload, default=str),
            prose=True,
            prompt_version=CUSTOM_PROMPT_VERSION if custom else PROMPT_VERSION,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            data_timestamp=datetime.fromisoformat(data_timestamp)
            if data_timestamp
            else None,
            max_age_seconds=SNAPSHOT_MAX_AGE_SECONDS,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        if not (advice.ok and advice.data and advice.data.get("text")):
            logger.info(
                "Briefing AI narrative unavailable (%s); deterministic message only",
                advice.error or advice.status.value,
            )
        return advice

    def check_and_send(self, now: datetime | None = None) -> BriefingResult:
        """Send the briefing for the current slot if due and not yet sent.

        Idempotency: the slot marker is persisted BEFORE the Telegram send;
        it is only cleared when the send fails (enabling a retry). A crash
        after a successful send therefore cannot produce a duplicate.
        """
        if not self.advisor.feature_enabled("briefing"):
            return BriefingResult(sent=False, reason="briefing feature disabled")
        current = (now or self._now()).astimezone(BRIEFING_TZ)
        if not is_briefing_day(current, self.frequency, self.weekday, self.month_day):
            return BriefingResult(sent=False, reason="not a briefing day")
        slot_dt = slot_for(current, self.schedule_hour, self.schedule_minute)
        if current < slot_dt:
            return BriefingResult(sent=False, reason="slot not reached")
        slot = slot_dt.date().isoformat()

        with self._send_lock:
            if self._load_marker() == slot:
                return BriefingResult(sent=False, slot=slot, reason="already sent")

            snapshot = self.snapshot_fn()
            values = display_values(snapshot)
            data_timestamp = snapshot.get("generated_at")
            general_news, pair_news = self._fetch_news(snapshot)
            narrative_news = list(general_news) + list(pair_news)

            advice = self._request_narrative(snapshot, values, narrative_news)
            ai_text = (
                advice.data["text"]
                if advice is not None and advice.ok and advice.data
                else None
            )
            source = "ai" if ai_text else "detailed"
            message = build_briefing_message(
                values=values,
                slot_label=self._slot_label(slot_dt),
                sections=self.sections,
                ai_text=ai_text,
                news_general=general_news,
                news_pair=pair_news,
            )

            entry = {
                "slot": slot,
                "generated_at": utc_now().isoformat(),
                "data_timestamp": data_timestamp,
                "source": source,
                "values": values,
                "text": message,
                "narrative": ai_text,
                "news_general": _headline_lines(general_news),
                "news_pair": _headline_lines(pair_news),
            }
            self._append_history(entry)
            # Marker first: at-most-once even if we crash during the send.
            self._write_marker(slot)
            try:
                self.notifier.send(message)
            except Exception as exc:
                # Roll back marker and the history entry so the retry is clean.
                self._clear_marker()
                history = self.load_history()
                if history and history[-1].get("slot") == slot:
                    atomic_write_json(self.history_file, history[:-1])
                logger.warning("Briefing send failed, marker cleared for retry: %s", exc)
                return BriefingResult(sent=False, slot=slot, reason=f"send failed: {exc}")
            return BriefingResult(sent=True, slot=slot, source=source)

    # ------------------------------------------------------------------
    # Manual test send (story A9)
    # ------------------------------------------------------------------

    def send_test_briefing(self) -> TestBriefingResult:
        """Manual end-to-end test triggered from the dashboard.

        Sends the real briefing message — snapshot -> optional AI narrative
        -> Telegram — without consuming the scheduled slot and without
        writing to the briefing history. When the narrative section is
        enabled, a provider failure is reported as-is (never masked): the
        point of a test is to surface real problems. When it is disabled,
        the deterministic message is tested and the AI is not called.
        """
        if not self.advisor.enabled:
            return TestBriefingResult(
                ok=False,
                reason=self.advisor.disabled_reason or "advisor is disabled",
            )
        if not self.advisor.feature_enabled("briefing"):
            return TestBriefingResult(
                ok=False,
                reason="briefing feature is not enabled",
            )

        snapshot = self.snapshot_fn()
        values = display_values(snapshot)
        narrative_enabled = bool(self.sections.get("narrative"))
        general_news, pair_news = self._fetch_news(snapshot)
        narrative_news = list(general_news) + list(pair_news)

        started = time.monotonic()
        advice = self._request_narrative(
            snapshot,
            values,
            narrative_news,
            # Keep the web request well inside the reverse-proxy timeout so
            # a slow/unreachable provider surfaces as a real error, not 504.
            timeout_seconds=min(
                float(self.advisor.settings.timeout_seconds),
                TEST_NARRATIVE_TIMEOUT_SECONDS,
            ),
            max_retries=0,
        )
        latency_ms = round((time.monotonic() - started) * 1000, 1)

        ai_text: str | None = None
        if narrative_enabled:
            if advice is None or not (advice.ok and advice.data and advice.data.get("text")):
                status = "disabled" if advice is None else (advice.error or advice.status.value)
                failure_reason = f"AI request failed: {status}"
                if advice is not None and advice.raw_excerpt:
                    failure_reason += f' — model replied: "{advice.raw_excerpt}"'
                return TestBriefingResult(
                    ok=False,
                    latency_ms=latency_ms,
                    reason=failure_reason,
                )
            ai_text = advice.data["text"]

        message = build_briefing_message(
            values=values,
            slot_label="manual test",
            sections=self.sections,
            ai_text=ai_text,
            news_general=general_news,
            news_pair=pair_news,
        )
        try:
            self.notifier.send(message)
            telegram_sent = True
            reason = None if narrative_enabled else "AI narrative disabled — deterministic message only"
        except Exception as exc:
            telegram_sent = False
            reason = f"Telegram send failed: {exc}"
        return TestBriefingResult(
            ok=True,
            source="ai" if ai_text else "detailed",
            text=ai_text,
            telegram_sent=telegram_sent,
            latency_ms=latency_ms if narrative_enabled else None,
            reason=reason,
        )


def make_snapshot_fn(config: Any, state: Any, store: Any) -> Callable[[], dict[str, Any]]:
    """Standard snapshot factory used by KrakenDCA."""
    def _fn() -> dict[str, Any]:
        return build_state_snapshot(config, state, store)

    return _fn
