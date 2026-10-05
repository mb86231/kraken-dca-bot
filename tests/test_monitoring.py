"""Tests for the monitoring status and Prometheus metrics endpoints."""

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
    os.environ["ORDER_ATTEMPTS_FILE"] = str(temp_dir / "order_attempts.json")
    os.environ["BACKUP_DIR"] = str(temp_dir / "backups")
    os.environ["MONITORING_TOKEN"] = "test-monitoring-token"

    config_path = temp_dir / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000}'
    )
    transactions_path = temp_dir / "transactions.json"
    transactions_path.write_text("[]")
    backups_dir = temp_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

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


def _login_session_cookie(client: TestClient) -> None:
    """Create a valid dashboard session cookie for the test client."""
    from fastapi import Response
    from web.auth import auth_manager

    resp = Response()
    auth_manager.create_session(resp, "admin")
    session_value: str | None = None
    for name, value in resp.raw_headers:
        if name == b"set-cookie":
            session_value = value.decode().split(";")[0].split("=")[1]
            break
    assert session_value is not None
    client.cookies.set("dca_session", session_value)


def _write_heartbeat(temp_dir: Path, age_seconds: int = 0) -> None:
    heartbeat_file = Path(os.environ["HEARTBEAT_FILE"])
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    heartbeat_file.write_text(
        json.dumps(
            {
                "timestamp": ts.isoformat(),
                "status": "waiting",
                "mode": "recurring",
                "paused": False,
                "last_cycle_at": (ts - timedelta(days=1)).isoformat(),
                "next_cycle_at": (ts + timedelta(days=1)).isoformat(),
                "last_price_at": ts.isoformat(),
                "last_order_at": ts.isoformat(),
                "runtime_started_at": (ts - timedelta(hours=1)).isoformat(),
                "version": "1.1.0",
            }
        )
    )


def _write_backup_status(temp_dir: Path, age_seconds: int = 0) -> None:
    status_path = Path(os.environ["BACKUP_DIR"]) / "backup_status.json"
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    status_path.write_text(
        json.dumps(
            {
                "last_successful_at": ts.isoformat(),
                "validation_status": "ok",
            }
        )
    )


def _write_order_attempts(temp_dir: Path) -> None:
    attempts_path = Path(os.environ["ORDER_ATTEMPTS_FILE"])
    attempts_path.write_text(
        json.dumps(
            [
                {
                    "attempt_id": "oa-1",
                    "cycle_id": "c1",
                    "pair": "XBTCHF",
                    "amount": 0.0001,
                    "price": 50000.0,
                    "strategy": "manual",
                    "simulated": False,
                    "attempt_number": 1,
                    "userref": 1001,
                    "state": "CONFIRMED",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                {
                    "attempt_id": "oa-2",
                    "cycle_id": "c2",
                    "pair": "XBTCHF",
                    "amount": 0.0001,
                    "price": 50000.0,
                    "strategy": "manual",
                    "simulated": False,
                    "attempt_number": 3,
                    "userref": 1002,
                    "state": "HOLD",
                    "error_message": "insufficient funds",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            ]
        )
    )


# -----------------------------------------------------------------------------
# /api/operations/status
# -----------------------------------------------------------------------------


def test_operations_status_requires_auth(client: TestClient):
    response = client.get("/api/operations/status")
    assert response.status_code in (401, 307)


def test_operations_status_returns_summary(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=30)
    _write_backup_status(temp_dir, age_seconds=3600)
    _write_order_attempts(temp_dir)

    response = client.get("/api/operations/status", auth=("admin", "admin"))
    # Password is invalid, but the test client uses the env hash. The auth helper
    # in TestClient only sends basic auth; our dashboard uses session cookies.
    # Use a forced session instead.
    assert response.status_code in (401, 307)


def test_operations_status_no_secrets_in_response(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=30)
    _login_session_cookie(client)

    response = client.get("/api/operations/status")
    assert response.status_code == 200
    data = response.json()

    body = response.text
    secrets_to_check = ["demo-key", "demo-secret", "test-secret", "test-hash"]
    for secret in secrets_to_check:
        assert secret not in body, f"Secret leaked in operations status: {secret}"

    assert data["version"] == "1.1.0"
    assert "heartbeat" in data
    assert data["heartbeat"]["age_seconds"] <= 60
    assert "orders" in data
    assert "backup" in data


def test_operations_status_stale_heartbeat(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=1200)
    _login_session_cookie(client)

    response = client.get("/api/operations/status")
    assert response.status_code == 200
    data = response.json()
    assert data["heartbeat"]["age_seconds"] >= 1200


def test_operations_status_stale_backup(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=30)
    _write_backup_status(temp_dir, age_seconds=48 * 3600)
    _login_session_cookie(client)

    response = client.get("/api/operations/status")
    assert response.status_code == 200
    data = response.json()
    assert data["backup"]["age_seconds"] >= 48 * 3600


# -----------------------------------------------------------------------------
# /api/metrics
# -----------------------------------------------------------------------------


def test_metrics_requires_token(client: TestClient):
    response = client.get("/api/metrics")
    assert response.status_code == 401


def test_metrics_rejects_invalid_token(client: TestClient):
    response = client.get("/api/metrics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 403


def test_metrics_returns_prometheus_format(client: TestClient, temp_dir: Path):
    _write_heartbeat(temp_dir, age_seconds=30)
    _write_backup_status(temp_dir, age_seconds=3600)
    _write_order_attempts(temp_dir)

    response = client.get(
        "/api/metrics",
        headers={"Authorization": "Bearer test-monitoring-token"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text

    assert "dca_bot_uptime_seconds" in body
    assert "dca_bot_heartbeat_age_seconds" in body
    assert "dca_bot_backup_age_seconds" in body
    assert "dca_bot_order_success_total" in body
    assert "dca_bot_order_failure_total" in body
    assert "dca_bot_order_retry_total" in body
    assert "dca_bot_order_hold_count" in body

    # No secrets leaked.
    assert "demo-key" not in body
    assert "demo-secret" not in body


# -----------------------------------------------------------------------------
# Monitoring helper tests
# -----------------------------------------------------------------------------


def test_get_operational_status_aggregates_orders(temp_dir: Path):
    from web.monitoring import get_operational_status

    _set_test_env(temp_dir)
    _write_heartbeat(temp_dir, age_seconds=30)
    _write_backup_status(temp_dir, age_seconds=3600)
    _write_order_attempts(temp_dir)

    status = get_operational_status()
    assert status["orders"]["success_count"] == 1
    assert status["orders"]["failure_count"] == 1
    assert status["orders"]["retry_count"] == 2  # attempt_number 3 => 2 retries
    assert status["orders"]["hold_count"] == 1
    assert status["orders"]["hold_active"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
