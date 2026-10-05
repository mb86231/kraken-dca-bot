"""Tests for the OIDC / SSO authentication flow."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import bcrypt
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt

from web.app import app
from web.oidc import OIDCProvider, oidc_provider


ISSUER_URL = "https://authentik.example.com/application/o/dca-bot-staging/"
CLIENT_ID = "dca-bot-staging"
CLIENT_SECRET = "super-secret-client-secret"
REDIRECT_URI = "https://staging.example.com/auth/callback"


def _set_test_env(temp_dir: Path, oidc_enabled: bool = True):
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

    os.environ["OIDC_ENABLED"] = "true" if oidc_enabled else "false"
    os.environ["OIDC_ISSUER_URL"] = ISSUER_URL
    os.environ["OIDC_CLIENT_ID"] = CLIENT_ID
    os.environ["OIDC_CLIENT_SECRET"] = CLIENT_SECRET
    os.environ["OIDC_REDIRECT_URI"] = REDIRECT_URI
    os.environ["OIDC_SCOPES"] = "openid email profile"

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
def oidc_client(temp_dir: Path):
    _set_test_env(temp_dir, oidc_enabled=True)
    oidc_provider._discovery = None  # type: ignore[assignment]
    oidc_provider._jwks = None  # type: ignore[assignment]
    oidc_provider._discovered_at = None  # type: ignore[assignment]
    with TestClient(app) as c:
        yield c


@pytest.fixture
def disabled_client(temp_dir: Path):
    _set_test_env(temp_dir, oidc_enabled=False)
    oidc_provider._discovery = None  # type: ignore[assignment]
    oidc_provider._jwks = None  # type: ignore[assignment]
    oidc_provider._discovered_at = None  # type: ignore[assignment]
    with TestClient(app) as c:
        yield c


def _generate_rsa_key_and_jwk(kid: str = "test-key") -> tuple[Any, Dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    numbers = public_key.public_numbers()

    def b64url(value: int) -> str:
        byte_length = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")

    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "n": b64url(numbers.n),
        "e": b64url(numbers.e),
    }
    return private_key, jwk


def _sign_id_token(private_key: Any, claims: Dict[str, Any], kid: str = "test-key") -> str:
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return jose_jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


class MockResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP error {self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self):
        return self._json


class MockAsyncClient:
    def __init__(self, responses: Dict[str, Any]):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url: str, **kwargs):
        response = self.responses.get(url)
        if callable(response):
            response = response(url)
        return response

    async def post(self, url: str, **kwargs):
        response = self.responses.get(url)
        if callable(response):
            response = response(url, kwargs)
        return response


def _mock_httpx(responses: Dict[str, Any]):
    def _factory(**kwargs):
        return MockAsyncClient(responses)

    return patch("web.oidc.httpx.AsyncClient", side_effect=_factory)


def _default_discovery() -> Dict[str, Any]:
    return {
        "issuer": ISSUER_URL,
        "authorization_endpoint": f"{ISSUER_URL}authorize/",
        "token_endpoint": f"{ISSUER_URL}token/",
        "jwks_uri": f"{ISSUER_URL}jwks/",
    }


@pytest.fixture
def mock_oidc_provider():
    private_key, jwk = _generate_rsa_key_and_jwk()
    discovery = _default_discovery()

    def _token_response(url: str, kwargs: Dict[str, Any]) -> MockResponse:
        data = kwargs.get("data", {})
        assert data.get("grant_type") == "authorization_code"
        assert data.get("code") == "test-code"
        assert data.get("client_id") == CLIENT_ID
        assert data.get("code_verifier")
        return MockResponse(
            json_data={
                "access_token": "test-access-token",
                "id_token": _sign_id_token(
                    private_key,
                    {
                        "iss": ISSUER_URL,
                        "aud": CLIENT_ID,
                        "sub": "user-123",
                        "email": "user@example.com",
                        "preferred_username": "testuser",
                        "nonce": "test-nonce",
                        "iat": int(datetime.now(timezone.utc).timestamp()),
                        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                    },
                ),
            }
        )

    responses = {
        discovery["jwks_uri"]: MockResponse(json_data={"keys": [jwk]}),
        f"{ISSUER_URL}.well-known/openid-configuration": MockResponse(json_data=discovery),
        discovery["token_endpoint"]: _token_response,
    }
    return private_key, discovery, responses


def test_oidc_login_page_shows_button_when_enabled(oidc_client: TestClient):
    response = oidc_client.get("/login")
    assert response.status_code == 200
    assert "Sign in with Authentik" in response.text


def test_oidc_login_page_no_button_when_disabled(disabled_client: TestClient):
    response = disabled_client.get("/login")
    assert response.status_code == 200
    assert "Sign in with Authentik" not in response.text


def test_oidc_login_disabled_returns_404(disabled_client: TestClient):
    response = disabled_client.get("/login/oidc")
    assert response.status_code == 404


def test_oidc_login_redirect(oidc_client: TestClient, mock_oidc_provider):
    _, _, responses = mock_oidc_provider
    with _mock_httpx(responses):
        response = oidc_client.get("/login/oidc", follow_redirects=False)
    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER_URL}authorize/")
    assert f"client_id={CLIENT_ID}" in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert "state=" in location
    assert "nonce=" in location


def test_oidc_callback_validates_token_and_creates_session(oidc_client: TestClient, mock_oidc_provider):
    _, _, responses = mock_oidc_provider

    # Step 1: start the login flow to populate the session
    with patch.object(oidc_provider, "_generate_nonce", return_value="test-nonce"), _mock_httpx(responses):
        start = oidc_client.get("/login/oidc", follow_redirects=False)
    assert start.status_code == 307

    # Extract the state and nonce from the query string so we can simulate the callback
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(start.headers["location"])
    params = parse_qs(parsed.query)
    state = params["state"][0]

    # Step 2: simulate Authentik's callback
    with _mock_httpx(responses):
        callback = oidc_client.get(
            "/auth/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/dashboard"
    assert "dca_session" in callback.cookies
    assert "dca_csrf" in callback.cookies


def test_oidc_callback_bad_state_is_rejected(oidc_client: TestClient, mock_oidc_provider):
    _, _, responses = mock_oidc_provider

    with _mock_httpx(responses):
        oidc_client.get("/login/oidc", follow_redirects=False)

    with _mock_httpx(responses):
        callback = oidc_client.get(
            "/auth/callback",
            params={"code": "test-code", "state": "wrong-state"},
        )
    assert callback.status_code == 403


def test_oidc_callback_missing_code_or_state(oidc_client: TestClient, mock_oidc_provider):
    _, _, responses = mock_oidc_provider

    with _mock_httpx(responses):
        oidc_client.get("/login/oidc", follow_redirects=False)

    with _mock_httpx(responses):
        callback = oidc_client.get("/auth/callback", params={"state": "some-state"})
    assert callback.status_code == 400


def test_oidc_callback_rejects_authentik_error(oidc_client: TestClient, mock_oidc_provider):
    _, _, responses = mock_oidc_provider

    with _mock_httpx(responses):
        oidc_client.get("/login/oidc", follow_redirects=False)

    with _mock_httpx(responses):
        callback = oidc_client.get(
            "/auth/callback",
            params={"error": "access_denied", "error_description": "User denied access"},
        )
    assert callback.status_code == 400
    assert "User denied access" in callback.text


def test_oidc_callback_rejects_invalid_nonce(oidc_client: TestClient, mock_oidc_provider):
    private_key, discovery, responses = mock_oidc_provider

    # Replace the token endpoint to return a token with a different nonce
    def _bad_token_response(url: str, kwargs: Dict[str, Any]) -> MockResponse:
        return MockResponse(
            json_data={
                "id_token": _sign_id_token(
                    private_key,
                    {
                        "iss": ISSUER_URL,
                        "aud": CLIENT_ID,
                        "sub": "user-123",
                        "email": "user@example.com",
                        "nonce": "wrong-nonce",
                        "iat": int(datetime.now(timezone.utc).timestamp()),
                        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                    },
                ),
            }
        )

    responses[discovery["token_endpoint"]] = _bad_token_response  # type: ignore[index]

    with patch.object(oidc_provider, "_generate_nonce", return_value="test-nonce"), _mock_httpx(responses):
        start = oidc_client.get("/login/oidc", follow_redirects=False)

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(start.headers["location"])
    state = parse_qs(parsed.query)["state"][0]

    with _mock_httpx(responses):
        callback = oidc_client.get(
            "/auth/callback",
            params={"code": "test-code", "state": state},
        )
    assert callback.status_code == 403


def test_oidc_extract_username_prefers_preferred_username():
    provider = OIDCProvider()
    assert provider.extract_username({"sub": "sub", "email": "a@b.com", "preferred_username": "bob"}) == "bob"


def test_oidc_extract_username_falls_back_to_email():
    provider = OIDCProvider()
    assert provider.extract_username({"sub": "sub", "email": "a@b.com"}) == "a@b.com"


def test_oidc_extract_username_falls_back_to_sub():
    provider = OIDCProvider()
    assert provider.extract_username({"sub": "sub"}) == "sub"


def test_local_password_login_still_works_with_oidc_enabled(oidc_client: TestClient):
    login_page = oidc_client.get("/login")
    assert login_page.status_code == 200
    csrf = str(oidc_client.cookies.get("dca_csrf") or "")
    response = oidc_client.post(
        "/login",
        data={"username": "admin", "password": "testpass", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "dca_session" in response.cookies


def _sign_id_token_with_header(private_key: Any, claims: Dict[str, Any], kid: str | None, alg: str = "RS256") -> str:
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    headers: Dict[str, Any] = {}
    if kid is not None:
        headers["kid"] = kid
    return jose_jwt.encode(claims, private_pem, algorithm=alg, headers=headers)


def _build_token_response(private_key: Any, kid: str | None, alg: str = "RS256") -> MockResponse:
    claims = {
        "iss": ISSUER_URL,
        "aud": CLIENT_ID,
        "sub": "user-123",
        "email": "user@example.com",
        "nonce": "test-nonce",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    return MockResponse(json_data={"id_token": _sign_id_token_with_header(private_key, claims, kid, alg)})


def test_oidc_unknown_kid_is_rejected(oidc_client: TestClient):
    """A token signed with a key ID not present in the JWKS must fail closed."""
    private_key, jwk = _generate_rsa_key_and_jwk(kid="published-key")
    discovery = _default_discovery()
    responses = {
        discovery["jwks_uri"]: MockResponse(json_data={"keys": [jwk]}),
        f"{ISSUER_URL}.well-known/openid-configuration": MockResponse(json_data=discovery),
        discovery["token_endpoint"]: lambda _url, _kwargs: _build_token_response(private_key, kid="unknown-key"),
    }
    with patch.object(oidc_provider, "_generate_nonce", return_value="test-nonce"), _mock_httpx(responses):
        start = oidc_client.get("/login/oidc", follow_redirects=False)
    assert start.status_code == 307

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(start.headers["location"])
    state = parse_qs(parsed.query)["state"][0]

    with _mock_httpx(responses):
        callback = oidc_client.get(
            "/auth/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
    assert callback.status_code == 401
    assert "unknown key" in callback.text.lower()


def test_oidc_ambiguous_jwks_without_kid_is_rejected(oidc_client: TestClient):
    """A token without a kid must be rejected when the JWKS contains multiple keys."""
    private_key1, jwk1 = _generate_rsa_key_and_jwk(kid="key-1")
    _private_key2, jwk2 = _generate_rsa_key_and_jwk(kid="key-2")
    discovery = _default_discovery()
    responses = {
        discovery["jwks_uri"]: MockResponse(json_data={"keys": [jwk1, jwk2]}),
        f"{ISSUER_URL}.well-known/openid-configuration": MockResponse(json_data=discovery),
        discovery["token_endpoint"]: lambda _url, _kwargs: _build_token_response(private_key1, kid=None),
    }
    with patch.object(oidc_provider, "_generate_nonce", return_value="test-nonce"), _mock_httpx(responses):
        start = oidc_client.get("/login/oidc", follow_redirects=False)
    assert start.status_code == 307

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(start.headers["location"])
    state = parse_qs(parsed.query)["state"][0]

    with _mock_httpx(responses):
        callback = oidc_client.get(
            "/auth/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
    assert callback.status_code == 401
    assert "ambiguous" in callback.text.lower()


def test_oidc_jwks_rotation_with_new_kid_is_accepted(oidc_client: TestClient):
    """When the provider rotates to a new key, a token signed with the new kid validates."""
    private_key, jwk = _generate_rsa_key_and_jwk(kid="rotated-key")
    discovery = _default_discovery()
    responses = {
        discovery["jwks_uri"]: MockResponse(json_data={"keys": [jwk]}),
        f"{ISSUER_URL}.well-known/openid-configuration": MockResponse(json_data=discovery),
        discovery["token_endpoint"]: lambda _url, _kwargs: _build_token_response(private_key, kid="rotated-key"),
    }
    with patch.object(oidc_provider, "_generate_nonce", return_value="test-nonce"), _mock_httpx(responses):
        start = oidc_client.get("/login/oidc", follow_redirects=False)
    assert start.status_code == 307

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(start.headers["location"])
    state = parse_qs(parsed.query)["state"][0]

    with _mock_httpx(responses):
        callback = oidc_client.get(
            "/auth/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/dashboard"
