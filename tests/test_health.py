"""Tests for the HTTP liveness/readiness health endpoints and heartbeat file."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.app import app


def _set_test_env(temp_dir: Path):
    os.environ["APP_ENV"] = "development"
    os.environ["WEB_UI_USERNAME"] = "admin"
    os.environ["WEB_UI_PASSWORD_HASH"] = "test-hash"
    os.environ["SESSION_SECRET"] = "test-secret-32-bytes-long-value"
    os.environ["DEMO_MODE"] = "true"
    os.environ["KRAKEN_API_KEY"] = "demo-key"
    os.environ["KRAKEN_API_SECRET"] = "demo-secret"
    os.environ["LIVE_TRADING_ENABLED"] = "false"
    os.environ["WEB_UI_SECURE_COOKIE"] = "false"
    os.environ["DISABLE_RATE_LIMIT"] = "true"
    os.environ["OIDC_ENABLED"] = "false"
    os.environ.pop("OIDC_ISSUER_URL", None)
    os.environ.pop("OIDC_CLIENT_ID", None)
    os.environ.pop("OIDC_CLIENT_SECRET", None)
    os.environ["HEARTBEAT_FILE"] = str(temp_dir / "heartbeat.json")
    os.environ["HEARTBEAT_MAX_AGE_SECONDS"] = "900"

    config_path = temp_dir / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000}'
    )
    transactions_path = temp_dir / "transactions.json"
    transactions_path.write_text("[]")

    from bot.config import Config
    from bot.state import BotState, RuntimeOverrides
    from bot.store import TransactionStore

    app.state.config = Config(config_path=config_path)
    app.state.store = TransactionStore(filepath=transactions_path)
    app.state.bot_state = BotState(persist=False)
    app.state.overrides = RuntimeOverrides(filepath=temp_dir / "runtime_overrides.json")


@pytest.fixture
def client(temp_dir: Path):
    _set_test_env(temp_dir)
    from web.oidc import oidc_provider

    oidc_provider._discovery = None
    oidc_provider._jwks = None
    oidc_provider._discovered_at = None
    with TestClient(app) as c:
        yield c


def _write_heartbeat(temp_dir: Path, age_seconds: int = 0) -> Path:
    """Write a heartbeat file with a timestamp ``age_seconds`` in the past."""
    heartbeat_file = Path(os.environ["HEARTBEAT_FILE"])
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    heartbeat_file.write_text(
        json.dumps(
            {
                "timestamp": ts.isoformat(),
                "status": "waiting",
                "mode": "recurring",
                "paused": False,
                "last_cycle_at": None,
                "next_cycle_at": None,
                "last_price_at": ts.isoformat(),
                "last_order_at": None,
                "runtime_started_at": ts.isoformat(),
                "version": "1.1.0",
            }
        )
    )
    return heartbeat_file


def test_health_live_returns_alive(client: TestClient):
    response = client.get("/health/live")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "alive"
    assert data["app"] == "ok"


def test_health_ready_without_heartbeat_is_not_ready(client: TestClient):
    response = client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not_ready"
    assert data["trading_loop"] == "missing"
    assert data["checks"]["heartbeat"] == "missing"


def test_health_ready_with_recent_heartbeat(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=30)
    app.state.bot_state.status = "waiting"

    response = client.get("/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["trading_loop"] == "ok"
    assert data["storage"] == "ok"
    assert data["configuration"] == "ok"
    assert data["heartbeat_age_seconds"] <= 60


def test_health_ready_stale_heartbeat(client: TestClient, temp_dir: Path, monkeypatch):
    _write_heartbeat(temp_dir, age_seconds=1200)
    monkeypatch.setenv("HEARTBEAT_MAX_AGE_SECONDS", "900")
    app.state.bot_state.status = "waiting"

    response = client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not_ready"
    assert data["trading_loop"] == "stale"
    assert data["checks"]["heartbeat"] == "stale"
    assert data["heartbeat_age_seconds"] >= 1200


def test_health_ready_paused_bot_remains_ready(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=10)
    app.state.bot_state.status = "paused"
    app.state.bot_state.paused = True

    response = client.get("/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["checks"]["bot_state"] == "paused"


def test_health_ready_fatal_state_unready(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=10)
    app.state.bot_state.status = "stopped"

    response = client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not_ready"
    assert data["checks"]["bot_state"] == "fatal"


def test_health_ready_error_state_unready(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=10)
    app.state.bot_state.status = "error"

    response = client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["checks"]["bot_state"] == "fatal"


def test_health_ready_unwritable_storage_is_not_ready(client: TestClient, temp_dir: Path, monkeypatch):
    _write_heartbeat(temp_dir, age_seconds=10)
    app.state.bot_state.status = "waiting"

    import web.app as web_app

    monkeypatch.setattr(web_app, "_is_data_dir_writable", lambda _path: False)

    response = client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["storage"] == "not_writable"
    assert data["checks"]["storage"] == "not_writable"


def test_health_ready_response_contains_no_secrets(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=10)
    app.state.bot_state.status = "waiting"

    response = client.get("/health/ready")
    body = response.text
    secrets_to_check = [
        "demo-key",
        "demo-secret",
        "test-secret",
        "test-hash",
        "KRAKEN_API_KEY",
        "api_key",
        "api_secret",
    ]
    for secret in secrets_to_check:
        assert secret not in body


def test_heartbeat_file_written_by_bot(temp_dir: Path, monkeypatch):
    """The trading loop's heartbeat writer produces the expected JSON file."""
    import bot.core as core_module

    from bot.config import Config
    from bot.core import KrakenDCA
    from bot.state import BotState
    from bot.store import TransactionStore

    config_path = temp_dir / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000}'
    )
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")

    config = Config(config_path=config_path)
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    state = BotState(filepath=temp_dir / "state.json", persist=False)
    state.status = "waiting"
    state.mode = "recurring"

    bot = KrakenDCA(config=config, store=store, state=state)
    # Override the default heartbeat path for the test.
    written = {}

    def _capture_atomic_write(filepath, data, indent=2):
        if filepath.name == "heartbeat.json":
            written["data"] = data
            written["path"] = filepath
        # Do not actually write, to keep the temp dir clean.

    monkeypatch.setattr(core_module, "atomic_write_json", _capture_atomic_write)
    bot.write_health()

    assert written["path"].name == "heartbeat.json"
    data = written["data"]
    assert "timestamp" in data
    assert data["status"] == "waiting"
    assert data["mode"] == "recurring"
    assert "api_key" not in data
    assert "api_secret" not in data
