"""First-run setup flow: token-gated admin bootstrap.

When no admin password is configured anywhere (env var or secrets store),
the login page shows a one-time setup form guarded by a setup token that is
printed to the container logs. After a successful setup the normal login
form is shown and the setup route is closed.
"""

from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient

from web.app import app
from web.auth import AuthManager, first_run_setup


@pytest.fixture
def setup_client(temp_dir: Path, monkeypatch):
    """Client in an unconfigured state: no admin password anywhere."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("WEB_UI_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("WEB_UI_USERNAME", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-32-bytes-long-value")
    monkeypatch.setenv("SECRETS_PATH", str(temp_dir / "secrets.json"))
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "true")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("OIDC_ENABLED", "false")

    config_path = temp_dir / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"poll_interval_seconds": 300, "buy_hour": 8}'
    )
    (temp_dir / "transactions.json").write_text("[]")

    from bot.config import Config
    from bot.state import BotState, RuntimeOverrides
    from bot.store import TransactionStore

    app.state.config = Config(config_path=config_path)
    app.state.store = TransactionStore(filepath=temp_dir / "transactions.json")
    app.state.bot_state = BotState()
    app.state.overrides = RuntimeOverrides(filepath=temp_dir / "runtime_overrides.json")

    # Reset the module-level singleton between tests.
    first_run_setup.complete()
    with TestClient(app) as client:
        yield client
    first_run_setup.complete()


def _setup_token() -> str:
    token = first_run_setup.ensure_token()
    assert token, "setup token should be armed when no password is configured"
    return token


def _post_setup(client: TestClient, **overrides):
    client.get("/login")  # sets the CSRF cookie like a real browser visit
    csrf = str(client.cookies.get("dca_csrf") or "")
    data = {
        "csrf_token": csrf,
        "setup_token": _setup_token(),
        "username": "admin",
        "password": "super-secret-1",
        "password_confirm": "super-secret-1",
    }
    data.update(overrides)
    return client.post("/setup", data=data, follow_redirects=False)


def test_login_page_shows_setup_form_when_unconfigured(setup_client: TestClient):
    response = setup_client.get("/login")
    assert response.status_code == 200
    assert "First-run setup" in response.text
    assert 'action="/setup"' in response.text
    assert 'action="/login"' not in response.text


def test_setup_token_is_announced_in_logs(setup_client: TestClient):
    from unittest import mock

    first_run_setup.complete()
    with mock.patch("web.auth.logger") as fake_logger:
        token = first_run_setup.ensure_token()
    assert token
    fake_logger.warning.assert_called_once()
    assert token in fake_logger.warning.call_args[0]


def test_setup_rejects_wrong_token(setup_client: TestClient):
    response = _post_setup(setup_client, setup_token="wrong-token")
    assert response.status_code == 400
    assert "Invalid setup token" in response.text
    assert first_run_setup.required()


def test_setup_rejects_short_password(setup_client: TestClient):
    response = _post_setup(setup_client, password="short12", password_confirm="short12")
    assert response.status_code == 400
    assert "at least 8 characters" in response.text
    assert first_run_setup.required()


def test_setup_rejects_mismatched_passwords(setup_client: TestClient):
    response = _post_setup(setup_client, password_confirm="different-pass-1")
    assert response.status_code == 400
    assert "do not match" in response.text
    assert first_run_setup.required()


def test_setup_success_creates_admin_and_closes_setup(setup_client: TestClient, temp_dir: Path):
    response = _post_setup(setup_client, username="markus")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert "dca_session" in response.cookies

    # Credentials persisted in the secrets store (env untouched).
    import json

    stored = json.loads((temp_dir / "secrets.json").read_text())
    assert stored["web_ui_username"] == "markus"
    assert bcrypt.checkpw("super-secret-1".encode(), stored["web_ui_password_hash"].encode())

    # Setup flow closed; login form is back.
    assert not first_run_setup.required()
    page = setup_client.get("/login")
    assert 'action="/login"' in page.text
    assert "First-run setup" not in page.text

    # New credentials actually work for login.
    csrf = str(setup_client.cookies.get("dca_csrf") or "")
    login = setup_client.post(
        "/login",
        data={"csrf_token": csrf, "username": "markus", "password": "super-secret-1"},
        follow_redirects=False,
    )
    assert login.status_code == 303


def test_setup_route_closed_after_completion(setup_client: TestClient):
    assert _post_setup(setup_client).status_code == 303
    setup_client.get("/login")
    csrf = str(setup_client.cookies.get("dca_csrf") or "")
    again = setup_client.post(
        "/setup",
        data={
            "csrf_token": csrf,
            "setup_token": "any-token",
            "username": "intruder",
            "password": "whatever-123",
            "password_confirm": "whatever-123",
        },
        follow_redirects=False,
    )
    assert again.status_code == 400
    assert "already been completed" in again.text


def test_setup_not_offered_when_env_password_set(setup_client: TestClient, monkeypatch):
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", bcrypt.hashpw("x".encode(), bcrypt.gensalt()).decode())
    first_run_setup.complete()
    page = setup_client.get("/login")
    assert 'action="/login"' in page.text
    assert first_run_setup.ensure_token() is None


def test_session_secret_auto_generated_and_persisted(temp_dir: Path, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(temp_dir / "secrets.json"))
    manager = AuthManager()
    secret1 = manager.session_secret
    assert len(secret1) >= 32
    # Persisted: a fresh manager resolves the same value from the store.
    manager2 = AuthManager()
    assert manager2.session_secret == secret1
    import json

    stored = json.loads((temp_dir / "secrets.json").read_text())
    assert stored["session_secret"] == secret1
