"""Additional coverage for dashboard API endpoints."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from web.app import app


def _set_test_env(temp_dir: Path):
    os.environ["APP_ENV"] = "development"
    os.environ["WEB_UI_USERNAME"] = "admin"
    os.environ["WEB_UI_PASSWORD_HASH"] = bcrypt.hashpw("testpass".encode(), bcrypt.gensalt()).decode()
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
    os.environ["BACKUP_DIR"] = str(temp_dir / "backups")

    config_path = temp_dir / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000}'
    )
    transactions_path = temp_dir / "transactions.json"
    transactions_path.write_text("[]")

    from bot.config import Config
    from bot.order_execution import OrderAttemptStore
    from bot.state import BotState, RuntimeOverrides
    from bot.store import TransactionStore

    app.state.config = Config(config_path=config_path)
    app.state.store = TransactionStore(filepath=transactions_path)
    app.state.attempt_store = OrderAttemptStore(filepath=temp_dir / "order_attempts.json")
    app.state.bot_state = BotState()
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


def _get_login_csrf(client: TestClient) -> str:
    response = client.get("/login")
    assert response.status_code == 200
    return str(client.cookies.get("dca_csrf") or "")


@pytest.fixture
def auth_client(client: TestClient):
    csrf = _get_login_csrf(client)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "dca_session" in response.cookies
    client.headers["X-CSRF-Token"] = response.cookies.get("dca_csrf") or ""
    return client


def test_api_performance_empty(auth_client: TestClient):
    response = auth_client.get("/api/performance")
    assert response.status_code == 200
    data = response.json()
    assert data["purchase_count"] == 0
    assert data["total_invested"] == 0


def test_api_transactions_authenticated(auth_client: TestClient):
    response = auth_client.get("/api/transactions")
    assert response.status_code == 200


def test_api_transactions_export_json(auth_client: TestClient):
    app.state.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
    response = auth_client.get("/api/transactions/export?format=json")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1


def test_api_transactions_export_csv(auth_client: TestClient):
    app.state.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
    response = auth_client.get("/api/transactions/export?format=csv")
    assert response.status_code == 200
    assert "strategy" in response.text


def test_api_transactions_filters(auth_client: TestClient):
    app.state.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled", simulated=True)
    app.state.store.add_transaction("XBTCHF", 0.0002, 50000.0, strategy="manual", simulated=False)
    response = auth_client.get("/api/transactions?strategy=manual&simulated=false")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["transactions"][0]["strategy"] == "manual"


def test_api_performance_with_transaction(auth_client: TestClient):
    store = app.state.store
    store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
    response = auth_client.get("/api/performance")
    assert response.status_code == 200
    data = response.json()
    assert data["purchase_count"] == 1


def test_api_charts_portfolio_value(auth_client: TestClient):
    store = app.state.store
    store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
    response = auth_client.get("/api/charts/portfolio_value?range=all")
    assert response.status_code == 200
    data = response.json()
    assert "labels" in data
    assert "datasets" in data


def test_api_charts_ranges(auth_client: TestClient):
    store = app.state.store
    store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
    for range in ("24h", "7d", "30d", "90d", "1y", "all"):
        response = auth_client.get(f"/api/charts/invested_vs_value?range={range}")
        assert response.status_code == 200, range


def test_api_charts_not_found(auth_client: TestClient):
    response = auth_client.get("/api/charts/nonexistent?range=all")
    assert response.status_code == 404


def test_api_chart_price(auth_client: TestClient):
    response = auth_client.get("/api/charts/price?range=24h")
    assert response.status_code == 200
    data = response.json()
    assert "price_dataset" in data
    assert "buy_dataset" in data


def test_api_logs_empty(auth_client: TestClient, temp_dir: Path):
    log_path = Path("logs/app.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")
    response = auth_client.get("/api/logs")
    assert response.status_code == 200
    data = response.json()
    assert data["logs"] == []


def test_api_logs_download(auth_client: TestClient, temp_dir: Path):
    log_path = Path("logs/app.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({"level": "INFO", "message": "test"}) + "\n")
    response = auth_client.get("/api/logs/download")
    assert response.status_code == 200
    assert "test" in response.text


def test_api_logs_archive(auth_client: TestClient, temp_dir: Path):
    log_path = Path("logs/app.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({"level": "INFO", "message": "test"}) + "\n")
    response = auth_client.post("/api/logs/archive")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "archive" in data


def test_api_logs_archive_missing(auth_client: TestClient, temp_dir: Path):
    log_path = Path("logs/app.json")
    if log_path.exists():
        log_path.unlink()
    response = auth_client.post("/api/logs/archive")
    assert response.status_code == 404


def test_api_logs_filters(auth_client: TestClient, temp_dir: Path):
    log_path = Path("logs/app.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps({"level": "INFO", "message": "alpha", "timestamp": "2026-07-25T10:00:00"}) + "\n"
        + json.dumps({"level": "ERROR", "message": "beta", "timestamp": "2026-07-25T12:00:00"}) + "\n"
    )
    response = auth_client.get("/api/logs?level=ERROR&search=beta&from_date=2026-07-25T11:00:00&to_date=2026-07-25T13:00:00")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["logs"][0]["message"] == "beta"


def test_api_alerts(auth_client: TestClient, temp_dir: Path):
    alerts_path = Path("data/alerts.json")
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    alerts_path.write_text(json.dumps([{"id": "1", "message": "test", "acknowledged": False}]))
    response = auth_client.get("/api/alerts")
    assert response.status_code == 200
    data = response.json()
    assert len(data["alerts"]) == 1


def test_api_alert_acknowledge(auth_client: TestClient, temp_dir: Path):
    alerts_path = Path("data/alerts.json")
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    alerts_path.write_text(json.dumps([{"id": "1", "message": "test", "acknowledged": False}]))
    response = auth_client.post("/api/alerts/1/acknowledge")
    assert response.status_code == 200


def test_api_alert_acknowledge_missing(auth_client: TestClient):
    response = auth_client.post("/api/alerts/missing/acknowledge")
    assert response.status_code == 404


def test_api_backup_create_and_list(auth_client: TestClient, temp_dir: Path):
    response = auth_client.post("/api/backups/create")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    response = auth_client.get("/api/backups")
    assert response.status_code == 200
    data = response.json()
    assert len(data["backups"]) >= 1


def test_api_settings_history(auth_client: TestClient, temp_dir: Path):
    audit_path = Path("data/audit_log.json")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps([{"action": "test", "timestamp": datetime.now(timezone.utc).isoformat()}]))
    response = auth_client.get("/api/settings/history")
    assert response.status_code == 200
    assert "history" in response.json()


def test_api_settings_runtime(auth_client: TestClient):
    response = auth_client.get("/api/settings/runtime")
    assert response.status_code == 200
    data = response.json()
    assert "source" in data


def test_api_telegram(auth_client: TestClient):
    response = auth_client.get("/api/telegram")
    assert response.status_code == 200
    data = response.json()
    assert "enabled" in data


def test_api_telegram_test_no_config(auth_client: TestClient):
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    response = auth_client.post("/api/telegram/test")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False


def test_api_restore_settings(auth_client: TestClient, temp_dir: Path):
    config_path = app.state.config.config_path
    original = config_path.read_text()
    auth_client.put("/api/settings", json={"max_price": 12345})
    response = auth_client.post("/api/settings/restore")
    assert response.status_code == 200
    assert config_path.read_text() == original


def test_api_bot_controls(auth_client: TestClient):
    response = auth_client.post("/api/bot/pause")
    assert response.status_code == 200
    assert app.state.bot_state.status == "paused"

    response = auth_client.post("/api/bot/resume")
    assert response.status_code == 200
    assert app.state.bot_state.status == "waiting"

    response = auth_client.post("/api/bot/cycle")
    assert response.status_code == 200

    response = auth_client.post("/api/bot/stop")
    assert response.status_code == 200

    response = auth_client.post("/api/bot/restart")
    assert response.status_code == 200


def test_api_bot_cycle_when_stopped(auth_client: TestClient):
    app.state.bot_state.status = "stopped"
    response = auth_client.post("/api/bot/cycle")
    assert response.status_code == 503


def test_api_bot_cycle_when_paused(auth_client: TestClient):
    app.state.bot_state.status = "paused"
    app.state.bot_state.paused = True
    app.state.overrides.set_paused(True, reason="test")
    response = auth_client.post("/api/bot/cycle")
    assert response.status_code == 409


def test_api_bot_cycle_over_budget(auth_client: TestClient):
    app.state.bot_state.status = "waiting"
    app.state.bot_state.paused = False
    app.state.overrides.set_paused(False)
    response = auth_client.post("/api/bot/cycle", json={"over_budget": True})
    assert response.status_code == 200
    assert "over monthly budget" in response.json()["message"]
    assert app.state.overrides.manual_buy_over_budget is True
    app.state.overrides.clear_manual_cycle()
    app.state.overrides.clear_over_budget()


def test_api_restore_settings_missing_backup(auth_client: TestClient, temp_dir: Path):
    config_path = app.state.config.config_path
    backup_path = config_path.with_suffix(".json.bak")
    if backup_path.exists():
        backup_path.unlink()
    response = auth_client.post("/api/settings/restore")
    assert response.status_code == 404


def test_web_index_redirect(auth_client: TestClient):
    response = auth_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"


def test_web_telegram_page_redirect(auth_client: TestClient):
    response = auth_client.get("/telegram", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_web_health_page_redirect(auth_client: TestClient):
    response = auth_client.get("/health", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_web_unknown_page_404(auth_client: TestClient):
    response = auth_client.get("/nonexistent-page")
    assert response.status_code == 404


def test_validate_production_security_rejects_short_session_secret(monkeypatch):
    from web.security import validate_production_security

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "short")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "false")
    with pytest.raises(RuntimeError, match="SESSION_SECRET must be at least 16 characters"):
        validate_production_security()


def test_health_ready_invalid_heartbeat_timestamp(auth_client: TestClient, temp_dir: Path, monkeypatch):
    heartbeat_file = temp_dir / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"timestamp": "not-a-timestamp"}))
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    response = auth_client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["checks"]["heartbeat"] == "invalid_timestamp"


def test_health_ready_missing_bot_state(auth_client: TestClient, temp_dir: Path, monkeypatch):
    heartbeat_file = temp_dir / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    original_state = app.state.bot_state
    app.state.bot_state = None
    try:
        response = auth_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["checks"]["bot_state"] == "missing"
    finally:
        app.state.bot_state = original_state


def test_health_ready_fatal_bot_state(auth_client: TestClient, temp_dir: Path, monkeypatch):
    heartbeat_file = temp_dir / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    from bot.state import BotState

    original_state = app.state.bot_state
    app.state.bot_state = BotState()
    app.state.bot_state.status = "error"
    try:
        response = auth_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["checks"]["bot_state"] == "fatal"
    finally:
        app.state.bot_state = original_state


def test_health_ready_invalid_heartbeat_format(auth_client: TestClient, temp_dir: Path, monkeypatch):
    heartbeat_file = temp_dir / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"no_timestamp": True}))
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    response = auth_client.get("/health/ready")
    data = response.json()
    assert data["checks"]["heartbeat"] == "invalid_format"


def test_health_ready_backup_status_parsing(auth_client: TestClient, temp_dir: Path, monkeypatch):
    heartbeat_file = temp_dir / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    monkeypatch.setenv("BACKUP_DIR", str(temp_dir / "backups"))
    backup_dir = temp_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    status_file = backup_dir / "backup_status.json"
    status_file.write_text(
        json.dumps({
            "last_successful_at": datetime.now(timezone.utc).isoformat(),
            "validation_status": "valid",
        })
    )
    response = auth_client.get("/health/ready")
    data = response.json()
    assert data["checks"]["backup_status"] == "valid"
    assert data["checks"]["backup_age_seconds"] is not None


def test_health_ready_backup_status_invalid_timestamp(auth_client: TestClient, temp_dir: Path, monkeypatch):
    heartbeat_file = temp_dir / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    monkeypatch.setenv("BACKUP_DIR", str(temp_dir / "backups2"))
    backup_dir = temp_dir / "backups2"
    backup_dir.mkdir(parents=True, exist_ok=True)
    status_file = backup_dir / "backup_status.json"
    status_file.write_text(json.dumps({"last_successful_at": "invalid", "validation_status": "valid"}))
    response = auth_client.get("/health/ready")
    data = response.json()
    assert data["checks"]["backup_status"] == "invalid_timestamp"


def test_health_ready_missing_config(auth_client: TestClient):
    original_config = app.state.config
    app.state.config = None
    try:
        response = auth_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["checks"]["configuration"] == "missing"
    finally:
        app.state.config = original_config


def test_health_ready_config_not_readable(auth_client: TestClient, temp_dir: Path):
    original_config = app.state.config
    existing_config = app.state.config
    existing_config.config_path = temp_dir / "deleted_config.json"
    existing_config.config_path.write_text("{}")
    existing_config.config_path.unlink()
    app.state.config = existing_config
    try:
        response = auth_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["checks"]["configuration"] == "config_not_readable"
    finally:
        app.state.config = original_config


def test_health_ready_missing_store(auth_client: TestClient):
    original_store = app.state.store
    app.state.store = None
    try:
        response = auth_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["checks"]["storage"] == "missing"
    finally:
        app.state.store = original_store


def test_health_ready_unwritable_storage(auth_client: TestClient, temp_dir: Path, monkeypatch):
    heartbeat_file = temp_dir / "heartbeat.json"
    heartbeat_file.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    original_store = app.state.store
    from bot.store import TransactionStore

    app.state.store = TransactionStore(filepath=temp_dir / "transactions.json")
    monkeypatch.setattr("web.app._is_data_dir_writable", lambda _path: False)
    try:
        response = auth_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["checks"]["storage"] == "not_writable"
    finally:
        app.state.store = original_store


def test_order_cancel_endpoint(auth_client: TestClient):
    from bot.order_execution import OrderAttempt, OrderState

    attempt = OrderAttempt(
        attempt_id="oa-test-1",
        cycle_id="XBTCHF:scheduled:2025-01-01T00:00:00",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=False,
        state=OrderState.HOLD.value,
        final_outcome="test hold",
    )
    app.state.attempt_store.save(attempt)

    response = auth_client.post("/api/orders/oa-test-1/cancel")
    assert response.status_code == 200
    data = response.json()
    assert data["order"]["state"] == OrderState.CANCELLED.value


def test_order_acknowledge_endpoint(auth_client: TestClient):
    from bot.order_execution import OrderAttempt, OrderState

    attempt = OrderAttempt(
        attempt_id="oa-test-2",
        cycle_id="XBTCHF:scheduled:2025-01-01T00:00:00",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=False,
        state=OrderState.HOLD.value,
        final_outcome="test hold",
    )
    app.state.attempt_store.save(attempt)

    response = auth_client.post("/api/orders/oa-test-2/acknowledge")
    assert response.status_code == 200
    data = response.json()
    assert data["order"]["acknowledged"] is True


def test_order_resolve_endpoint_retry(auth_client: TestClient):
    from bot.order_execution import OrderAttempt, OrderState

    attempt = OrderAttempt(
        attempt_id="oa-test-3",
        cycle_id="XBTCHF:scheduled:2025-01-01T00:00:00",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=False,
        state=OrderState.UNKNOWN.value,
    )
    app.state.attempt_store.save(attempt)

    response = auth_client.post("/api/orders/oa-test-3/resolve", json={"action": "retry"})
    assert response.status_code == 200
    data = response.json()
    assert data["order"]["state"] == OrderState.RETRY_SCHEDULED.value


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
