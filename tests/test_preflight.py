"""Tests for the production preflight system."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from bot.config import Config
from bot.order_execution import OrderAttempt, OrderAttemptStore, OrderState
from bot.preflight import (
    DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    CheckStatus,
    PreflightResult,
    ProductionPreflight,
    latest_preflight_is_valid,
    write_preflight_result,
)
from bot.state import BotState, RuntimeOverrides
from bot.utils import utc_now


@pytest.fixture
def production_env(monkeypatch):
    """Set environment variables for a valid production preflight."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", "AK_TEST_12345")
    monkeypatch.setenv("KRAKEN_API_SECRET", "AS_TEST_67890")
    monkeypatch.setenv("SESSION_SECRET", "a" * 32)
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "$2b$12$validhash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.delenv("DISABLE_RATE_LIMIT", raising=False)
    monkeypatch.setenv("OIDC_ENABLED", "false")


@pytest.fixture
def tmp_config(tmp_path, production_env):
    """Create a Config backed by a temporary config.json."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "mode": "recurring",
                "trading_pair": "XBTCHF",
                "deposit_day": 15,
                "crypto_amount": 0.0002,
                "dip_threshold_percent": 5.0,
                "poll_interval_seconds": 600,
                "buy_hour": 8,
                "dip_buy_cooldown_hours": 24.0,
            }
        ),
        encoding="utf-8",
    )
    return Config(config_path=config_path)


class _FakeKrakenAPI:
    """Fake Kraken API for preflight tests."""

    def __init__(
        self,
        pair_info: dict[str, Any] | None = None,
        price: float = 60000.0,
        balance: dict[str, float] | None = None,
        auth_ok: bool = True,
    ):
        self.pair_info = pair_info or {
            "ordermin": "0.0001",
            "costmin": "10",
            "lot_decimals": 8,
            "pair_decimals": 2,
        }
        self.price = price
        self.balance = balance or {"ZCHF": 10000.0, "CHF": 10000.0}
        self.auth_ok = auth_ok
        self.calls: list[str] = []

    def test_connection(self) -> bool:
        self.calls.append("test_connection")
        if not self.auth_ok:
            raise Exception("Invalid API credentials")
        return True

    def get_asset_pair_info(self, pair: str) -> dict[str, Any]:
        self.calls.append("get_asset_pair_info")
        return dict(self.pair_info)

    def get_ticker(self, pair: str) -> float:
        self.calls.append("get_ticker")
        return self.price

    def get_balance(self) -> dict[str, float]:
        self.calls.append("get_balance")
        return dict(self.balance)

    def place_market_order(self, pair: str, volume: str, order_type: str = "buy", userref: int | None = None) -> dict[str, Any]:
        # Allow this fake to be used for order-execution assertions in preflight tests.
        return {
            "descr": {"order": f"buy {volume} {pair} @ market"},
            "txid": [f"FAKE-{pair}-{volume}"],
            "userref": userref,
        }


def _running_state() -> BotState:
    state = BotState(persist=False, status="running")
    state.next_cycle_at = utc_now() + timedelta(hours=1)
    return state


def _make_preflight(
    tmp_config: Config,
    tmp_path: Path,
    api: Any = None,
    state: BotState | None = None,
    overrides: RuntimeOverrides | None = None,
    attempt_store: OrderAttemptStore | None = None,
    notifier: Any = None,
    disabled_checks: list[str] | None = None,
) -> ProductionPreflight:
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    now = utc_now()
    (data_dir / "heartbeat.json").write_text(
        json.dumps({"timestamp": now.isoformat(), "status": "running"}),
        encoding="utf-8",
    )

    return ProductionPreflight(
        config=tmp_config,
        api=api,
        state=state or _running_state(),
        overrides=overrides or RuntimeOverrides(filepath=data_dir / "runtime_overrides.json"),
        attempt_store=attempt_store or OrderAttemptStore(filepath=data_dir / "order_attempts.json"),
        notifier=notifier,
        data_dir=data_dir,
        backup_dir=backup_dir,
        disabled_checks=disabled_checks,
    )


def _check_names(result: PreflightResult) -> dict[str, str]:
    return {c.name: c.status for c in result.checks}


def test_valid_production_preflight(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    # A valid production preflight may still WARN on items Kraken cannot verify
    # automatically (key permissions) or optional integrations (Telegram).
    assert result.overall in (CheckStatus.PASS, CheckStatus.WARN)
    assert result.can_place_live_orders is True
    assert not any(c.status == CheckStatus.FAIL for c in result.checks)
    names = _check_names(result)
    assert names["app_env_production"] == CheckStatus.PASS
    assert names["demo_mode_disabled"] == CheckStatus.PASS
    assert names["kraken_credentials_present"] == CheckStatus.PASS
    assert names["kraken_authentication"] == CheckStatus.PASS
    assert names["volume_above_minimum"] == CheckStatus.PASS
    assert names["balance_sufficient"] == CheckStatus.PASS


def test_demo_mode_enabled(tmp_config, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    api = _FakeKrakenAPI()
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    assert result.overall == CheckStatus.FAIL
    assert _check_names(result)["demo_mode_disabled"] == CheckStatus.FAIL


def test_missing_credentials(tmp_path, production_env):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "mode": "recurring",
                "trading_pair": "XBTCHF",
                "deposit_day": 15,
                "crypto_amount": 0.0002,
            }
        ),
        encoding="utf-8",
    )
    # Config requires non-empty credentials to instantiate, so load it with
    # dummy values and then clear them to exercise the preflight credential check.
    config = Config(config_path=config_path)
    config.api_key = ""
    config.api_secret = ""
    preflight = _make_preflight(config, tmp_path)
    result = preflight.run()
    assert result.overall == CheckStatus.FAIL
    assert _check_names(result)["kraken_credentials_present"] == CheckStatus.FAIL


def test_invalid_credentials(tmp_config, tmp_path):
    api = _FakeKrakenAPI(auth_ok=False)
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    assert _check_names(result)["kraken_authentication"] == CheckStatus.FAIL
    assert result.overall == CheckStatus.FAIL


def test_insufficient_balance(tmp_config, tmp_path):
    api = _FakeKrakenAPI(balance={"ZCHF": 0.0})
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    assert _check_names(result)["balance_sufficient"] == CheckStatus.FAIL
    assert result.overall == CheckStatus.FAIL


def test_amount_below_minimum(tmp_config, tmp_path):
    api = _FakeKrakenAPI(pair_info={"ordermin": "0.001", "costmin": "10", "lot_decimals": 8, "pair_decimals": 2})
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    assert _check_names(result)["volume_above_minimum"] == CheckStatus.FAIL
    assert result.overall == CheckStatus.FAIL


def test_invalid_precision(tmp_config, tmp_path):
    api = _FakeKrakenAPI(pair_info={"ordermin": "0.00000001", "costmin": "0.01", "lot_decimals": 5, "pair_decimals": 2})
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run(amount=0.000123456)
    assert _check_names(result)["amount_precision"] == CheckStatus.WARN


def test_invalid_pair(tmp_config, tmp_path):
    api = _FakeKrakenAPI()

    def raise_pair(*_args, **_kwargs):
        raise Exception("Unknown asset pair")

    api.get_asset_pair_info = raise_pair
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    assert _check_names(result)["pair_exists"] == CheckStatus.FAIL
    assert result.overall == CheckStatus.FAIL


def test_stale_heartbeat(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    preflight.data_dir.mkdir(parents=True, exist_ok=True)
    old_ts = (utc_now() - timedelta(seconds=DEFAULT_HEARTBEAT_MAX_AGE_SECONDS + 60)).isoformat()
    (preflight.data_dir / "heartbeat.json").write_text(
        json.dumps({"timestamp": old_ts, "status": "running"}),
        encoding="utf-8",
    )
    result = preflight.run()
    assert _check_names(result)["heartbeat_recent"] == CheckStatus.WARN
    assert result.overall == CheckStatus.WARN


def test_hold_state(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    state = BotState(persist=False, status="hold")
    preflight = _make_preflight(tmp_config, tmp_path, api=api, state=state)
    result = preflight.run()
    assert _check_names(result)["no_hold_state"] == CheckStatus.FAIL
    assert result.overall == CheckStatus.FAIL


def test_unknown_order(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    store = OrderAttemptStore(filepath=tmp_path / "data" / "order_attempts.json")
    attempt = OrderAttempt(
        attempt_id="test-1",
        cycle_id="cycle-1",
        pair="XBTCHF",
        amount=0.0002,
        price=60000.0,
        strategy="scheduled",
        simulated=False,
        state=OrderState.UNKNOWN.value,
    )
    store.save(attempt)
    preflight = _make_preflight(tmp_config, tmp_path, api=api, attempt_store=store)
    result = preflight.run()
    assert _check_names(result)["no_unknown_orders"] == CheckStatus.FAIL
    assert result.overall == CheckStatus.FAIL


def test_telegram_failure(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    notifier = MagicMock()
    notifier.enabled = True
    notifier.test = lambda: (False, "Network unreachable")
    preflight = _make_preflight(tmp_config, tmp_path, api=api, notifier=notifier)
    result = preflight.run(telegram_test=True)
    assert _check_names(result)["telegram_test"] == CheckStatus.FAIL
    assert result.overall == CheckStatus.FAIL


def test_json_output_format(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    data = result.to_dict(redact=True)
    assert data["overall"] in ("PASS", "WARN")
    assert "checks" in data
    assert "preflight_id" in data
    assert data["can_place_live_orders"] is True


def test_secret_redaction_in_output(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    data = result.to_dict(redact=True)
    raw = json.dumps(data)
    assert os.environ["KRAKEN_API_KEY"] not in raw
    assert os.environ["KRAKEN_API_SECRET"] not in raw
    assert os.environ["SESSION_SECRET"] not in raw

    # Explicitly verify the redaction helper masks secret-bearing dictionary keys.
    from bot.preflight import _redact_data

    assert _redact_data({"api_key": "supersecret"}) == {"api_key": "[REDACTED]"}
    assert _redact_data({"nested": {"password": "hunter2"}}) == {"nested": {"password": "[REDACTED]"}}


def test_latest_preflight_is_valid(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    path = tmp_path / "preflight.json"
    write_preflight_result(path, result)

    ok, reason = latest_preflight_is_valid(tmp_config, path=path)
    assert ok is True
    assert reason == "ok"

    # Expired result is rejected.
    expired = PreflightResult(
        preflight_id="expired",
        ran_at=(utc_now() - timedelta(hours=2)).isoformat(),
        expires_at=(utc_now() - timedelta(hours=1)).isoformat(),
        config_hash=result.config_hash,
        overall=CheckStatus.PASS,
        can_place_live_orders=True,
        checks=[],
    )
    write_preflight_result(path, expired)
    ok, reason = latest_preflight_is_valid(tmp_config, path=path)
    assert ok is False
    assert "expired" in reason


def test_preflight_cli_json(tmp_config, tmp_path, monkeypatch):
    # Run the CLI via subprocess to verify exit code and JSON output.
    project_root = Path(__file__).parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    env.update(
        {
            "APP_ENV": "production",
            "DEMO_MODE": "false",
            "LIVE_TRADING_ENABLED": "true",
            "KRAKEN_API_KEY": "AK_CLI_12345",
            "KRAKEN_API_SECRET": "AS_CLI_67890",
            "SESSION_SECRET": "a" * 32,
            "WEB_UI_PASSWORD_HASH": "$2b$12$hash",
            "WEB_UI_SECURE_COOKIE": "true",
            "DISABLE_RATE_LIMIT": "",
            "RATE_LIMIT_ENABLED": "true",
        }
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "mode": "recurring",
                "trading_pair": "XBTCHF",
                "deposit_day": 15,
                "crypto_amount": 0.0002,
                "dip_threshold_percent": 5.0,
                "poll_interval_seconds": 600,
                "buy_hour": 8,
                "dip_buy_cooldown_hours": 24.0,
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "preflight.json"
    cmd = [
        sys.executable,
        "scripts/preflight_production.py",
        "--json",
        "--output",
        str(output_path),
        "--config",
        str(config_path),
    ]
    # We cannot easily inject the fake API from subprocess, so this test verifies
    # only the CLI wiring and JSON shape. The preflight will likely fail because
    # Kraken credentials are fake, which is fine for JSON-shape testing.
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parent.parent, env=env)
    assert result.stdout, f"CLI produced no stdout; stderr: {result.stderr}"
    parsed = json.loads(result.stdout)
    assert "overall" in parsed
    assert "can_place_live_orders" in parsed


def test_preflight_cli_demo_mode_exits_nonzero(tmp_path, monkeypatch):
    project_root = Path(__file__).parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    env["APP_ENV"] = "production"
    env["DEMO_MODE"] = "true"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "mode": "recurring",
                "trading_pair": "XBTCHF",
                "deposit_day": 15,
                "crypto_amount": 0.0002,
            }
        ),
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        "scripts/preflight_production.py",
        "--json",
        "--config",
        str(config_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parent.parent, env=env)
    assert result.stdout, f"CLI produced no stdout; stderr: {result.stderr}"
    parsed = json.loads(result.stdout)
    assert parsed["overall"] == "FAIL"
    assert result.returncode == 1


def test_disabled_checks_are_skipped(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    preflight = _make_preflight(
        tmp_config,
        tmp_path,
        api=api,
        disabled_checks=["kraken_minimum_permissions", "kraken_withdrawal_permission", "telegram_configured"],
    )
    result = preflight.run()
    names = _check_names(result)
    assert "kraken_minimum_permissions" not in names
    assert "kraken_withdrawal_permission" not in names
    assert "telegram_configured" not in names
    # Other checks still run.
    assert "kraken_authentication" in names
    assert "system_time_plausible" in names


def test_disabled_checks_from_config_attribute(tmp_config, tmp_path):
    api = _FakeKrakenAPI()
    tmp_config.preflight_disabled_checks = ["system_time_plausible", "bot_not_paused"]
    preflight = _make_preflight(tmp_config, tmp_path, api=api)
    result = preflight.run()
    names = _check_names(result)
    assert "system_time_plausible" not in names
    assert "bot_not_paused" not in names
    assert "app_env_production" in names
