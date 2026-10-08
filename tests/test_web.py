"""Tests for the web dashboard API."""

from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
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
    os.environ["SECRETS_PATH"] = str(temp_dir / "secrets.json")
    os.environ["WEB_UI_SECURE_COOKIE"] = "false"
    os.environ["DISABLE_RATE_LIMIT"] = "true"
    os.environ["OIDC_ENABLED"] = "false"
    os.environ.pop("OIDC_ISSUER_URL", None)
    os.environ.pop("OIDC_CLIENT_ID", None)
    os.environ.pop("OIDC_CLIENT_SECRET", None)

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
def client(temp_dir: Path):
    _set_test_env(temp_dir)
    from web.oidc import oidc_provider

    oidc_provider._discovery = None
    oidc_provider._jwks = None
    oidc_provider._discovered_at = None
    with TestClient(app) as c:
        yield c


def _get_login_csrf(client: TestClient) -> str:
    """Fetch the login page and return the CSRF cookie value."""
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
    # Persist cookies on the client for subsequent requests
    client.headers["X-CSRF-Token"] = response.cookies.get("dca_csrf") or ""
    return client


def test_login_and_dashboard(client):
    csrf = _get_login_csrf(client)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "dca_session" in response.cookies

    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in with Authentik" not in response.text

    response = client.get("/dashboard", cookies=response.cookies)
    assert response.status_code == 200
    assert "DCA-Bot" in response.text


def test_api_status_requires_auth(client):
    response = client.get("/api/status")
    assert response.status_code == 401


def test_api_status_authenticated(auth_client):
    response = auth_client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert data["demo_mode"] is True
    assert data["live_trading_enabled"] is False
    assert data["live_trading_env_override"] is True


def test_api_portfolio_authenticated(auth_client):
    response = auth_client.get("/api/portfolio")
    assert response.status_code == 200


def test_api_transactions_authenticated(auth_client):
    response = auth_client.get("/api/transactions")
    assert response.status_code == 200


def test_settings_update_dynamic_dca_roundtrip(auth_client):
    response = auth_client.put(
        "/api/settings",
        json={
            "dynamic_dca": {
                "enabled": True,
                "reference": "last_buy",
                "cooldown_hours": 12.0,
                "tiers": [
                    {"threshold_percent": 10.0, "amount": 0.0, "enabled": True},
                    {"threshold_percent": -20.0, "amount": 0.0005, "enabled": True},
                ],
            }
        },
    )
    assert response.status_code == 200

    response = auth_client.get("/api/settings")
    assert response.status_code == 200
    data = response.json()
    assert data["dynamic_dca"]["enabled"] is True
    assert data["dynamic_dca"]["cooldown_hours"] == 12.0
    assert len(data["dynamic_dca"]["tiers"]) == 2
    assert data["dynamic_dca"]["tiers"][-1]["amount"] == 0.0005


def test_settings_update_dynamic_dca_partial_keeps_tiers(auth_client):
    # First set dynamic DCA with a custom tier
    auth_client.put(
        "/api/settings",
        json={
            "dynamic_dca": {
                "enabled": True,
                "tiers": [
                    {"threshold_percent": -30.0, "amount": 0.0009, "enabled": True},
                ],
            }
        },
    )
    # Then toggle only enabled; tiers should remain
    response = auth_client.put(
        "/api/settings",
        json={"dynamic_dca": {"enabled": False}},
    )
    assert response.status_code == 200

    response = auth_client.get("/api/settings")
    data = response.json()
    assert data["dynamic_dca"]["enabled"] is False
    assert len(data["dynamic_dca"]["tiers"]) == 1
    assert data["dynamic_dca"]["tiers"][0]["amount"] == 0.0009


class _InputAttrParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            self.attrs = attrs


def test_settings_tier_checkbox_renders_checked_for_enabled_tier(auth_client):
    """Regression: a stray quote after the 'checked' interpolation produced
    the attribute 'checked\"' instead of 'checked', so enabled tiers always
    rendered unchecked after reload."""
    html = auth_client.get("/settings").text
    m = re.search(
        r'<td><input type="checkbox" class="tier-enabled"[^<]*\$\{tier\.enabled[^<]*</td>',
        html,
    )
    assert m, "tier checkbox template line not found on settings page"
    line = m.group(0)
    # Simulate an enabled tier: the JS interpolates 'checked' into the tag.
    rendered = line.replace("${tier.enabled ? 'checked' : ''}", "checked")
    rendered = rendered.split("<td>")[1].split("</td>")[0]
    parser = _InputAttrParser()
    parser.feed(rendered)
    attr_names = [name for name, _ in parser.attrs]
    assert "checked" in attr_names, f"checked missing from rendered attrs: {parser.attrs}"


def test_delete_all_transactions_authenticated(auth_client):
    # Seed a transaction first
    store = app.state.store
    store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
    assert store.get_transaction_count() > 0

    response = auth_client.delete("/api/transactions")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["count"] == 1
    assert store.get_transaction_count() == 0


def test_delete_all_transactions_without_csrf_is_rejected(client):
    csrf = _get_login_csrf(client)
    login = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    response = client.delete("/api/transactions", cookies=login.cookies)
    assert response.status_code == 403


def test_settings_update_with_csrf(auth_client):
    response = auth_client.put(
        "/api/settings",
        json={"max_price": 70000},
    )
    assert response.status_code == 200


def test_settings_update_live_trading_roundtrip(auth_client):
    response = auth_client.put(
        "/api/settings",
        json={"live_trading_enabled": True},
    )
    assert response.status_code == 200

    response = auth_client.get("/api/settings")
    data = response.json()
    # The LIVE_TRADING_ENABLED env var ("false") overrides config.json in tests.
    assert data["live_trading_enabled"] is False

    config_json = json.loads((app.state.config.config_path).read_text())
    assert config_json["live_trading_enabled"] is True


def test_exchange_status_endpoint(auth_client):
    response = auth_client.get("/api/settings/exchange")
    assert response.status_code == 200
    data = response.json()
    # Test env provides KRAKEN_API_KEY/SECRET, so environment wins.
    assert data["effective_source"] == "environment"
    assert data["env_key_set"] is True


def test_exchange_credentials_save_and_clear(auth_client):
    response = auth_client.put(
        "/api/settings/exchange",
        json={"api_key": "stored-key-123456", "api_secret": "stored-secret-123456"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["exchange"]["stored_key_set"] is True

    secrets_path = Path(os.environ["SECRETS_PATH"])
    saved = json.loads(secrets_path.read_text())
    assert saved["kraken_api_key"] == "stored-key-123456"
    # The raw secret must never appear in the API response.
    assert "stored-secret-123456" not in response.text

    response = auth_client.post("/api/settings/exchange/clear")
    assert response.status_code == 200
    saved = json.loads(secrets_path.read_text())
    assert "kraken_api_key" not in saved
    assert "kraken_api_secret" not in saved


def test_exchange_credentials_rejected_when_too_short(auth_client):
    response = auth_client.put(
        "/api/settings/exchange",
        json={"api_key": "short", "api_secret": "short"},
    )
    assert response.status_code == 422


def test_telegram_settings_persist_to_store(auth_client):
    response = auth_client.put(
        "/api/telegram",
        json={"bot_token": "123456:stored-token-abc", "chat_id": "424242"},
    )
    assert response.status_code == 200
    assert "stored" in response.json()["message"] or "saved" in response.json()["message"].lower()

    import os
    from pathlib import Path
    import json as _json
    secrets_path = Path(os.environ["SECRETS_PATH"])
    saved = _json.loads(secrets_path.read_text())
    assert saved["telegram_bot_token"] == "123456:stored-token-abc"
    assert saved["telegram_chat_id"] == "424242"
    assert "stored-token-abc" not in response.text

    response = auth_client.post("/api/telegram/clear")
    assert response.status_code == 200
    saved = _json.loads(secrets_path.read_text())
    assert "telegram_bot_token" not in saved


def test_auth_status_endpoint(auth_client):
    response = auth_client.get("/api/settings/auth")
    assert response.status_code == 200
    data = response.json()
    assert data["local"]["username"] == "admin"
    # Test env provides WEB_UI_PASSWORD_HASH and SESSION_SECRET.
    assert data["local"]["password_hash"]["set"] is True
    assert data["local"]["session_secret"]["set"] is True
    assert data["oidc"]["fields"]["enabled"]["set"] is True  # OIDC_ENABLED=false in test env
    assert data["oidc_enabled"] is False


def test_settings_update_without_csrf_is_rejected(client):
    # Login once for this test
    csrf = _get_login_csrf(client)
    login = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    response = client.put("/api/settings", json={"max_price": 70000}, cookies=login.cookies)
    assert response.status_code == 403


def test_login_page_sets_csrf_cookie(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "dca_csrf" in response.cookies
    assert response.cookies["dca_csrf"]


def test_login_without_csrf_token_is_rejected(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "testpass"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_login_with_invalid_csrf_token_is_rejected(client):
    client.get("/login")  # sets the csrf cookie
    response = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_login_csrf_token_rotated_after_success(client):
    csrf = _get_login_csrf(client)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "dca_csrf" in response.cookies
    assert response.cookies["dca_csrf"] != csrf


def test_settings_page_renders_subsections(auth_client):
    response = auth_client.get("/settings")
    assert response.status_code == 200
    assert "API Keys" in response.text
    assert "Authentication" in response.text
    assert "Local Admin" in response.text
    assert "Authentik (OIDC)" in response.text
    assert "web_auth_username" in response.text
    assert "oidc_issuer_url" in response.text


def test_alerts_page_renders_acknowledge_all(auth_client):
    response = auth_client.get("/alerts")
    assert response.status_code == 200
    assert "Acknowledge All" in response.text


def test_preflight_checks_endpoint(auth_client):
    response = auth_client.get("/api/preflight/checks")
    assert response.status_code == 200
    data = response.json()
    names = {c["name"] for c in data["checks"]}
    assert "kraken_minimum_permissions" in names
    assert "live_trading_enabled" in names
    assert all(c["enabled"] is True for c in data["checks"])
    assert data["disabled"] == []


def test_preflight_disabled_checks_roundtrip(auth_client):
    response = auth_client.put(
        "/api/settings",
        json={"preflight_disabled_checks": ["kraken_minimum_permissions", "telegram_configured"]},
    )
    assert response.status_code == 200

    response = auth_client.get("/api/preflight/checks")
    data = response.json()
    disabled = {c["name"] for c in data["checks"] if not c["enabled"]}
    assert disabled == {"kraken_minimum_permissions", "telegram_configured"}

    # Reflected in the settings read-back as well.
    response = auth_client.get("/api/settings")
    assert set(response.json()["preflight_disabled_checks"]) == disabled

    # Restore: empty list re-enables everything.
    response = auth_client.put("/api/settings", json={"preflight_disabled_checks": []})
    assert response.status_code == 200
    response = auth_client.get("/api/preflight/checks")
    assert all(c["enabled"] for c in response.json()["checks"])


def test_preflight_unknown_check_name_rejected(auth_client):
    response = auth_client.put(
        "/api/settings",
        json={"preflight_disabled_checks": ["not_a_real_check"]},
    )
    assert response.status_code == 422


def test_dashboard_renders_system_nav_group(auth_client):
    response = auth_client.get("/dashboard")
    assert response.status_code == 200
    assert "nav-system-toggle" in response.text
    assert "System" in response.text
    # Settings link is part of the nav (bottom of the navigation).
    assert response.text.index('href="/settings"') > response.text.index('href="/preflight"')


def test_logout_clears_session_and_csrf_cookies(auth_client: TestClient):
    # Ensure we have a session before logging out.
    assert "dca_session" in auth_client.cookies
    assert "dca_csrf" in auth_client.cookies
    response = auth_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    # The response should instruct the browser to clear both cookies.
    set_cookies = response.headers.get_list("set-cookie")
    cleared = [c for c in set_cookies if "Max-Age=0" in c or 'expires=Thu, 01 Jan 1970' in c]
    assert any("dca_session" in c for c in cleared)
    assert any("dca_csrf" in c for c in cleared)


def test_web_settings_save_local_admin(auth_client):
    # Env vars win over the store, so remove them to exercise the store path.
    os.environ.pop("WEB_UI_USERNAME", None)
    os.environ.pop("WEB_UI_PASSWORD_HASH", None)

    response = auth_client.put(
        "/api/settings/web",
        json={"username": "boss", "password": "super-secret-1"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["web"]["fields"]["username"]["value"] == "boss"
    # The raw password must never appear in the API response.
    assert "super-secret-1" not in response.text

    secrets_path = Path(os.environ["SECRETS_PATH"])
    saved = json.loads(secrets_path.read_text())
    assert saved["web_ui_username"] == "boss"
    stored_hash = saved["web_ui_password_hash"]
    assert stored_hash != "super-secret-1"
    assert bcrypt.checkpw("super-secret-1".encode(), stored_hash.encode())

    # Existing session stays valid after the change.
    assert auth_client.get("/api/status").status_code == 200

    # New credentials work for a fresh login.
    csrf = _get_login_csrf(auth_client)
    login = auth_client.post(
        "/login",
        data={"username": "boss", "password": "super-secret-1", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert login.status_code == 303


def test_web_settings_password_mismatch_rules(auth_client):
    os.environ.pop("WEB_UI_PASSWORD_HASH", None)
    # Password below the minimum length is rejected by the schema.
    response = auth_client.put("/api/settings/web", json={"password": "short"})
    assert response.status_code == 422
    # Empty update is rejected.
    response = auth_client.put("/api/settings/web", json={})
    assert response.status_code == 400


def test_web_settings_clear(auth_client):
    os.environ.pop("WEB_UI_USERNAME", None)
    auth_client.put("/api/settings/web", json={"username": "tempadmin"})
    response = auth_client.post("/api/settings/web/clear")
    assert response.status_code == 200
    saved = json.loads(Path(os.environ["SECRETS_PATH"]).read_text())
    assert "web_ui_username" not in saved
    assert "web_ui_password_hash" not in saved


def test_oidc_settings_save_and_clear(auth_client):
    os.environ.pop("OIDC_ENABLED", None)
    os.environ.pop("OIDC_ISSUER_URL", None)
    os.environ.pop("OIDC_CLIENT_ID", None)
    os.environ.pop("OIDC_CLIENT_SECRET", None)
    os.environ.pop("OIDC_REDIRECT_URI", None)

    response = auth_client.put(
        "/api/settings/oidc",
        json={
            "enabled": True,
            "issuer_url": "https://auth.example.com/application/o/bot/",
            "client_id": "my-client",
            "client_secret": "top-secret-value",
            "redirect_uri": "https://bot.example.com/auth/callback",
            "scopes": "openid email profile",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["oidc_enabled"] is True
    assert data["oidc"]["fields"]["issuer_url"]["value"] == "https://auth.example.com/application/o/bot/"
    # The raw client secret must never appear in the API response.
    assert "top-secret-value" not in response.text

    saved = json.loads(Path(os.environ["SECRETS_PATH"]).read_text())
    assert saved["oidc_enabled"] == "true"
    assert saved["oidc_client_secret"] == "top-secret-value"

    response = auth_client.post("/api/settings/oidc/clear")
    assert response.status_code == 200
    assert response.json()["oidc_enabled"] is False
    saved = json.loads(Path(os.environ["SECRETS_PATH"]).read_text())
    assert "oidc_client_id" not in saved
    assert "oidc_client_secret" not in saved


def test_oidc_enable_requires_issuer_and_client(auth_client):
    os.environ.pop("OIDC_ENABLED", None)
    os.environ.pop("OIDC_ISSUER_URL", None)
    os.environ.pop("OIDC_CLIENT_ID", None)
    response = auth_client.put("/api/settings/oidc", json={"enabled": True})
    assert response.status_code == 400


def test_alerts_acknowledge_all(auth_client):
    import contextlib

    alerts_path = Path("data/alerts.json")
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    alerts_path.write_text(json.dumps([
        {"id": "a1", "severity": "warning", "message": "m1", "acknowledged": False},
        {"id": "a2", "severity": "critical", "message": "m2", "acknowledged": False},
    ]))
    try:
        response = auth_client.post("/api/alerts/acknowledge-all")
        assert response.status_code == 200
        assert response.json()["acknowledged"] == 2

        response = auth_client.get("/api/alerts")
        assert response.json()["alerts"] == []

        # Already-acknowledged alerts are not counted again.
        response = auth_client.post("/api/alerts/acknowledge-all")
        assert response.json()["acknowledged"] == 0
    finally:
        with contextlib.suppress(OSError):
            alerts_path.unlink()
