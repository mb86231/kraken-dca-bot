"""Regression tests for the late-deposit warning (issue #22).

A deposit arriving shortly before the deposit day is spread over the hours
remaining until that day — i.e. spent almost immediately. The bot must warn
via log, Telegram notification and a dashboard alert, and stay silent when
the deposit lands with plenty of cycle time left.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import bot.alerts as alerts_mod
from bot.config import Config
from bot.core import DEPOSIT_LATE_WARNING_HOURS, KrakenDCA
from bot.state import BotState, RuntimeOverrides
from bot.store import TransactionStore


class _FakeAPI:
    def get_ticker(self, pair: str) -> float:
        return 100000.0

    def get_balance(self) -> dict[str, float]:
        return {"ZCHF": 1000.0, "CHF": 1000.0}

    def test_connection(self) -> bool:
        return True


def _make_bot(tmp_path: Path, monkeypatch) -> KrakenDCA:
    config_data: dict[str, Any] = {
        "trading_pair": "XBTCHF",
        "deposit_day": 24,
        "crypto_amount": 0.0001,
        "dip_threshold_percent": 5.0,
        "poll_interval_seconds": 300,
        "buy_hour": 8,
        "dip_buy_cooldown_hours": 2.0,
        "dynamic_dca": {"enabled": False, "tiers": []},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setattr(alerts_mod, "ALERTS_PATH", tmp_path / "alerts.json")
    config = Config(config_path=config_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    overrides = RuntimeOverrides(filepath=tmp_path / "runtime_overrides.json")
    bot = KrakenDCA(config=config, store=store, state=state, overrides=overrides)
    bot.api = cast(Any, _FakeAPI())
    return bot


def _spy_notifier(bot: KrakenDCA) -> list[str]:
    sent: list[str] = []

    def _send(message: str) -> bool:
        sent.append(message)
        return True

    # cast(Any, …): the notifier attribute is typed Notifier; a stub is fine
    # for these tests but must not trip CI's stricter mypy over tests/.
    cast(Any, bot).notifier = SimpleNamespace(send=_send)
    return sent


def test_late_deposit_warns_via_all_channels(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    sent = _spy_notifier(bot)

    bot._warn_late_deposit(remaining_hours=10.7, max_buys=7)

    assert len(sent) == 1
    assert "10.7 h before the deposit day" in sent[0]
    assert "7 buy(s)" in sent[0]
    stored = alerts_mod.load_alerts()
    assert len(stored) == 1
    assert stored[0]["severity"] == "warning"
    assert "Late deposit:" in stored[0]["message"]
    assert "max_monthly_amount" in stored[0]["message"]


def test_early_deposit_stays_silent(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    sent = _spy_notifier(bot)

    bot._warn_late_deposit(remaining_hours=DEPOSIT_LATE_WARNING_HOURS + 1, max_buys=7)

    assert sent == []
    assert alerts_mod.load_alerts() == []


def test_boundary_and_zero_buys_stay_silent(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    sent = _spy_notifier(bot)

    bot._warn_late_deposit(remaining_hours=0, max_buys=7)
    bot._warn_late_deposit(remaining_hours=10.0, max_buys=0)

    assert sent == []
    assert alerts_mod.load_alerts() == []


def test_warning_mentions_buy_interval(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch)
    sent = _spy_notifier(bot)

    bot._warn_late_deposit(remaining_hours=10.0, max_buys=5)

    assert "~every 2.0 h" in sent[0]
