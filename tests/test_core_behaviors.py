"""Tests for core trading-loop behaviors: deposit detection, dynamic buy, dip buy."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from bot.config import Config
from bot.core import KrakenDCA
from bot.order_execution import OrderAttemptStore
from bot.state import BotState, RuntimeOverrides
from bot.store import TransactionStore
from bot.utils import now_tz


class _FakeAPI:
    """Controllable fake API for core-behavior tests."""

    def __init__(self, price: float = 100000.0, balance: dict[str, float] | None = None):
        self.price = price
        self.balance = balance or {"ZCHF": 100000.0, "CHF": 100000.0}
        self._next_balance: dict[str, float] | None = None

    def get_ticker(self, pair: str) -> float:
        return self.price

    def get_balance(self) -> dict[str, float]:
        return dict(self.balance)

    def test_connection(self) -> bool:
        return True

    def inject_deposit(self, amount: float) -> None:
        self.balance["ZCHF"] += amount
        self.balance["CHF"] += amount


def _make_bot(tmp_path: Path, monkeypatch, **config_overrides: Any) -> KrakenDCA:
    dynamic_overrides = {
        k: config_overrides.pop(k)
        for k in list(config_overrides.keys())
        if k.startswith("dynamic_dca_")
    }
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
        "dynamic_dca": {
            "enabled": False,
            "reference": "last_buy",
            "cooldown_hours": 2.0,
            "tiers": [
                {"threshold_percent": 10.0, "amount": 0.0, "enabled": True},
                {"threshold_percent": 5.0, "amount": 0.00005, "enabled": True},
                {"threshold_percent": -2.0, "amount": 0.0001, "enabled": True},
                {"threshold_percent": -5.0, "amount": 0.00015, "enabled": True},
                {"threshold_percent": -10.0, "amount": 0.0002, "enabled": True},
                {"threshold_percent": -20.0, "amount": 0.0003, "enabled": True},
            ],
        },
    }
    if dynamic_overrides:
        config_data["dynamic_dca"].update(
            {k.replace("dynamic_dca_", ""): v for k, v in dynamic_overrides.items()}
        )
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
    overrides = RuntimeOverrides(filepath=tmp_path / "runtime_overrides.json")
    bot = KrakenDCA(config=config, store=store, state=state, overrides=overrides)
    bot.api = cast(Any, _FakeAPI(price=100000.0))
    return bot


def _api(bot: KrakenDCA) -> Any:
    return cast(Any, bot.api)


def test_dynamic_dca_triggers_larger_buy(tmp_path: Path, monkeypatch):
    bot = _make_bot(
        tmp_path,
        monkeypatch,
        dynamic_dca_enabled=True,
        dynamic_dca_reference="last_buy",
        dynamic_dca_cooldown_hours=0,
        dynamic_dca_tiers=[
            {"threshold_percent": -10.0, "amount": 0.0003, "enabled": True},
        ],
    )
    # Seed a previous buy at a higher price so a 10% drop triggers the tier.
    bot.store.add_transaction("XBTCHF", 0.0001, 110000.0, strategy="scheduled")
    _api(bot).price = 99000.0  # ~10% below 110k

    amount = bot.resolve_buy_amount(_api(bot).price)
    assert amount == 0.0003


def test_dynamic_dca_respects_cooldown(tmp_path: Path, monkeypatch):
    tz = now_tz().tzinfo
    last_buy_time = datetime.now(tz) - timedelta(minutes=30)
    bot = _make_bot(
        tmp_path,
        monkeypatch,
        dynamic_dca_enabled=True,
        dynamic_dca_reference="last_buy",
        dynamic_dca_cooldown_hours=2.0,
        dynamic_dca_tiers=[
            {"threshold_percent": -10.0, "amount": 0.0003, "enabled": True},
        ],
    )
    bot.store.add_transaction("XBTCHF", 0.0001, 110000.0, strategy="scheduled")
    # Patch the last buy time to be recent.
    tx = bot.store.get_transactions("XBTCHF")[-1]
    tx.date = last_buy_time.isoformat()
    bot.store._save()

    _api(bot).price = 99000.0
    assert not bot._dynamic_cooldown_ok()


def test_dip_buy_triggered(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, dip_threshold_percent=5.0)
    # Seed previous buy at 100k; current price 94k is a 6% dip.
    bot.store.add_transaction("XBTCHF", 0.0001, 100000.0, strategy="scheduled")
    _api(bot).price = 94000.0

    assert bot._last_buy_time() is not None
    assert bot.resolve_buy_amount(_api(bot).price) == bot.config.crypto_amount


def test_paused_state_blocks_check_overrides(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.overrides.set_paused(True, reason="maintenance")
    assert bot._check_runtime_overrides() is False
    assert bot.state.paused is True
    assert bot.state.status == "paused"


def test_manual_cycle_override(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    bot.overrides.request_manual_cycle()
    assert bot._check_runtime_overrides() is True
    assert not bot.overrides.manual_cycle_requested


def test_deposit_detection_increases_balance(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    _api(bot).balance["ZCHF"] = 5000.0
    initial = bot.get_fiat_balance(_api(bot).get_balance())
    _api(bot).inject_deposit(2000.0)
    after = bot.get_fiat_balance(_api(bot).get_balance())
    assert after == initial + 2000.0


def test_get_fiat_currency_strips_prefixes(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, trading_pair="XXBTZUSD")
    assert bot.get_fiat_currency() == "USD"


def test_get_fiat_balance_prefers_z_prefix(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    balance = {"ZCHF": 1234.0, "CHF": 999.0}
    assert bot.get_fiat_balance(balance) == 1234.0


class _RichFakeAPI:
    """Fake API that also covers preflight and order-execution calls."""

    def __init__(self, price: float = 100000.0, balance: dict[str, float] | None = None):
        self.price = price
        self.balance = balance or {"ZCHF": 100000.0, "CHF": 100000.0}
        self.calls: list[tuple[Any, ...]] = []

    def get_ticker(self, pair: str) -> float:
        self.calls.append(("get_ticker", pair))
        return self.price

    def get_balance(self) -> dict[str, float]:
        self.calls.append(("get_balance",))
        return dict(self.balance)

    def test_connection(self) -> bool:
        self.calls.append(("test_connection",))
        return True

    def get_asset_pair_info(self, pair: str) -> dict[str, Any]:
        self.calls.append(("get_asset_pair_info", pair))
        return {
            "ordermin": "0.0001",
            "costmin": "10",
            "lot_decimals": 8,
            "pair_decimals": 2,
        }

    def place_market_order(
        self,
        pair: str,
        volume: str,
        order_type: str = "buy",
        userref: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("place_market_order", pair, volume, userref))
        return {
            "descr": {"order": f"buy {volume} {pair} @ market"},
            "txid": [f"FAKE-{pair}-{volume}"],
            "userref": userref,
        }


def _make_live_bot(
    tmp_path: Path,
    monkeypatch,
    live_trading_enabled: str = "true",
    demo_mode: str = "false",
) -> KrakenDCA:
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
        "dynamic_dca": {
            "enabled": False,
            "reference": "last_buy",
            "cooldown_hours": 2.0,
            "tiers": [
                {"threshold_percent": 10.0, "amount": 0.0, "enabled": True},
                {"threshold_percent": 5.0, "amount": 0.00005, "enabled": True},
                {"threshold_percent": -2.0, "amount": 0.0001, "enabled": True},
                {"threshold_percent": -5.0, "amount": 0.00015, "enabled": True},
                {"threshold_percent": -10.0, "amount": 0.0002, "enabled": True},
                {"threshold_percent": -20.0, "amount": 0.0003, "enabled": True},
            ],
        },
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", live_trading_enabled)
    monkeypatch.setenv("DEMO_MODE", demo_mode)
    config = Config(config_path=config_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    overrides = RuntimeOverrides(filepath=tmp_path / "runtime_overrides.json")
    bot = KrakenDCA(config=config, store=store, state=state, overrides=overrides)
    bot.order_executor.attempt_store = OrderAttemptStore(filepath=tmp_path / "order_attempts.json")
    bot.api = cast(Any, _RichFakeAPI(price=100000.0))
    return bot


def test_execute_buy_live_submits_order_without_preflight(
    tmp_path: Path, monkeypatch
):
    """Live orders should not require a preflight result anymore."""
    bot = _make_live_bot(
        tmp_path,
        monkeypatch,
        live_trading_enabled="true",
        demo_mode="false",
    )
    api = _RichFakeAPI()
    bot.api = cast(Any, api)
    bot.order_executor.api = cast(Any, api)
    bot.state.update(status="running")

    # Redirect stdout to avoid Windows cp1252 unicode encoding errors from
    # colour/symbol output during this test.
    monkeypatch.setattr(sys, "stdout", StringIO())

    bot.execute_buy(strategy="scheduled")

    assert any(call[0] == "place_market_order" for call in api.calls)
    assert bot.store.get_transaction_count(bot.config.trading_pair) == 1


# -----------------------------------------------------------------------------
# Budget guard: skipped buys must be visible, manual buys may exceed once
# -----------------------------------------------------------------------------


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.enabled = True

    def send(self, message: str) -> None:
        self.messages.append(message)


def _capture_alerts(monkeypatch) -> list[str]:
    alerts: list[str] = []
    monkeypatch.setattr(
        "bot.core.create_alert",
        lambda message, severity="warning", source="bot": alerts.append(message),
    )
    return alerts


def test_manual_buy_over_budget_skipped_with_feedback(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0)
    alerts = _capture_alerts(monkeypatch)
    notifier = _FakeNotifier()
    bot.notifier = cast(Any, notifier)
    before = bot.store.get_transaction_count("XBTCHF")

    bot.execute_buy(strategy="manual")

    assert bot.store.get_transaction_count("XBTCHF") == before  # nothing bought
    assert any("exceed limit" in a for a in alerts)  # visible in the dashboard
    assert any("skipped" in m for m in notifier.messages)  # pushed to Telegram


def test_manual_buy_over_budget_allowed_with_one_shot_override(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0)
    alerts = _capture_alerts(monkeypatch)
    notifier = _FakeNotifier()
    bot.notifier = cast(Any, notifier)
    before = bot.store.get_transaction_count("XBTCHF")

    bot.overrides.request_manual_cycle(over_budget=True)
    bot.execute_buy(strategy="manual")

    assert bot.store.get_transaction_count("XBTCHF") == before + 1  # buy happened
    assert not alerts  # no skip alert
    assert not any("skipped" in m for m in notifier.messages)
    assert bot.overrides.manual_buy_over_budget is False  # one-shot: consumed


def test_scheduled_buy_never_exceeds_budget(tmp_path: Path, monkeypatch):
    # The one-shot override only applies to operator-approved manual buys.
    bot = _make_bot(tmp_path, monkeypatch, max_monthly_amount=1.0)
    alerts = _capture_alerts(monkeypatch)
    before = bot.store.get_transaction_count("XBTCHF")

    bot.overrides.request_manual_cycle(over_budget=True)
    bot.execute_buy(strategy="scheduled")

    assert bot.store.get_transaction_count("XBTCHF") == before
    assert any("exceed limit" in a for a in alerts)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
