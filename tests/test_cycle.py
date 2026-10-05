"""Tests for recurring cycle scheduling and missed-cycle recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import bot.core as core_module
from bot.config import Config
from bot.core import CYCLE_GRACE_HOURS, KrakenDCA
from bot.state import BotState
from bot.store import TransactionStore
from bot.utils import add_months, now_tz


class _FakeAPI:
    """Stand-in Kraken API that avoids network calls in scheduling tests."""

    def __init__(self, price: float = 100000.0, balance: dict[str, float] | None = None):
        self.price = price
        self.balance = balance or {"ZCHF": 100000.0, "CHF": 100000.0}

    def get_ticker(self, pair: str) -> float:
        return self.price

    def get_balance(self) -> dict[str, float]:
        return self.balance

    def test_connection(self) -> bool:
        return True


def _make_bot(tmp_path: Path, monkeypatch, **config_overrides: Any) -> KrakenDCA:
    """Build a bot with a fake API, empty store, and non-persistent state."""
    config_data: dict[str, Any] = {
        "trading_pair": "XBTCHF",
        "deposit_day": 24,
        "crypto_amount": 0.0001,
        "dip_threshold_percent": 5.0,
        "poll_interval_seconds": 300,
        "buy_hour": 8,
        "dip_buy_cooldown_hours": 2.0,
        "max_price": 200000,
        "max_monthly_amount": 10000,
    }
    config_data.update(config_overrides)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    config = Config(config_path=config_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    bot = KrakenDCA(config=config, store=store, state=state)
    bot.store.clear()
    bot.api = _FakeAPI()  # type: ignore[assignment]
    return bot


def test_add_months_helper():
    assert add_months(datetime(2026, 6, 24, 8, 0), 1) == datetime(2026, 7, 24, 8, 0)
    assert add_months(datetime(2026, 1, 31, 8, 0), 1) == datetime(2026, 2, 28, 8, 0)
    assert add_months(datetime(2026, 12, 24, 8, 0), 1) == datetime(2027, 1, 24, 8, 0)


def test_next_cycle_after_normal_cycle(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, deposit_day=24, buy_hour=8)
    last_cycle = datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc)
    next_cycle = bot._next_cycle_after(last_cycle)
    assert next_cycle == datetime(2026, 7, 24, 8, 0, 0, tzinfo=timezone.utc)


def test_next_cycle_after_late_buy(tmp_path: Path, monkeypatch):
    # If the last recorded buy was late in the cycle, the next cycle should still
    # be the following month's deposit day, not the same day.
    bot = _make_bot(tmp_path, monkeypatch, deposit_day=24, buy_hour=8)
    last_cycle = datetime(2026, 6, 24, 10, 30, 0, tzinfo=timezone.utc)
    next_cycle = bot._next_cycle_after(last_cycle)
    assert next_cycle == datetime(2026, 7, 24, 8, 0, 0, tzinfo=timezone.utc)


def test_next_cycle_after_transaction_mid_cycle(tmp_path: Path, monkeypatch):
    # A transaction in the middle of a cycle should still point the next expected
    # cycle to the upcoming deposit day.
    bot = _make_bot(tmp_path, monkeypatch, deposit_day=24, buy_hour=8)
    last_cycle = datetime(2026, 6, 16, 15, 0, 0, tzinfo=timezone.utc)
    next_cycle = bot._next_cycle_after(last_cycle)
    assert next_cycle == datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc)


def test_calculate_next_buy_uses_last_cycle_at(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    tz = now_tz().tzinfo
    last_cycle = datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    bot.state.last_cycle_at = last_cycle

    # Today is July 10: the next cycle should be July 24, roughly two weeks away.
    fixed_now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert remaining > 0
    assert remaining >= 13 * 24  # at least 13 days until July 24
    assert next_buy > fixed_now
    assert next_buy < datetime(2026, 7, 24, 8, 0, 0, tzinfo=tz)
    assert max_buys > 0


def test_calculate_next_buy_catches_missed_cycle_within_grace(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    tz = now_tz().tzinfo
    last_cycle = datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    bot.state.last_cycle_at = last_cycle

    # Bot missed July 24; today is July 25, still within the 48-hour grace window.
    fixed_now = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert next_buy == fixed_now
    assert hours == 0.0
    assert remaining == 0
    assert max_buys > 0


def test_calculate_next_buy_skips_missed_cycle_outside_grace(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    tz = now_tz().tzinfo
    last_cycle = datetime(2026, 6, 24, 8, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    bot.state.last_cycle_at = last_cycle

    # Bot missed July 24 by more than the grace window; should schedule August 24.
    fixed_now = datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    # remaining_hours is the time until the period end (August 24), not the first buy.
    assert remaining >= 27 * 24  # at least 27 days until August 24
    assert next_buy > fixed_now
    assert next_buy < datetime(2026, 8, 24, 8, 0, 0, tzinfo=tz)
    assert max_buys > 0


def test_calculate_next_buy_without_last_cycle_uses_calendar(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    tz = now_tz().tzinfo
    bot.state.last_cycle_at = None

    # Today is July 25, no history: should point to the next deposit day (Aug 24).
    fixed_now = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert remaining >= 29 * 24  # at least 29 days until August 24
    assert next_buy > fixed_now
    assert next_buy < datetime(2026, 8, 24, 8, 0, 0, tzinfo=tz)
    assert max_buys > 0


def test_seed_last_cycle_from_transactions(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
    assert bot.state.last_cycle_at is None
    bot._seed_last_cycle_from_transactions()
    assert bot.state.last_cycle_at is not None


def test_execute_buy_records_last_cycle_at(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    tz = now_tz().tzinfo
    cycle_time = datetime(2026, 7, 24, 8, 0, 0, tzinfo=timezone.utc).astimezone(tz)
    fixed_now = cycle_time + timedelta(minutes=5)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    assert bot.state.last_cycle_at is None
    bot.execute_buy(strategy="scheduled", cycle_time=cycle_time)
    assert bot.state.last_cycle_at == cycle_time
    assert bot.store.get_transaction_count("XBTCHF") == 1


def test_execute_buy_manual_does_not_update_last_cycle_at(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    tz = now_tz().tzinfo
    fixed_now = datetime(2026, 7, 24, 8, 5, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    bot.execute_buy(strategy="manual")
    assert bot.state.last_cycle_at is None
    assert bot.store.get_transaction_count("XBTCHF") == 1


def test_cycle_grace_constant_is_positive():
    assert CYCLE_GRACE_HOURS > 0


def _make_config(tmp_path: Path, monkeypatch, **config_overrides: Any) -> Config:
    """Build a Config object for validation tests."""
    config_data: dict[str, Any] = {
        "trading_pair": "XBTCHF",
        "deposit_day": 24,
        "crypto_amount": 0.0001,
        "dip_threshold_percent": 5.0,
        "poll_interval_seconds": 300,
        "buy_hour": 8,
        "dip_buy_cooldown_hours": 2.0,
        "max_price": 200000,
        "max_monthly_amount": 10000,
    }
    config_data.update(config_overrides)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    return Config(config_path=config_path)


def test_lump_sum_requires_end_date(tmp_path: Path, monkeypatch):
    with pytest.raises(Exception, match="dca_end_date is required"):
        _make_config(tmp_path, monkeypatch, mode="lump_sum")


def test_lump_sum_rejects_past_end_date(tmp_path: Path, monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    with pytest.raises(Exception, match="dca_end_date must be in the future"):
        _make_config(tmp_path, monkeypatch, mode="lump_sum", dca_end_date=past)


def test_lump_sum_rejects_today_end_date(tmp_path: Path, monkeypatch):
    # A date string without time resolves to midnight today, which is in the past
    # relative to the current moment and must be rejected.
    today = datetime.now(timezone.utc).date().isoformat()
    with pytest.raises(Exception, match="dca_end_date must be in the future"):
        _make_config(tmp_path, monkeypatch, mode="lump_sum", dca_end_date=today)


def test_lump_sum_valid_future_end_date(tmp_path: Path, monkeypatch):
    future = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    bot = _make_bot(tmp_path, monkeypatch, mode="lump_sum", dca_end_date=future)
    assert bot.config.mode == "lump_sum"
    assert bot.config.dca_end_date is not None
    assert bot.config.dca_end_date > datetime.now(timezone.utc)
    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert max_buys > 0
    assert remaining > 0


def test_lump_sum_end_date_timezone_aware(tmp_path: Path, monkeypatch):
    tz = now_tz().tzinfo
    future = (datetime.now(tz) + timedelta(days=30)).isoformat()
    bot = _make_bot(tmp_path, monkeypatch, mode="lump_sum", dca_end_date=future)
    assert bot.config.dca_end_date is not None
    assert bot.config.dca_end_date.tzinfo is not None
    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert max_buys > 0
    assert remaining > 0


def test_lump_sum_end_date_timezone_naive(tmp_path: Path, monkeypatch):
    # Naive datetimes are interpreted as system-local time by datetime.fromisoformat().astimezone().
    future = (datetime.now() + timedelta(days=30)).replace(microsecond=0).isoformat()
    bot = _make_bot(tmp_path, monkeypatch, mode="lump_sum", dca_end_date=future)
    assert bot.config.dca_end_date is not None
    assert bot.config.dca_end_date.tzinfo is not None
    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert max_buys > 0
    assert remaining > 0


def test_lump_sum_across_dst_transition(tmp_path: Path, monkeypatch):
    tz = now_tz().tzinfo
    # Pick a future date that is likely to span a DST boundary in Europe/Zurich.
    future = datetime(2027, 4, 1, 8, 0, 0, tzinfo=tz)
    if future <= datetime.now(timezone.utc).astimezone(tz):
        future = future.replace(year=future.year + 1)
    bot = _make_bot(tmp_path, monkeypatch, mode="lump_sum", dca_end_date=future.isoformat())
    assert bot.config.dca_end_date is not None
    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert max_buys > 0
    assert remaining > 0


def test_calculate_next_buy_lump_sum_end_date_reached(tmp_path: Path, monkeypatch):
    tz = now_tz().tzinfo
    end_date = datetime.now(tz) - timedelta(days=1)
    # Config validation rejects past dates, so seed an already-loaded bot directly.
    bot = _make_bot(
        tmp_path,
        monkeypatch,
        mode="lump_sum",
        dca_end_date=(datetime.now(tz) + timedelta(days=30)).date().isoformat(),
    )
    bot.config.dca_end_date = end_date
    fixed_now = datetime.now(tz)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert next_buy == end_date
    assert hours == 0.0
    assert remaining == 0
    assert max_buys == 0


def test_calculate_next_buy_uninitialized_next_cycle(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.state.last_cycle_at = None
    fixed_now = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc).astimezone(now_tz().tzinfo)
    monkeypatch.setattr(core_module, "now_tz", lambda: fixed_now)

    next_buy, hours, remaining, max_buys = bot.calculate_next_buy()
    assert remaining >= 29 * 24
    assert next_buy > fixed_now
    assert max_buys > 0


def test_require_end_date_raises_when_missing(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, mode="lump_sum", dca_end_date=(datetime.now() + timedelta(days=30)).date().isoformat())
    bot.config.dca_end_date = None
    with pytest.raises(RuntimeError, match="dca_end_date is required"):
        bot._require_end_date()


def test_bot_state_respects_paused_override(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.overrides.set_paused(True, reason="maintenance")
    assert bot.overrides.paused is True
    assert bot._check_runtime_overrides() is False
    assert bot.state.paused is True
    assert bot.state.status == "paused"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
