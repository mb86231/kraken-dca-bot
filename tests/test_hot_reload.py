"""Hot-reload tests: settings changes apply to the running bot without a restart."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import bcrypt
import pytest
from fastapi.testclient import TestClient

from web.app import app


# ---------------------------------------------------------------------------
# bot/core.py
# ---------------------------------------------------------------------------

def test_reload_api_client_swaps_client_and_executor(monkeypatch):
    from bot.core import KrakenDCA

    created = []

    class FakeKraken:
        def __init__(self, key, secret):
            self.key, self.secret = key, secret
            created.append(self)

    monkeypatch.setattr("bot.core.KrakenAPI", FakeKraken)
    monkeypatch.setattr("bot.core.is_demo_mode", lambda: False)

    bot = KrakenDCA.__new__(KrakenDCA)
    bot.config = SimpleNamespace(api_key="k1", api_secret="s1")
    bot.order_executor = SimpleNamespace(api=object())

    bot.reload_api_client()
    assert len(created) == 1
    assert bot.api is bot.order_executor.api
    assert bot.api.key == "k1"

    bot.config = SimpleNamespace(api_key="k2", api_secret="s2")
    bot.reload_api_client()
    assert len(created) == 2
    assert bot.api.key == "k2"
    assert bot.order_executor.api is bot.api


def test_reload_api_client_demo_mode(monkeypatch):
    from bot.core import KrakenDCA

    class FakeDemo:
        def __init__(self, key, secret):
            self.key, self.secret = key, secret

    monkeypatch.setattr("bot.core.DemoKrakenAPI", FakeDemo)
    monkeypatch.setattr("bot.core.is_demo_mode", lambda: True)

    bot = KrakenDCA.__new__(KrakenDCA)
    bot.config = SimpleNamespace(api_key="k", api_secret="s")
    bot.order_executor = SimpleNamespace(api=None)

    bot.reload_api_client()
    assert isinstance(bot.api, FakeDemo)
    assert bot.order_executor.api is bot.api


# ---------------------------------------------------------------------------
# Web layer: endpoints hot-reload the attached bot
# ---------------------------------------------------------------------------

class _FakeUpdateChecker:
    """Records construction/startup; never touches the network."""

    instances: list["_FakeUpdateChecker"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        _FakeUpdateChecker.instances.append(self)

    def start_background(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _make_bot_stub() -> SimpleNamespace:
    calls: list[str] = []
    stub = SimpleNamespace(
        _calls=calls,
        reload_api_client=lambda: calls.append("reload_api_client"),
    )
    return stub


@pytest.fixture
def hot_client(tmp_path: Path, monkeypatch):
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
    os.environ["SECRETS_PATH"] = str(tmp_path / "secrets.json")
    os.environ["HEARTBEAT_FILE"] = str(tmp_path / "heartbeat.json")
    os.environ["BACKUP_DIR"] = str(tmp_path / "backups")

    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000, '
        '"update_check_enabled": true, "update_check_interval_hours": 24}'
    )
    (tmp_path / "transactions.json").write_text("[]")

    from bot.config import Config
    from bot.order_execution import OrderAttemptStore
    from bot.state import BotState, RuntimeOverrides
    from bot.store import TransactionStore

    app.state.config = Config(config_path=config_path)
    app.state.store = TransactionStore(filepath=tmp_path / "transactions.json")
    app.state.attempt_store = OrderAttemptStore(filepath=tmp_path / "order_attempts.json")
    app.state.bot_state = BotState()
    app.state.overrides = RuntimeOverrides(filepath=tmp_path / "runtime_overrides.json")
    app.state.bot = _make_bot_stub()

    _FakeUpdateChecker.instances.clear()
    monkeypatch.setattr("web.app.UpdateChecker", _FakeUpdateChecker)
    monkeypatch.setattr("web.routers.api.UpdateChecker", _FakeUpdateChecker)

    from web.oidc import oidc_provider

    oidc_provider._discovery = None
    oidc_provider._jwks = None
    oidc_provider._discovered_at = None

    with TestClient(app) as client:
        # Login and attach the CSRF header for mutating calls.
        client.get("/login")
        csrf = str(client.cookies.get("dca_csrf") or "")
        response = client.post(
            "/login",
            data={"username": "admin", "password": "testpass", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        client.headers["X-CSRF-Token"] = response.cookies.get("dca_csrf") or ""
        yield client

    app.state.bot = None


def test_settings_save_applies_changes_without_restart(hot_client: TestClient):
    response = hot_client.put(
        "/api/settings",
        json={
            "poll_interval_seconds": 600,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requires_restart"] is False
    assert "applied" in body["message"]
    # Loop-relevant values are reloaded into the shared config.
    assert app.state.config.poll_interval_seconds == 600


def test_settings_save_rebuilds_update_checker_when_schedule_changes(hot_client: TestClient):
    assert len(_FakeUpdateChecker.instances) == 1  # built by the lifespan
    lifespan_checker = _FakeUpdateChecker.instances[0]
    assert lifespan_checker.started

    response = hot_client.put("/api/settings", json={"update_check_interval_hours": 12})
    assert response.status_code == 200

    assert len(_FakeUpdateChecker.instances) == 2
    new_checker = _FakeUpdateChecker.instances[1]
    assert new_checker.kwargs["interval_hours"] == 12
    assert new_checker.started
    assert lifespan_checker.stopped
    assert app.state.update_checker is new_checker


def test_settings_save_without_update_fields_keeps_checker(hot_client: TestClient):
    count = len(_FakeUpdateChecker.instances)
    response = hot_client.put("/api/settings", json={"poll_interval_seconds": 300})
    assert response.status_code == 200
    assert len(_FakeUpdateChecker.instances) == count


def test_exchange_save_hot_reloads_api_client(hot_client: TestClient):
    bot = app.state.bot
    bot._calls.clear()
    response = hot_client.put(
        "/api/settings/exchange",
        json={"api_key": "new-key-12345", "api_secret": "new-secret-12345"},
    )
    assert response.status_code == 200
    assert "reload_api_client" in bot._calls
    assert "automatically" in response.json()["message"]


def test_exchange_clear_hot_reloads_api_client(hot_client: TestClient):
    bot = app.state.bot
    bot._calls.clear()
    response = hot_client.post("/api/settings/exchange/clear")
    assert response.status_code == 200
    assert "reload_api_client" in bot._calls
