"""Home-screen / PWA support (GitHub issue #19).

The manifest and icons must be reachable without a session — browsers fetch
them independently of (and before) any login cookie.
"""

from __future__ import annotations

import json
import os
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


def test_manifest_reachable_without_login(client):
    response = client.get("/static/manifest.webmanifest")
    assert response.status_code == 200
    data = json.loads(response.text)
    assert data["display"] == "standalone"
    assert data["name"] == "Kraken DCA Bot"
    assert data["start_url"] == "/"
    for icon in data["icons"]:
        assert icon["src"].startswith("/static/icons/")


def test_icons_reachable_without_login(client):
    for name in (
        "icon-192.png",
        "icon-512.png",
        "icon-512-maskable.png",
        "apple-touch-icon.png",
    ):
        response = client.get(f"/static/icons/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == "image/png"


def test_favicon_route_serves_icon(client):
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_login_page_has_home_screen_tags(client):
    text = client.get("/login").text
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in text
    assert '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">' in text
    assert '<meta name="theme-color" content="#0c0f18">' in text
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in text


def test_dashboard_page_has_home_screen_tags(auth_client):
    text = auth_client.get("/dashboard").text
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in text
    assert '<meta name="theme-color" content="#0c0f18">' in text
