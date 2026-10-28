"""Tests for the per-tier monthly budget override (issue #20).

A dynamic tier can opt out of the monthly budget cap entirely: buys matched
by that tier ignore the budget limit, do not count toward ``get_monthly_spent``
and are flagged on the transaction (displayed as "Dynamic -20% · budget
override"). The manual "buy anyway" one-shot override is unaffected: those
buys still count toward the monthly budget.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from bot.config import Config
from bot.core import KrakenDCA
from bot.order_execution import OrderAttemptStore
from bot.state import BotState
from bot.store import Transaction, TransactionStore


# -----------------------------------------------------------------------------
# Config model
# -----------------------------------------------------------------------------


def _write_config(tmp_path: Path, tiers: list[dict[str, Any]]) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "trading_pair": "XBTCHF",
                "deposit_day": 24,
                "crypto_amount": 0.0001,
                "dip_threshold_percent": 5.0,
                "poll_interval_seconds": 300,
                "buy_hour": 8,
                "dip_buy_cooldown_hours": 2.0,
                "max_price": 200000,
                "max_monthly_amount": 10000,
                "dynamic_dca": {
                    "enabled": True,
                    "reference": "last_buy",
                    "cooldown_hours": 24.0,
                    "tiers": tiers,
                },
            }
        )
    )
    return config_path


def _make_config(tmp_path: Path, tiers: list[dict[str, Any]], monkeypatch) -> Config:
    config_path = _write_config(tmp_path, tiers)
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    return Config(config_path=config_path)


def test_tier_override_flag_parses(tmp_path: Path, monkeypatch):
    config = _make_config(
        tmp_path,
        [{"threshold_percent": -20.0, "amount": 0.0003, "enabled": True,
          "override_monthly_budget": True}],
        monkeypatch,
    )
    assert config.dynamic_dca_tiers[0].override_monthly_budget is True


def test_tier_override_defaults_false_for_legacy_configs(tmp_path: Path, monkeypatch):
    # Configs written before the feature must keep working unchanged.
    config = _make_config(
        tmp_path,
        [{"threshold_percent": -20.0, "amount": 0.0003, "enabled": True}],
        monkeypatch,
    )
    assert config.dynamic_dca_tiers[0].override_monthly_budget is False


def test_tier_override_roundtrips_through_to_dict(tmp_path: Path, monkeypatch):
    config = _make_config(
        tmp_path,
        [{"threshold_percent": -20.0, "amount": 0.0003, "enabled": True,
          "override_monthly_budget": True}],
        monkeypatch,
    )
    data = config.to_dict()
    assert data["dynamic_dca"]["tiers"][0]["override_monthly_budget"] is True


# -----------------------------------------------------------------------------
# Bookkeeping: get_monthly_spent excludes override transactions
# -----------------------------------------------------------------------------


def test_get_monthly_spent_excludes_override_transactions(tmp_path: Path):
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    store.add_transaction("XBTCHF", 0.0001, 50000.0)  # 5.0, counts
    store.add_transaction(
        "XBTCHF", 0.0003, 40000.0, strategy="dynamic",
        dynamic_tier=-20.0, budget_override=True,
    )  # 12.0, does not count
    spent = store.get_monthly_spent("XBTCHF", deposit_day=1, buy_hour=0)
    assert spent == pytest.approx(5.0)


def test_transaction_budget_override_persistence(tmp_path: Path):
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    store.add_transaction(
        "XBTCHF", 0.0003, 40000.0, strategy="dynamic",
        dynamic_tier=-20.0, budget_override=True,
    )
    reloaded = TransactionStore(filepath=tmp_path / "transactions.json")
    assert reloaded.transactions[-1].budget_override is True


def test_legacy_transaction_defaults_to_no_override():
    txn = Transaction.from_dict(
        {
            "date": "2026-10-01T00:00:00+00:00",
            "trading_pair": "XBTCHF",
            "amount": 0.0001,
            "price": 50000.0,
        }
    )
    assert txn.budget_override is False


# -----------------------------------------------------------------------------
# Buy logic: tier override bypasses both monthly budget checks
# -----------------------------------------------------------------------------


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class _FakeAPI:
    def __init__(self, price: float, balance: dict[str, float] | None = None):
        self.price = price
        self.balance = balance or {"ZCHF": 100000.0, "CHF": 100000.0}

    def get_ticker(self, pair: str) -> float:
        return self.price

    def get_balance(self) -> dict[str, float]:
        return dict(self.balance)


def _capture_alerts(monkeypatch) -> list[str]:
    alerts: list[str] = []
    monkeypatch.setattr(
        "bot.core.create_alert",
        lambda message, severity="warning", source="bot": alerts.append(message),
    )
    return alerts


def _make_bot(
    tmp_path: Path,
    monkeypatch,
    *,
    max_monthly_amount: float,
    override: bool,
    live: bool = False,
    balance: dict[str, float] | None = None,
) -> KrakenDCA:
    tiers = [
        {"threshold_percent": -2.0, "amount": 0.0001, "enabled": True},
        {"threshold_percent": -20.0, "amount": 0.0003, "enabled": True,
         "override_monthly_budget": override},
    ]
    config_path = _write_config(tmp_path, tiers)
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true" if live else "false")
    monkeypatch.setenv("DEMO_MODE", "false" if live else "true")
    if live:
        monkeypatch.setenv("APP_ENV", "production")
    config = Config(config_path=config_path)
    config.max_monthly_amount = max_monthly_amount
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    bot = KrakenDCA(config=config, store=store, state=state)
    bot.store.clear()
    api = _FakeAPI(price=80000.0, balance=balance)
    bot.api = cast(Any, api)
    bot.order_executor.api = cast(Any, api)
    bot.notifier = cast(Any, _FakeNotifier())
    if live:
        bot.order_executor.attempt_store = OrderAttemptStore(
            filepath=tmp_path / "order_attempts.json"
        )
    # Reference (last buy) at 110'000; current 80'000 is -27%: the -20% tier
    # applies (crash protection). Its cost (0.0003 * 80000 = 24) always
    # exceeds the tiny monthly budget below.
    bot.store.add_transaction("XBTCHF", 0.0001, 110000.0, strategy="scheduled")
    return bot


def test_override_tier_buys_over_exhausted_budget(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0, override=True)
    alerts = _capture_alerts(monkeypatch)
    before = bot.store.get_transaction_count("XBTCHF")

    bot.execute_buy(strategy="dynamic")

    assert bot.store.get_transaction_count("XBTCHF") == before + 1
    assert not alerts  # no skip alert: the buy went through
    txn = bot.store.get_transactions("XBTCHF")[-1]
    assert txn.strategy == "dynamic"
    assert txn.dynamic_tier == -20.0
    assert txn.budget_override is True


def test_override_buy_does_not_count_toward_monthly_spent(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0, override=True)
    bot.execute_buy(strategy="dynamic")
    # Only the seeded reference buy (0.0001 * 110000 = 11) counts.
    spent = bot.store.get_monthly_spent(
        "XBTCHF", bot.config.deposit_day, bot.config.buy_hour
    )
    assert spent == pytest.approx(11.0)


def test_tier_without_override_still_respects_budget(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0, override=False)
    alerts = _capture_alerts(monkeypatch)
    before = bot.store.get_transaction_count("XBTCHF")

    bot.execute_buy(strategy="dynamic")

    assert bot.store.get_transaction_count("XBTCHF") == before  # skipped
    assert any("exceed limit" in a for a in alerts)


def test_scheduled_buy_still_blocked_even_when_override_tier_exists(
    tmp_path: Path, monkeypatch
):
    # The override applies only to dynamic buys matched by that tier.
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0, override=True)
    alerts = _capture_alerts(monkeypatch)
    before = bot.store.get_transaction_count("XBTCHF")

    bot.execute_buy(strategy="scheduled")

    assert bot.store.get_transaction_count("XBTCHF") == before
    assert any("exceed limit" in a for a in alerts)


def test_cooldown_blocks_second_override_buy(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0, override=True)
    bot.execute_buy(strategy="dynamic")
    assert bot._dynamic_cooldown_ok() is False  # loop will not re-trigger


def test_override_buy_telegram_mentions_override(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0, override=True)
    notifier = cast(Any, bot.notifier)
    monkeypatch.setattr(sys, "stdout", StringIO())
    bot.execute_buy(strategy="dynamic")
    assert any("Budget override" in m for m in notifier.messages)


def test_live_override_buy_with_insufficient_balance_skips(tmp_path: Path, monkeypatch):
    # The override bypasses the monthly budget cap, NOT the balance pre-check:
    # an order the account cannot fill must never reach Kraken.
    bot = _make_bot(
        tmp_path,
        monkeypatch,
        max_monthly_amount=1.0,
        override=True,
        live=True,
        balance={"ZCHF": 0.0, "CHF": 0.0},
    )
    alerts = _capture_alerts(monkeypatch)
    monkeypatch.setattr(sys, "stdout", StringIO())
    before = bot.store.get_transaction_count("XBTCHF")

    bot.execute_buy(strategy="dynamic")

    assert bot.store.get_transaction_count("XBTCHF") == before
    skip = [a for a in alerts if "insufficient" in a]
    assert skip
    assert "budget override" in skip[0]


# -----------------------------------------------------------------------------
# Web API roundtrip
# -----------------------------------------------------------------------------


def test_settings_api_roundtrips_tier_override(tmp_path: Path, monkeypatch):
    from web.schemas import DynamicTierUpdate

    tier = DynamicTierUpdate(
        threshold_percent=-20.0, amount=0.0003, enabled=True,
        override_monthly_budget=True,
    )
    assert tier.override_monthly_budget is True
    # Payloads from older UIs without the field keep working.
    legacy = DynamicTierUpdate(
        threshold_percent=-20.0, amount=0.0003, enabled=True,
    )
    assert legacy.override_monthly_budget is False
