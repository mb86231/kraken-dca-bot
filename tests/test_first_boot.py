"""First-boot behaviour: a missing config or missing API credentials must not be fatal.

The public quick-start path boots the container with no configuration at all.
The bot must come up (web dashboard included), create the template config,
and idle until credentials are provided via the dashboard.
"""

from __future__ import annotations

import json

import pytest

from bot.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.json"))
    return tmp_path


def _write_config(path, **overrides):
    data = {
        "trading_pair": "XXBTZUSD",
        "crypto_amount": 0.0001,
        "deposit_day": 1,
        "poll_interval_seconds": 600,
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_config_creates_template_and_continues(env):
    path = env / "config.json"
    cfg = Config(config_path=path)
    assert path.exists(), "template config must be created"
    assert cfg.configured is False
    template = json.loads(path.read_text(encoding="utf-8"))
    assert template["trading_pair"] == "XXBTZUSD"
    assert template["live_trading_enabled"] is False
    assert "configured" in cfg.to_dict()


def test_config_without_keys_is_not_fatal(env):
    path = env / "config.json"
    _write_config(path)
    cfg = Config(config_path=path)
    assert cfg.configured is False
    assert cfg.to_dict()["configured"] is False


def test_config_with_keys_is_configured(env, monkeypatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "secret")
    path = env / "config.json"
    _write_config(path)
    cfg = Config(config_path=path)
    assert cfg.configured is True


def test_invalid_trading_pair_still_raises(env):
    path = env / "config.json"
    _write_config(path, trading_pair="")
    with pytest.raises(Exception, match="trading_pair"):
        Config(config_path=path)


def test_invalid_values_still_raise_when_unconfigured(env):
    path = env / "config.json"
    _write_config(path, poll_interval_seconds=10)
    with pytest.raises(Exception, match="poll_interval"):
        Config(config_path=path)


def test_reload_picks_up_new_credentials(env, monkeypatch):
    path = env / "config.json"
    _write_config(path)
    cfg = Config(config_path=path)
    assert cfg.configured is False
    monkeypatch.setenv("KRAKEN_API_KEY", "key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "secret")
    cfg.reload()
    assert cfg.configured is True
