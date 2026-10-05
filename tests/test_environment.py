"""Environment and trading-mode safety tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.api_client import KrakenAPI, LiveOrderBlockedError
from bot.config import Config
from bot.core import KrakenDCA
from bot.demo import is_demo_mode
from bot.state import BotState
from bot.store import TransactionStore


def test_kraken_api_refuses_order_in_staging(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    api = KrakenAPI("key", "secret")
    with pytest.raises(LiveOrderBlockedError, match="Live order refused"):
        api.place_market_order("XBTCHF", "0.0001")


def test_kraken_api_refuses_order_in_development(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    api = KrakenAPI("key", "secret")
    with pytest.raises(LiveOrderBlockedError, match="Live order refused"):
        api.place_market_order("XBTCHF", "0.0001")


def test_kraken_api_allows_order_when_app_env_unset(monkeypatch):
    """Unset APP_ENV preserves the legacy production default."""
    monkeypatch.delenv("APP_ENV", raising=False)
    api = KrakenAPI("key", "secret")
    api._api_request = lambda *args, **kwargs: {"txid": ["X"]}  # type: ignore[method-assign]
    result = api.place_market_order("XBTCHF", "0.0001")
    assert result == {"txid": ["X"]}


def test_kraken_api_allows_order_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    api = KrakenAPI("key", "secret")
    # Mock the underlying request to avoid network.
    api._api_request = lambda *args, **kwargs: {"txid": ["X"]}  # type: ignore[method-assign]
    result = api.place_market_order("XBTCHF", "0.0001")
    assert result == {"txid": ["X"]}


def test_is_demo_mode_env_staging_forces_demo(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "staging")
    assert is_demo_mode() is True


def test_is_demo_mode_development_forces_demo(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    assert is_demo_mode() is True


def test_is_demo_mode_production_is_not_demo(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    assert is_demo_mode() is False


def test_is_demo_mode_flag_overrides(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "true")
    assert is_demo_mode() is True


def test_kraken_dca_staging_without_demo_raises(tmp_path: Path, monkeypatch):
    """Staging must not start unless demo mode is active."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8}'
    )
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("KRAKEN_API_KEY", "test-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "test-secret")
    config = Config(config_path=config_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    with pytest.raises(RuntimeError, match="APP_ENV=staging requires DEMO_MODE=true"):
        KrakenDCA(config=config, store=store, state=state)


def test_config_defaults_to_config_path_env_var(tmp_path: Path, monkeypatch):
    """Config() without arguments reads CONFIG_PATH from the environment."""
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8}'
    )
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("KRAKEN_API_KEY", "test-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "test-secret")
    config = Config()
    assert config.config_path == config_path
    assert config.trading_pair == "XBTCHF"


def test_config_defaults_to_config_json_when_env_var_missing(tmp_path: Path, monkeypatch):
    """Config() without arguments falls back to config.json when CONFIG_PATH is not set."""
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.setenv("KRAKEN_API_KEY", "test-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "test-secret")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8}'
    )
    config = Config()
    assert config.config_path == Path("config.json")
    assert config.config_path.resolve() == (tmp_path / "config.json").resolve()
    assert config.trading_pair == "XBTCHF"
