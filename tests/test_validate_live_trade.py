"""Tests for scripts/validate_live_trade.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import validate_live_trade


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
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
            }
        )
    )
    return config_path


def _set_valid_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", "live-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "live-secret")


def test_check_environment_rejects_staging(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(SystemExit):
        validate_live_trade._check_environment()


def test_check_environment_rejects_demo_mode(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(SystemExit):
        validate_live_trade._check_environment()


def test_check_environment_rejects_live_disabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    with pytest.raises(SystemExit):
        validate_live_trade._check_environment()


def test_main_rejects_amount_below_minimum(tmp_config: Path, monkeypatch):
    _set_valid_env(monkeypatch)
    with pytest.raises(SystemExit):
        validate_live_trade.main(["--config", str(tmp_config), "--amount", "1", "--yes"])


def test_main_rejects_wrong_confirmation(tmp_config: Path, monkeypatch):
    _set_valid_env(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "wrong phrase")
    with pytest.raises(SystemExit):
        validate_live_trade.main(["--config", str(tmp_config), "--amount", "10"])


def test_main_succeeds_with_yes(tmp_config: Path, monkeypatch, tmp_path: Path):
    _set_valid_env(monkeypatch)

    fake_api = MagicMock()
    fake_api.get_balance.return_value = {"ZCHF": 1000.0, "CHF": 1000.0}
    fake_api.get_ticker.return_value = 50000.0

    fake_attempt = MagicMock()
    fake_attempt.attempt_id = "oa-123"
    fake_attempt.state = "CONFIRMED"
    fake_attempt.kraken_ref = "KRAKEN-REF-1"
    fake_attempt.final_outcome = None

    fake_executor = MagicMock()
    fake_executor.submit_buy.return_value = fake_attempt

    fake_bot = MagicMock()
    fake_bot.order_executor = fake_executor

    fake_store = MagicMock()
    fake_store.get_transaction_count.return_value = 1

    monkeypatch.setattr("scripts.validate_live_trade.KrakenAPI", lambda _k, _s: fake_api)
    monkeypatch.setattr("scripts.validate_live_trade.KrakenDCA", lambda **_: fake_bot)
    monkeypatch.setattr("scripts.validate_live_trade.TransactionStore", lambda: fake_store)

    # Redirect working directory so BotState and any unpatched stores don't
    # write into the repo root.
    monkeypatch.chdir(tmp_path)

    assert validate_live_trade.main(["--config", str(tmp_config), "--amount", "10", "--yes"]) == 0

    call = fake_executor.submit_buy.call_args
    assert call.kwargs["pair"] == "XBTCHF"
    assert call.kwargs["simulated"] is False
