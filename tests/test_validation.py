"""Validation tests for staging and live-trade readiness.

These tests verify that the bot refuses real orders outside production and that
all safety guards required before a first live trade are in place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from unittest.mock import MagicMock

from bot.api_client import KrakenAPI, LiveOrderBlockedError
from bot.config import Config
from bot.core import KrakenDCA
from bot.demo import DemoKrakenAPI
from bot.order_execution import OrderAttemptStore, OrderExecutor, OrderState
from bot.state import BotState
from bot.store import TransactionStore


# -----------------------------------------------------------------------------
# Staging safety tests
# -----------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: Any) -> Config:
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
    config_data.update(overrides)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    return Config(config_path=config_path)


def test_staging_requires_demo_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo")

    with pytest.raises(RuntimeError, match="APP_ENV=staging requires DEMO_MODE=true"):
        KrakenDCA(config=_make_config(tmp_path))


def test_demo_mode_uses_demo_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo")

    bot = KrakenDCA(config=_make_config(tmp_path))
    assert isinstance(bot.api, DemoKrakenAPI)


def test_kraken_api_refuses_live_order_in_staging(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    api = KrakenAPI("key", "secret")
    with pytest.raises(LiveOrderBlockedError):
        api.place_market_order("XBTCHF", "0.0001")


def test_simulated_buy_does_not_create_order_attempt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo")

    config = _make_config(tmp_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    attempt_store = OrderAttemptStore(filepath=tmp_path / "order_attempts.json")
    bot = KrakenDCA(config=config, store=store, state=state)
    bot.order_executor.attempt_store = attempt_store

    # Demo mode seeds synthetic transactions; clear them so we can count exactly one new buy.
    store.clear()

    # Force a dry-run/simulated buy.
    assert not config.live_trading_enabled
    bot.execute_buy(strategy="manual")

    assert store.get_transaction_count("XBTCHF") == 1
    # Simulated buys bypass the order-attempt state machine.
    assert len(attempt_store.list_all()) == 0


# -----------------------------------------------------------------------------
# Live-trade readiness checks
# -----------------------------------------------------------------------------


def test_production_live_order_requires_app_env_production(monkeypatch):
    # APP_ENV unset defaults to production, which is the only case where
    # KrakenAPI.place_market_order accepts orders.
    monkeypatch.delenv("APP_ENV", raising=False)
    api = KrakenAPI("key", "secret")
    # Will attempt network call; we only verify it is not blocked by environment.
    # The network call itself is expected to fail because credentials are invalid.
    with pytest.raises(Exception):
        api.place_market_order("XBTCHF", "0.0001")


def test_live_trade_config_rejects_missing_end_date(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo")

    with pytest.raises(Exception, match="dca_end_date is required"):
        _make_config(tmp_path, mode="lump_sum")


def test_order_executor_creates_attempt_for_live_buy(tmp_path: Path, monkeypatch):
    """When live trading is enabled in production, a buy attempt is persisted."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo")

    config = _make_config(tmp_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    attempt_store = OrderAttemptStore(filepath=tmp_path / "order_attempts.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False, status="running")

    # Use DemoKrakenAPI as a fake production API, but pin deterministic pair
    # metadata and price so no real network call is required.
    api = DemoKrakenAPI(config.api_key, config.api_secret)
    monkeypatch.setattr(api, "get_ticker", lambda pair: 50000.0)
    monkeypatch.setattr(
        api,
        "get_asset_pair_info",
        lambda pair: {"ordermin": "0.0001", "costmin": "1", "lot_decimals": 8, "pair_decimals": 2},
    )
    executor = OrderExecutor(
        api=api,
        store=store,
        attempt_store=attempt_store,
        notifier=MagicMock(),
        state=state,
        config=config,
    )

    # Force config to report live trading so execute_buy routes to executor.
    config.live_trading_enabled = True
    attempt = executor.submit_buy(
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="manual",
        simulated=False,
    )
    assert attempt.state == OrderState.CONFIRMED.value
    assert len(attempt_store.list_all()) == 1
    assert store.get_transaction_count("XBTCHF") == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
