"""Tests for portfolio and performance calculations."""

from __future__ import annotations

from bot.config import Config
from bot.core import KrakenDCA
from bot.demo import DemoKrakenAPI
from bot.state import BotState
from bot.store import TransactionStore


def test_average_purchase_price(temp_dir):
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    store.add_transaction("XBTCHF", 0.0001, 50000.0)
    store.add_transaction("XBTCHF", 0.0001, 60000.0)
    total_amount, avg_price, _, total_spent = store.get_statistics("XBTCHF")
    assert avg_price == 55000.0
    assert total_spent == 11.0
    assert total_amount == 0.0002


def test_portfolio_excludes_non_bot_assets(temp_dir):
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    store.add_transaction("XBTCHF", 0.0001, 50000.0)
    # Portfolio only considers bot-owned transactions; exchange balance is not included.
    assert store.get_transaction_count("XBTCHF") == 1
    assert store.get_transaction_count("ETHCHF") == 0


def test_duplicate_order_protection_by_order_id(temp_dir):
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    first = store.add_transaction("XBTCHF", 0.0001, 50000.0, order_id="DEMO-ORDER-0001")
    second = store.add_transaction("XBTCHF", 0.0001, 50000.0, order_id="DEMO-ORDER-0001")
    # Duplicate order ID returns the existing transaction without double-counting.
    assert first.id == second.id
    assert store.get_transaction_count() == 1


def test_demo_bot_does_not_place_live_orders(temp_dir, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")  # Even if set, demo API is used
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    config_path = temp_dir / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000}'
    )
    config = Config(config_path=config_path)
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    state = BotState(filepath=temp_dir / "state.json", persist=False)
    bot = KrakenDCA(config=config, store=store, state=state)
    assert isinstance(bot.api, DemoKrakenAPI)
