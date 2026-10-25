"""Tests for the morning briefing (story A2).

Covers Europe/Zurich scheduling with DST edges, at-most-once idempotency
across restarts and retries, the deterministic template fallback,
value/narrative separation, and history handling.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from bot.advisor.briefing import (
    BriefingService,
    build_briefing_message,
    display_values,
    is_briefing_day,
    parse_briefing_time,
    render_custom_prompt,
    slot_for,
)
from bot.advisor.news import Headline
from bot.advisor.provider import OpenAICompatibleClient, ProviderResult
from bot.advisor.service import Advisor, AdvisorSettings
from bot.config import Config

ZURICH = ZoneInfo("Europe/Zurich")


class _FakeNotifier:
    def __init__(self, fail_times: int = 0):
        self.messages: list[str] = []
        self._fail_times = fail_times

    def send(self, message: str) -> bool:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("telegram down")
        self.messages.append(message)
        return True


def _handler_returning(content: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"total_tokens": 20},
            },
        )

    return handler


def _advisor(handler, *, features: dict[str, bool] | None = None) -> Advisor:
    settings = AdvisorSettings(
        enabled=True,
        provider="openai",
        base_url="https://example.test/v1",
        model="test-model",
        api_key="test-key",
        features=features or {"briefing": True, "explainer": False},
    )
    provider = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        timeout=5.0,
        max_retries=0,
    )
    provider._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0)
    )
    return Advisor(settings, provider=provider)


def _snapshot(price: float = 50000.0, *, stale: bool = False) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    if stale:
        generated_at -= timedelta(hours=2)
    return {
        "schema_version": "state-snapshot/v1",
        "generated_at": generated_at.isoformat(),
        "trading_pair": "XBTCHF",
        "mode": "recurring",
        "live_trading_enabled": False,
        "bot_status": "running",
        "last_price": price,
        "last_price_at": generated_at.isoformat(),
        "next_cycle_at": None,
        "estimated_buys": 12,
        "portfolio": {
            "total_amount": 0.01,
            "avg_buy_price": 48000.0,
            "last_buy_price": 49000.0,
            "total_spent": 480.0,
            "purchase_count": 3,
        },
        "monthly_budget": {"limit": 1000.0, "spent": 480.0},
    }


def _service(
    tmp_path: Path,
    advisor: Advisor,
    notifier: _FakeNotifier,
    snapshot: dict[str, Any],
    *,
    schedule: str = "07:00",
    frequency: str = "daily",
    weekday: int = 0,
    month_day: int = 1,
    sections: dict[str, bool] | None = None,
    max_history: int = 30,
) -> BriefingService:
    return BriefingService(
        advisor,
        notifier,
        lambda: snapshot,
        schedule=schedule,
        frequency=frequency,
        weekday=weekday,
        month_day=month_day,
        sections=sections,
        state_file=tmp_path / "briefing_state.json",
        history_file=tmp_path / "briefing_history.json",
        max_history=max_history,
    )


# ---------------------------------------------------------------------------
# Schedule parsing and slot computation (incl. DST)
# ---------------------------------------------------------------------------


def test_parse_briefing_time_valid():
    assert parse_briefing_time("07:00") == (7, 0)
    assert parse_briefing_time("23:59") == (23, 59)
    assert parse_briefing_time("0:15") == (0, 15)


@pytest.mark.parametrize("value", ["7", "0700", "25:00", "12:60", "aa:bb", ""])
def test_parse_briefing_time_invalid(value: str):
    with pytest.raises(ValueError):
        parse_briefing_time(value)


def test_slot_for_is_7am_local():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    slot = slot_for(now, 7, 0)
    assert slot.hour == 7 and slot.minute == 0
    assert slot.utcoffset() == timedelta(hours=2)  # CEST in June


def test_slot_for_spring_forward_transition():
    # 2026-03-29: clocks jump 02:00 -> 03:00 (CET -> CEST).
    before = datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc)  # 00:00 CET Mar 29
    slot = slot_for(before, 7, 0)
    assert slot.date().isoformat() == "2026-03-29"
    assert slot.hour == 7
    assert slot.utcoffset() == timedelta(hours=2)  # already CEST at 07:00


def test_slot_for_autumn_fallback_transition():
    # 2026-10-25: clocks fall back 03:00 -> 02:00 (CEST -> CET).
    before = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)  # 00:00 CEST Oct 25
    slot = slot_for(before, 7, 0)
    assert slot.date().isoformat() == "2026-10-25"
    assert slot.hour == 7
    assert slot.utcoffset() == timedelta(hours=1)  # CET by 07:00


# ---------------------------------------------------------------------------
# Briefing frequency: daily / weekly / monthly
# ---------------------------------------------------------------------------

NARRATIVE_ON = {"narrative": True}


def test_is_briefing_day_daily_always():
    day = datetime(2026, 6, 17, 12, 0, tzinfo=ZURICH)  # a Wednesday
    assert is_briefing_day(day, "daily", 0, 1) is True


def test_is_briefing_day_weekly_matches_weekday():
    monday = datetime(2026, 6, 15, 12, 0, tzinfo=ZURICH)
    tuesday = datetime(2026, 6, 16, 12, 0, tzinfo=ZURICH)
    assert is_briefing_day(monday, "weekly", 0, 1) is True
    assert is_briefing_day(tuesday, "weekly", 0, 1) is False
    assert is_briefing_day(tuesday, "weekly", 1, 1) is True


def test_is_briefing_day_monthly_matches_day():
    fifteenth = datetime(2026, 6, 15, 12, 0, tzinfo=ZURICH)
    sixteenth = datetime(2026, 6, 16, 12, 0, tzinfo=ZURICH)
    assert is_briefing_day(fifteenth, "monthly", 0, 15) is True
    assert is_briefing_day(sixteenth, "monthly", 0, 15) is False


def test_weekly_briefing_skips_other_days(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("weekly narrative"))
    notifier = _FakeNotifier()
    # 2026-06-15 is a Monday; weekly on Tuesday must not send.
    service = _service(
        tmp_path, advisor, notifier, _snapshot(), frequency="weekly", weekday=1
    )
    result = service.check_and_send(now=morning)
    assert result.sent is False
    assert result.reason == "not a briefing day"
    assert notifier.messages == []
    advisor.executor.shutdown(wait=True)


def test_weekly_briefing_sends_on_matching_day(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("weekly narrative"))
    notifier = _FakeNotifier()
    service = _service(
        tmp_path, advisor, notifier, _snapshot(), frequency="weekly", weekday=0
    )
    result = service.check_and_send(now=morning)
    assert result.sent is True
    advisor.executor.shutdown(wait=True)


def test_monthly_briefing_skips_other_days(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("monthly narrative"))
    notifier = _FakeNotifier()
    service = _service(
        tmp_path, advisor, notifier, _snapshot(), frequency="monthly", month_day=16
    )
    result = service.check_and_send(now=morning)  # the 15th
    assert result.sent is False
    assert result.reason == "not a briefing day"
    advisor.executor.shutdown(wait=True)


def test_check_and_send_respects_dst_boundary(tmp_path: Path):
    # 06:59 local on the autumn-transition day: not yet due.
    advisor = _advisor(_handler_returning("quiet day"))
    notifier = _FakeNotifier()
    early = datetime(2026, 10, 25, 4, 59, tzinfo=timezone.utc)  # 06:59 CEST
    snapshot = _snapshot()
    service = _service(tmp_path, advisor, notifier, snapshot)

    result = service.check_and_send(now=early)

    assert result.sent is False
    assert result.reason == "slot not reached"
    assert notifier.messages == []
    advisor.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Due / idempotency
# ---------------------------------------------------------------------------


@pytest.fixture
def morning() -> datetime:
    return datetime(2026, 6, 15, 7, 30, tzinfo=ZURICH)


def test_not_sent_before_slot(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("too early"))
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())
    early = morning.replace(hour=6, minute=59)

    result = service.check_and_send(now=early)

    assert result.sent is False
    assert result.reason == "slot not reached"
    assert notifier.messages == []
    assert service.load_history() == []
    advisor.executor.shutdown(wait=True)


def test_sent_after_slot_and_only_once_per_day(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("Markets look steady today."))
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=NARRATIVE_ON)

    first = service.check_and_send(now=morning)
    assert first.sent is True
    assert first.source == "ai"
    assert len(notifier.messages) == 1

    # Same day: never a duplicate, even on explicit re-check.
    second = service.check_and_send(now=morning.replace(hour=12, minute=0))
    assert second.sent is False
    assert second.reason == "already sent"
    assert len(notifier.messages) == 1
    assert len(service.load_history()) == 1
    advisor.executor.shutdown(wait=True)


def test_restart_does_not_duplicate(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("steady"))
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())
    assert service.check_and_send(now=morning).sent is True

    # Simulate a restart: a brand-new service over the same state files.
    advisor2 = _advisor(_handler_returning("steady"))
    service2 = _service(tmp_path, advisor2, notifier, _snapshot())
    again = service2.check_and_send(now=morning.replace(hour=18, minute=0))
    assert again.sent is False
    assert again.reason == "already sent"
    assert len(notifier.messages) == 1
    advisor.executor.shutdown(wait=True)
    advisor2.executor.shutdown(wait=True)


def test_next_day_sends_again(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("day one"))
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())
    assert service.check_and_send(now=morning).sent is True

    advisor2 = _advisor(_handler_returning("day two"))
    next_day = morning + timedelta(days=1)
    service2 = _service(tmp_path, advisor2, notifier, _snapshot())
    result = service2.check_and_send(now=next_day)
    assert result.sent is True
    assert len(notifier.messages) == 2
    advisor.executor.shutdown(wait=True)
    advisor2.executor.shutdown(wait=True)


def test_telegram_failure_retries_without_duplicate(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("retry me"))
    notifier = _FakeNotifier(fail_times=1)
    service = _service(tmp_path, advisor, notifier, _snapshot())

    failed = service.check_and_send(now=morning)
    assert failed.sent is False
    assert len(service.load_history()) == 0  # rolled back

    retried = service.check_and_send(now=morning)
    assert retried.sent is True
    assert len(notifier.messages) == 1
    assert len(service.load_history()) == 1
    advisor.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Value / narrative separation and fallback
# ---------------------------------------------------------------------------


def test_model_numbers_never_replace_bot_values(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("The price soared to 999999 yesterday!"))
    notifier = _FakeNotifier()
    snapshot = _snapshot(price=50000.0)
    service = _service(tmp_path, advisor, notifier, snapshot, sections=NARRATIVE_ON)

    result = service.check_and_send(now=morning)

    assert result.source == "ai"
    entry = service.load_history()[-1]
    assert entry["values"]["last_price"] == 50000.0
    # The Telegram message carries the narrative and the bot-computed
    # values; the AI text is never parsed for numbers.
    message = notifier.messages[0]
    assert "The price soared to 999999 yesterday!" in message
    assert "Price: 50,000" in message
    assert entry["values"]["total_spent"] == 480.0
    # The AI narrative is archived as its own field so the dashboard can
    # show it as the summary, separate from the deterministic blocks.
    assert entry["narrative"] == "The price soared to 999999 yesterday!"
    advisor.executor.shutdown(wait=True)


def test_deterministic_message_when_provider_fails(tmp_path: Path, morning: datetime):
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    advisor = _advisor(failing)
    notifier = _FakeNotifier()
    snapshot = _snapshot(price=50000.0)
    service = _service(tmp_path, advisor, notifier, snapshot, sections=NARRATIVE_ON)

    result = service.check_and_send(now=morning)

    # The message is deterministic; a failing AI narrative never blocks it.
    assert result.sent is True
    assert result.source == "detailed"
    entry = service.load_history()[-1]
    assert "Price: 50,000" in entry["text"]
    assert "P/L:" in notifier.messages[0]
    # Without a usable AI reply the archived entry carries no narrative;
    # the dashboard falls back to a bot-composed summary.
    assert entry["narrative"] is None
    advisor.executor.shutdown(wait=True)


def test_deterministic_message_when_snapshot_stale(
    tmp_path: Path, morning: datetime
):
    advisor = _advisor(_handler_returning("fresh text"))
    notifier = _FakeNotifier()
    stale_snapshot = _snapshot(price=50000.0, stale=True)
    service = _service(
        tmp_path, advisor, notifier, stale_snapshot, sections=NARRATIVE_ON
    )

    result = service.check_and_send(now=morning)

    assert result.source == "detailed"
    advisor.executor.shutdown(wait=True)


def test_narrative_disabled_skips_provider_entirely(
    tmp_path: Path, morning: datetime
):
    calls = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": []})

    advisor = _advisor(counting)
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())  # narrative off

    result = service.check_and_send(now=morning)

    assert result.sent is True
    assert result.source == "detailed"
    assert calls == 0  # zero tokens consumed
    assert "P/L:" in notifier.messages[0]
    advisor.executor.shutdown(wait=True)


def test_feature_disabled_sends_nothing(tmp_path: Path, morning: datetime):
    advisor = _advisor(
        _handler_returning("nope"),
        features={"briefing": False, "explainer": False},
    )
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())

    result = service.check_and_send(now=morning)

    assert result.sent is False
    assert "disabled" in (result.reason or "")
    assert notifier.messages == []
    assert service.load_history() == []
    advisor.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# History and message format
# ---------------------------------------------------------------------------


def test_history_trimmed_to_max(tmp_path: Path, morning: datetime):
    notifier = _FakeNotifier()
    history_file = tmp_path / "briefing_history.json"
    service: BriefingService | None = None
    advisors: list[Advisor] = []
    for day in range(4):
        current = morning + timedelta(days=day)
        advisor = _advisor(_handler_returning(f"day {day}"))
        advisors.append(advisor)
        snapshot = _snapshot()
        snapshot_fn = lambda s=snapshot: s  # noqa: E731
        service = BriefingService(
            advisor,
            notifier,
            snapshot_fn,
            schedule="07:00",
            sections=NARRATIVE_ON,
            state_file=tmp_path / "briefing_state.json",
            history_file=history_file,
            max_history=3,
        )
        assert service.check_and_send(now=current).sent is True

    history = service.load_history()  # type: ignore[union-attr]
    assert len(history) == 3
    assert "day 3" in history[-1]["text"]
    for advisor in advisors:
        advisor.executor.shutdown(wait=True)


def test_message_formats_times_human_readable(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("calm"))
    notifier = _FakeNotifier()
    snapshot = _snapshot(price=50000.0)
    service = _service(tmp_path, advisor, notifier, snapshot)
    assert service.check_and_send(now=morning).sent is True

    message = notifier.messages[0]
    assert "(Zurich)" in message
    assert "Monday, 15 June 2026" in message
    assert "as of" in message
    # Raw ISO timestamps are gone from the operator-facing message.
    assert snapshot["generated_at"] not in message
    advisor.executor.shutdown(wait=True)


def test_message_computes_portfolio_pnl(tmp_path: Path, morning: datetime):
    # 0.01 @ 50,000 = 500 value vs 480 invested → +20.00 (+4.2%).
    advisor = _advisor(_handler_returning("calm"))
    notifier = _FakeNotifier()
    snapshot = _snapshot(price=50000.0)
    service = _service(tmp_path, advisor, notifier, snapshot)
    assert service.check_and_send(now=morning).sent is True

    message = notifier.messages[0]
    assert "Value now: 500" in message
    assert "P/L: +20.00 (+4.2%)" in message
    advisor.executor.shutdown(wait=True)


def test_message_respects_disabled_sections(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("calm"))
    notifier = _FakeNotifier()
    sections = {"market": False, "portfolio": True, "budget": True, "bot": True}
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=sections)
    assert service.check_and_send(now=morning).sent is True

    message = notifier.messages[0]
    assert "💹 Market" not in message
    assert "💼 Portfolio" in message
    assert "📅 Budget & Plan" in message
    assert "🤖 Bot" in message
    advisor.executor.shutdown(wait=True)


def test_build_briefing_message_handles_missing_values():
    values = display_values({"trading_pair": "XBTCHF", "portfolio": {}})
    text = build_briefing_message(values=values, slot_label="manual test")
    assert "n/a" in text
    assert "XBTCHF" in text


def test_build_briefing_message_includes_ai_narrative_and_blocks():
    values = display_values(
        {
            "trading_pair": "XBTCHF",
            "last_price": 50000.0,
            "portfolio": {"total_amount": 0.01, "total_spent": 480.0},
            "monthly_budget": {"limit": 1000.0, "spent": 480.0},
        }
    )
    message = build_briefing_message(
        values=values,
        slot_label="manual test",
        ai_text="Some narrative.",
    )
    assert "Some narrative." in message
    assert "Price: 50,000" in message
    assert "P/L:" in message
    assert "📊 DCA-Bot — Briefing" in message


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _config_with_advisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, advisor: dict[str, Any]) -> Config:
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    data = {
        "trading_pair": "XBTCHF",
        "deposit_day": 24,
        "crypto_amount": 0.0001,
        "dip_threshold_percent": 5.0,
        "poll_interval_seconds": 300,
        "buy_hour": 8,
        "dip_buy_cooldown_hours": 2.0,
        "advisor": advisor,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return Config(config_path=path)


def test_config_briefing_time_default_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _config_with_advisor(
        tmp_path, monkeypatch, {"enabled": True, "model": "m"}
    )
    assert config.advisor_briefing_time == "07:00"

    with pytest.raises(Exception, match="briefing_time"):
        _config_with_advisor(
            tmp_path, monkeypatch, {"enabled": True, "model": "m", "briefing_time": "7am"}
        )


def test_config_to_dict_includes_briefing_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _config_with_advisor(
        tmp_path,
        monkeypatch,
        {"enabled": True, "model": "m", "briefing_time": "08:30"},
    )
    assert config.to_dict()["advisor"]["briefing_time"] == "08:30"


def test_config_briefing_schedule_defaults_and_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _config_with_advisor(
        tmp_path, monkeypatch, {"enabled": True, "model": "m"}
    )
    assert config.advisor_briefing_frequency == "daily"
    assert config.advisor_briefing_weekday == 0
    assert config.advisor_briefing_month_day == 1
    assert config.advisor_briefing_sections["narrative"] is False
    assert config.advisor_briefing_sections["market"] is True

    advisor = config.to_dict()["advisor"]
    assert advisor["briefing_frequency"] == "daily"
    assert advisor["briefing_sections"]["portfolio"] is True


def test_config_briefing_schedule_custom_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _config_with_advisor(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "model": "m",
            "briefing_frequency": "weekly",
            "briefing_weekday": 3,
            "briefing_sections": {"market": False, "narrative": True, "bogus": True},
        },
    )
    assert config.advisor_briefing_frequency == "weekly"
    assert config.advisor_briefing_weekday == 3
    assert config.advisor_briefing_sections["market"] is False
    assert config.advisor_briefing_sections["narrative"] is True
    assert config.advisor_briefing_sections["portfolio"] is True  # default kept
    assert "bogus" not in config.advisor_briefing_sections  # unknown dropped


def test_config_briefing_schedule_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with pytest.raises(Exception, match="briefing_frequency"):
        _config_with_advisor(
            tmp_path,
            monkeypatch,
            {"enabled": True, "model": "m", "briefing_frequency": "hourly"},
        )
    with pytest.raises(Exception, match="briefing_weekday"):
        _config_with_advisor(
            tmp_path,
            monkeypatch,
            {"enabled": True, "model": "m", "briefing_weekday": 7},
        )
    with pytest.raises(Exception, match="briefing_month_day"):
        _config_with_advisor(
            tmp_path,
            monkeypatch,
            {"enabled": True, "model": "m", "briefing_month_day": 31},
        )


def test_thread_lifecycle(tmp_path: Path, morning: datetime):
    advisor = _advisor(_handler_returning("tick"))
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())
    service.start(tick_seconds=5)
    assert service._thread is not None
    service.stop()
    assert service._thread is None
    advisor.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Manual test send from the dashboard (story A9)
# ---------------------------------------------------------------------------

def test_test_briefing_disabled_advisor(tmp_path: Path):
    advisor = Advisor.disabled_stub("disabled in config")
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())
    result = service.send_test_briefing()
    assert result.ok is False
    assert "disabled" in (result.reason or "")
    assert notifier.messages == []


def test_test_briefing_feature_off(tmp_path: Path):
    advisor = _advisor(
        _handler_returning("hello"),
        features={"briefing": False, "explainer": False},
    )
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())
    result = service.send_test_briefing()
    assert result.ok is False
    assert "not enabled" in (result.reason or "")
    assert notifier.messages == []


def test_test_briefing_success_narrative_off_sends_deterministic(tmp_path: Path):
    """With the narrative section off (default), the test validates the
    deterministic message end-to-end and never calls the provider."""
    calls = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": []})

    advisor = _advisor(counting)
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot())
    result = service.send_test_briefing()
    assert result.ok is True
    assert result.source == "detailed"
    assert result.text is None
    assert result.telegram_sent is True
    assert result.latency_ms is None
    assert calls == 0
    assert "narrative" in (result.reason or "").lower()
    assert len(notifier.messages) == 1
    assert "manual test" in notifier.messages[0]
    assert "P/L:" in notifier.messages[0]
    # A test must neither consume the daily slot nor write history.
    assert not (tmp_path / "briefing_state.json").exists()
    assert not (tmp_path / "briefing_history.json").exists()


def test_test_briefing_success_with_narrative(tmp_path: Path):
    advisor = _advisor(_handler_returning("All quiet, price steady."))
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=NARRATIVE_ON)
    result = service.send_test_briefing()
    assert result.ok is True
    assert result.source == "ai"
    assert result.text == "All quiet, price steady."
    assert result.telegram_sent is True
    assert result.latency_ms is not None
    assert len(notifier.messages) == 1
    assert "All quiet, price steady." in notifier.messages[0]
    # A test must neither consume the daily slot nor write history.
    assert not (tmp_path / "briefing_state.json").exists()
    assert not (tmp_path / "briefing_history.json").exists()


def test_test_briefing_no_narrative_fallback_on_failure(tmp_path: Path):
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    advisor = _advisor(failing)
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=NARRATIVE_ON)
    result = service.send_test_briefing()
    assert result.ok is False
    assert "AI request failed" in (result.reason or "")
    # Masking a real provider problem with a silent fallback would defeat
    # the point of a test.
    assert notifier.messages == []


def test_test_briefing_refusal_includes_model_excerpt(tmp_path: Path):
    """A refusal must be reported with the model's own words so a false
    positive (e.g. an over-eager refusal detector) is diagnosable."""
    advisor = _advisor(
        _handler_returning("As an AI language model, I cannot assist with that.")
    )
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=NARRATIVE_ON)
    result = service.send_test_briefing()
    assert result.ok is False
    assert "refused" in (result.reason or "")
    assert "cannot assist" in (result.reason or "")
    assert notifier.messages == []


def test_test_briefing_empty_model_content_reported_clearly(tmp_path: Path):
    """Reasoning models that exhaust max_tokens return empty content; the
    test must say so explicitly instead of reporting a 'refusal'."""
    advisor = _advisor(_handler_returning(""))
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=NARRATIVE_ON)
    result = service.send_test_briefing()
    assert result.ok is False
    assert "empty content" in (result.reason or "")
    assert "refused" not in (result.reason or "")
    assert notifier.messages == []


def test_test_briefing_telegram_failure_reported(tmp_path: Path):
    advisor = _advisor(_handler_returning("narrative text"))
    notifier = _FakeNotifier(fail_times=1)
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=NARRATIVE_ON)
    result = service.send_test_briefing()
    # The AI side worked; only Telegram delivery failed — reported distinctly.
    assert result.ok is True
    assert result.telegram_sent is False
    assert "Telegram send failed" in (result.reason or "")
    assert result.text == "narrative text"


def test_test_briefing_narrative_call_is_bounded(tmp_path: Path):
    """A manual test send must fail fast: one provider attempt with a capped
    timeout, so the web request answers before the reverse proxy cuts it
    off with a 504. Regression test for the gateway-timeout bug."""
    seen: dict[str, Any] = {}

    class _SpyProvider:
        supports_json_mode = True

        def complete(self, system_prompt, user_prompt, **kwargs):
            seen.update(kwargs)
            return ProviderResult(
                text="bounded call ok", model="spy", latency_ms=1.0, tokens_used=3
            )

    settings = AdvisorSettings(
        enabled=True,
        provider="openai",
        base_url="https://example.test/v1",
        model="test-model",
        api_key="test-key",
        timeout_seconds=120.0,
        max_retries=3,
        features={"briefing": True, "explainer": False},
    )
    advisor = Advisor(settings, provider=_SpyProvider())
    notifier = _FakeNotifier()
    service = _service(tmp_path, advisor, notifier, _snapshot(), sections=NARRATIVE_ON)
    result = service.send_test_briefing()
    assert result.ok is True
    assert result.text == "bounded call ok"
    # Test path overrides: single attempt, timeout capped — never the
    # configured 120s/3 retries, and always below the reverse-proxy limit.
    assert seen["max_retries"] == 0
    assert seen["timeout"] <= 90.0
    advisor.executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Narrative customization: focus, custom prompt, word limit, news blocks
# ---------------------------------------------------------------------------


def test_render_custom_prompt_replaces_known_placeholders():
    values = display_values(_snapshot())
    out = render_custom_prompt(
        "Pair {pair} at {price}; invested {invested}; P/L {pnl_pct}%.", values
    )
    assert "{pair}" not in out
    assert "XBTCHF" in out
    assert "50000.0" in out
    assert "480.0" in out
    assert "4.2%" in out  # (500 CHF value vs 480 invested) -> +4.2%


def test_render_custom_prompt_keeps_unknown_and_marks_missing():
    out = render_custom_prompt(
        "Next: {next_run}; keep {unknown}; missing {pnl_pct}.",
        {"trading_pair": "XBTCHF"},
    )
    assert "{unknown}" in out  # typos stay visible instead of vanishing
    assert "Next: n/a" in out
    assert "missing n/a" in out


def test_build_briefing_message_news_blocks():
    values = display_values(_snapshot())
    news = [Headline(title="Bitcoin ETF inflows rise", source="coindesk.test")]
    base_sections = {
        "market": False,
        "portfolio": False,
        "budget": False,
        "bot": False,
        "news_general": True,
        "news_pair": True,
    }
    msg = build_briefing_message(
        values=values,
        slot_label="test",
        sections=base_sections,
        news_general=news,
        news_pair=news,
    )
    assert "📰 Crypto News" in msg
    assert "🎯 XBTCHF News" in msg
    assert "- Bitcoin ETF inflows rise (coindesk.test)" in msg
    # Sections off: no news blocks even when headlines are provided.
    msg_off = build_briefing_message(
        values=values,
        slot_label="test",
        sections={**base_sections, "news_general": False, "news_pair": False},
        news_general=news,
        news_pair=news,
    )
    assert "Crypto News" not in msg_off
    assert "XBTCHF News" not in msg_off


class _FakeNewsFetcher:
    def __init__(self, headlines):
        self._headlines = list(headlines)

    def fetch_all(self):
        return list(self._headlines)


def _recording_advisor(requests: list, content: str = "Quiet market morning."):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"total_tokens": 20},
            },
        )

    return _advisor(handler)


def test_briefing_with_news_blocks_and_narrative_context(
    tmp_path: Path, morning: datetime
):
    requests: list = []
    advisor = _recording_advisor(requests)
    notifier = _FakeNotifier()
    headlines = [
        Headline(title="Bitcoin ETF inflows rise", source="a.test"),
        Headline(title="Soybean exports jump", source="a.test"),  # not pair news
    ]
    service = BriefingService(
        advisor,
        notifier,
        lambda: _snapshot(),
        sections={"narrative": True, "news_general": True, "news_pair": True},
        narrative_focus=("market", "news"),
        news_fetcher=_FakeNewsFetcher(headlines),
        state_file=tmp_path / "briefing_state.json",
        history_file=tmp_path / "briefing_history.json",
    )

    result = service.check_and_send(now=morning)

    assert result.sent is True
    message = notifier.messages[0]
    assert "📰 Crypto News" in message
    assert "🎯 XBTCHF News" in message
    assert "Soybean exports jump" in message  # general block carries all headlines
    entry = service.load_history()[-1]
    assert entry["news_general"] == [
        "- Bitcoin ETF inflows rise (a.test)",
        "- Soybean exports jump (a.test)",
    ]
    assert entry["news_pair"] == ["- Bitcoin ETF inflows rise (a.test)"]
    # The narrative request carried the focus list and the news headlines.
    assert requests, "narrative should have been requested"
    user_msg = next(
        m["content"] for m in requests[-1]["messages"] if m["role"] == "user"
    )
    payload = json.loads(user_msg)
    assert payload["focus"] == ["market", "news"]
    assert any(
        "Bitcoin ETF inflows rise" in h for h in payload["news_headlines"]
    )
    advisor.executor.shutdown(wait=True)


def test_briefing_custom_prompt_rendered_into_request(
    tmp_path: Path, morning: datetime
):
    requests: list = []
    advisor = _recording_advisor(requests)
    notifier = _FakeNotifier()
    service = BriefingService(
        advisor,
        notifier,
        lambda: _snapshot(),
        sections={"narrative": True},
        custom_prompt="Report on {pair}: invested {invested}, {buys} buys.",
        state_file=tmp_path / "briefing_state.json",
        history_file=tmp_path / "briefing_history.json",
    )

    result = service.check_and_send(now=morning)

    assert result.sent is True
    body = json.dumps(requests[-1])
    assert "Report on XBTCHF: invested 480.0, 3 buys." in body
    advisor.executor.shutdown(wait=True)


def test_briefing_word_limit_reaches_system_prompt(
    tmp_path: Path, morning: datetime
):
    requests: list = []
    advisor = _recording_advisor(requests)
    notifier = _FakeNotifier()
    service = BriefingService(
        advisor,
        notifier,
        lambda: _snapshot(),
        sections={"narrative": True},
        narrative_max_words=200,
        state_file=tmp_path / "briefing_state.json",
        history_file=tmp_path / "briefing_history.json",
    )

    service.check_and_send(now=morning)

    body = json.dumps(requests[-1])
    assert "under 200 words" in body
    advisor.executor.shutdown(wait=True)
