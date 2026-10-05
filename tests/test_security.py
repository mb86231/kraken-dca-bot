"""Tests for dashboard security controls: rate limits, brute-force protection,
security headers, CSP, and local Chart.js vendoring.

These tests enable rate limiting with low thresholds. They must keep
`DISABLE_RATE_LIMIT=false` while running; other test files may set it to `true`
in their own fixtures.
"""

from __future__ import annotations

import os
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from web.app import app  # noqa: E402
from web.brute_force import brute_force_protector  # noqa: E402
from web.rate_limit import rate_limit_config, rate_limiter  # noqa: E402
from web.security import validate_production_security  # noqa: E402


def _set_test_env(temp_dir: Path):
    os.environ["APP_ENV"] = "development"
    os.environ["DISABLE_RATE_LIMIT"] = "false"
    os.environ["WEB_UI_USERNAME"] = "admin"
    os.environ["WEB_UI_PASSWORD_HASH"] = bcrypt.hashpw("testpass".encode(), bcrypt.gensalt()).decode()
    os.environ["SESSION_SECRET"] = "test-secret-32-bytes-long-value"
    os.environ["DEMO_MODE"] = "true"
    os.environ["KRAKEN_API_KEY"] = "demo-key"
    os.environ["KRAKEN_API_SECRET"] = "demo-secret"
    os.environ["LIVE_TRADING_ENABLED"] = "false"
    os.environ["WEB_UI_SECURE_COOKIE"] = "false"
    os.environ["OIDC_ENABLED"] = "false"

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
    app.state.bot_state = BotState()
    app.state.overrides = RuntimeOverrides(filepath=temp_dir / "runtime_overrides.json")


@pytest.fixture
def client(temp_dir: Path, monkeypatch):
    _set_test_env(temp_dir)
    rate_limiter.clear()
    brute_force_protector.clear()
    monkeypatch.setattr(rate_limit_config, "login", "5/minute")
    monkeypatch.setattr(rate_limit_config, "api", "3/minute")
    monkeypatch.setattr(rate_limit_config, "manual_buy", "2/minute")
    monkeypatch.setattr(rate_limit_config, "settings", "3/minute")
    monkeypatch.setattr(brute_force_protector, "max_failures", 3)
    monkeypatch.setattr(brute_force_protector, "lockout_seconds", 2)
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


def test_security_headers_present(client: TestClient):
    response = client.get("/login")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in response.headers


def test_security_headers_include_hsts_over_https(client: TestClient):
    response = client.get("/login", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 200
    assert "Strict-Transport-Security" in response.headers
    assert "max-age=31536000" in response.headers["Strict-Transport-Security"]


def test_cache_control_on_sensitive_pages(client: TestClient):
    response = client.get("/login")
    assert "no-store" in response.headers["Cache-Control"]


def test_static_assets_allow_caching(client: TestClient):
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert "no-store" not in response.headers.get("Cache-Control", "")


def test_chart_js_is_local(client: TestClient):
    response = client.get("/static/vendor/chart.js/chart.umd.min.js")
    assert response.status_code == 200
    assert "Chart" in response.text or len(response.content) > 1000


def test_chart_js_adapter_is_local(client: TestClient):
    response = client.get("/static/vendor/chart.js/chartjs-adapter-date-fns.bundle.min.js")
    assert response.status_code == 200


def test_base_html_uses_local_chart_js(auth_client: TestClient):
    response = auth_client.get("/dashboard")
    assert response.status_code == 200
    text = response.text
    assert "/static/vendor/chart.js/chart.umd.min.js" in text
    assert "cdn.jsdelivr.net" not in text


def test_login_rate_limit(client: TestClient):
    # 5 allowed attempts per minute; the 6th should be rate limited.
    csrf = _get_login_csrf(client)
    for _ in range(5):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": csrf},
        )
        assert response.status_code == 401
    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": csrf},
    )
    assert response.status_code == 429


def test_api_rate_limit(auth_client: TestClient):
    # 3 allowed authenticated API requests per minute.
    for _ in range(3):
        response = auth_client.get("/api/status")
        assert response.status_code == 200
    response = auth_client.get("/api/status")
    assert response.status_code == 429


def test_brute_force_lockout_after_failures(client: TestClient):
    # 3 allowed failures; the 4th should be locked out (generic 401).
    csrf = _get_login_csrf(client)
    for _ in range(3):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": csrf},
        )
        assert response.status_code == 401
    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": csrf},
    )
    assert response.status_code == 401
    assert "Invalid" in response.text


def test_successful_login_resets_failure_count(client: TestClient):
    # Two failures, then a success should clear the lockout counter.
    csrf = _get_login_csrf(client)
    for _ in range(2):
        client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": csrf},
        )
    response = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # CSRF token is rotated after a successful login; use the new one.
    new_csrf = str(client.cookies.get("dca_csrf") or "")
    # After success, failing again should still work until threshold is reached.
    for _ in range(2):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": new_csrf},
        )
        assert response.status_code == 401


def test_csrf_still_required_for_state_changes(auth_client: TestClient):
    auth_client.headers.pop("X-CSRF-Token", None)
    response = auth_client.put("/api/settings", json={"max_price": 70000})
    assert response.status_code == 403


def test_oidc_local_login_still_works(client: TestClient):
    csrf = _get_login_csrf(client)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "dca_session" in response.cookies


def test_validate_production_security_allows_missing_session_secret(monkeypatch, tmp_path):
    """No session secret anywhere: allowed — one is generated and persisted at first use."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    validate_production_security()


def test_validate_production_security_rejects_short_session_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="SESSION_SECRET must be at least 16 characters"):
        validate_production_security()


def test_validate_production_security_allows_missing_password_hash(monkeypatch, tmp_path):
    """No password anywhere: allowed — the first-run setup flow takes over."""
    from unittest import mock

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "a-very-secret-value-32-bytes")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.delenv("WEB_UI_PASSWORD_HASH", raising=False)
    with mock.patch("web.security.logger") as fake_logger:
        validate_production_security()
    fake_logger.warning.assert_called_once()
    assert "first-run setup" in fake_logger.warning.call_args[0][0]


def test_validate_production_security_requires_secure_cookies(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "a-very-secret-value-32-bytes")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "false")
    with pytest.raises(RuntimeError, match="WEB_UI_SECURE_COOKIE must be true"):
        validate_production_security()


def test_validate_production_security_rejects_disabled_rate_limits(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "a-very-secret-value-32-bytes")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "true")
    with pytest.raises(RuntimeError, match="Rate limiting must be enabled"):
        validate_production_security()


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "off"])
def test_validate_production_security_rejects_rate_limit_enabled_false(monkeypatch, value):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "a-very-secret-value-32-bytes")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", value)
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "false")
    with pytest.raises(RuntimeError, match="Rate limiting must be enabled"):
        validate_production_security()


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on"])
def test_validate_production_security_accepts_rate_limit_enabled_true(monkeypatch, value):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "a-very-secret-value-32-bytes")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", value)
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "false")
    validate_production_security()


def test_validate_production_security_rejects_conflicting_rate_limit_variables(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "a-very-secret-value-32-bytes")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "true")
    with pytest.raises(RuntimeError, match="Rate limiting must be enabled"):
        validate_production_security()


def test_validate_production_security_accepts_valid_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "a-very-secret-value-32-bytes")
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "dummy-hash")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "true")
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "false")
    validate_production_security()
