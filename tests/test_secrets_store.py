"""Tests for the dashboard-managed secrets store and credential config loading."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from bot.config import Config
from bot.secrets_store import SecretsStore


@pytest.fixture
def secrets_file(temp_dir: Path) -> Path:
    return temp_dir / "secrets.json"


@pytest.fixture
def store(secrets_file: Path) -> SecretsStore:
    return SecretsStore(filepath=secrets_file)


@pytest.fixture(autouse=True)
def _dummy_env_credentials(monkeypatch: pytest.MonkeyPatch):
    """Ensure Config can always load credentials; individual tests override."""
    monkeypatch.setenv("KRAKEN_API_KEY", "test-env-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "test-env-secret")


# ---------------------------------------------------------------------------
# SecretsStore
# ---------------------------------------------------------------------------

def test_roundtrip(store: SecretsStore, secrets_file: Path):
    store.save_exchange_credentials("test-key-123456", "test-secret-abcdef")
    key, secret = store.get_exchange_credentials()
    assert key == "test-key-123456"
    assert secret == "test-secret-abcdef"
    assert secrets_file.exists()


# Windows does not support Unix permission bits (chmod only toggles the
# read-only attribute), so the owner-only permission check is Unix-only.
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits not supported on Windows")
def test_file_permissions_are_owner_only(store: SecretsStore, secrets_file: Path):
    store.save_exchange_credentials("test-key-123456", "test-secret-abcdef")
    mode = stat.S_IMODE(secrets_file.stat().st_mode)
    assert mode == 0o600


def test_load_missing_file_returns_empty(store: SecretsStore):
    assert store.load() == {}
    assert store.get_exchange_credentials() == (None, None)


def test_load_invalid_json_returns_empty(store: SecretsStore, secrets_file: Path):
    secrets_file.write_text("{not json")
    assert store.load() == {}


def test_clear_removes_credentials(store: SecretsStore):
    store.save_exchange_credentials("test-key-123456", "test-secret-abcdef")
    assert store.clear_exchange_credentials() is True
    assert store.get_exchange_credentials() == (None, None)
    # Clearing again is a no-op, not an error.
    assert store.clear_exchange_credentials() is False


def test_clear_keeps_other_keys(store: SecretsStore, secrets_file: Path):
    secrets_file.write_text(json.dumps({"kraken_api_key": "k", "kraken_api_secret": "s", "other": "keep"}))
    store.clear_exchange_credentials()
    data = json.loads(secrets_file.read_text())
    assert data == {"other": "keep"}


def test_public_status_masks_key(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    store.save_exchange_credentials("abcdefgh12345678", "s" * 20)
    status = store.public_status()
    assert status["stored_key_set"] is True
    assert status["effective_source"] == "secrets_file"
    assert status["stored_key_masked"].endswith("5678")
    assert "abcdefgh" not in status["stored_key_masked"]
    assert status["stored_key_masked"].startswith("•")


def test_public_status_prefers_environment(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "env-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-secret")
    store.save_exchange_credentials("file-key-123456", "file-secret-123456")
    assert store.public_status()["effective_source"] == "environment"


def test_public_status_unset(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    assert store.public_status()["effective_source"] == "unset"


# ---------------------------------------------------------------------------
# Config credential loading: env vars take precedence over the secrets store
# ---------------------------------------------------------------------------

def _write_config(path: Path, extra: str = "") -> Path:
    path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000'
        + extra + "}"
    )
    return path


def test_config_uses_env_credentials(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "env-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-secret")
    monkeypatch.setenv("SECRETS_PATH", str(temp_dir / "secrets.json"))
    SecretsStore(filepath=temp_dir / "secrets.json").save_exchange_credentials("file-key-123456", "file-secret-123456")
    cfg = Config(config_path=_write_config(temp_dir / "config.json"))
    assert cfg.api_key == "env-key"
    assert cfg.api_secret == "env-secret"


def test_config_falls_back_to_secrets_store(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(temp_dir / "secrets.json"))
    SecretsStore(filepath=temp_dir / "secrets.json").save_exchange_credentials("file-key-123456", "file-secret-123456")
    cfg = Config(config_path=_write_config(temp_dir / "config.json"))
    assert cfg.api_key == "file-key-123456"
    assert cfg.api_secret == "file-secret-123456"


def test_config_requires_credentials_somewhere(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(temp_dir / "secrets.json"))
    with pytest.raises(Exception, match="api_key"):
        Config(config_path=_write_config(temp_dir / "config.json"))


# ---------------------------------------------------------------------------
# Live trading precedence: env var overrides config.json flag
# ---------------------------------------------------------------------------

def test_live_trading_defaults_to_false(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    cfg = Config(config_path=_write_config(temp_dir / "config.json"))
    assert cfg.live_trading_enabled is False


def test_live_trading_from_config_json(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    cfg = Config(config_path=_write_config(temp_dir / "config.json", ', "live_trading_enabled": true'))
    assert cfg.live_trading_enabled is True


def test_live_trading_env_overrides_config(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    cfg = Config(config_path=_write_config(temp_dir / "config.json", ', "live_trading_enabled": true'))
    assert cfg.live_trading_enabled is False


def test_live_trading_env_true_overrides_config(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    cfg = Config(config_path=_write_config(temp_dir / "config.json"))
    assert cfg.live_trading_enabled is True


def test_to_dict_reports_live_and_masks_credentials(temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "env-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "env-secret")
    cfg = Config(config_path=_write_config(temp_dir / "config.json"))
    data = cfg.to_dict(mask_secrets=True)
    assert data["live_trading_enabled"] is False
    assert data["api_key_set"] is True
    assert "env-key" not in json.dumps(data)


# ---------------------------------------------------------------------------
# Generic sections: resolve / save / clear / status
# ---------------------------------------------------------------------------

def test_resolve_env_wins_over_store(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token-123")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("telegram", {"bot_token": "stored-token-123456", "chat_id": " stored-chat "})
    values = store.resolve("telegram")
    assert values["bot_token"] == "env-token-123"
    assert values["chat_id"] == "stored-chat"


def test_resolve_empty_env_falls_back_to_store(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("telegram", {"bot_token": "stored-token-123456"})
    assert store.resolve("telegram")["bot_token"] == "stored-token-123456"


def test_resolve_missing_returns_none(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    values = store.resolve("telegram")
    assert values["bot_token"] is None
    assert values["chat_id"] is None


def test_resolve_enabled_normalized(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OIDC_ENABLED", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("oidc", {"enabled": "TRUE"})
    assert store.resolve("oidc")["enabled"] == "true"
    store.save_section("oidc", {"enabled": "off"})
    assert store.resolve("oidc")["enabled"] == "false"


def test_resolve_unknown_section_raises(store: SecretsStore):
    with pytest.raises(KeyError):
        store.resolve("nope")


def test_save_section_rejects_unknown_field(store: SecretsStore):
    with pytest.raises(KeyError):
        store.save_section("telegram", {"whatever": "x"})


def test_clear_section(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("WEB_UI_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("web", {"session_secret": "s" * 32, "password_hash": "h" * 20})
    assert store.clear_section("web") is True
    assert store.clear_section("web") is False
    assert store.resolve("web")["session_secret"] is None


def test_section_status_masks_secrets(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    for var in ("OIDC_ENABLED", "OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI", "OIDC_SCOPES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("oidc", {
        "enabled": "true",
        "issuer_url": "https://auth.example.com/application/o/crypto-bot/",
        "client_id": "client-id-1234567890",
        "client_secret": "super-secret-value-0123",
    })
    status = store.section_status("oidc")
    assert status["effective_source"] == "secrets_file"
    assert status["fields"]["enabled"]["value"] == "true"
    assert status["fields"]["issuer_url"]["value"].startswith("https://")
    assert "super-secret-value" not in json.dumps(status)
    assert status["fields"]["client_secret"]["value"].endswith("0123")
    assert status["fields"]["client_secret"]["source"] == "secrets_file"


# ---------------------------------------------------------------------------
# Consumers fall back to the store: Notifier, AuthManager, OIDCProvider
# ---------------------------------------------------------------------------

def test_notifier_falls_back_to_store(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("telegram", {"bot_token": "stored-token-123456", "chat_id": "12345"})
    from bot.notifier import Notifier
    notifier = Notifier()
    assert notifier.enabled is True
    assert notifier.bot_token == "stored-token-123456"
    assert notifier.chat_id == "12345"


def test_auth_manager_falls_back_to_store(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    from web.auth import AuthManager
    monkeypatch.delenv("WEB_UI_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("web", {"password_hash": "bcrypt-hash-value-123456", "session_secret": "s" * 40})
    manager = AuthManager()
    assert manager.password_hash == "bcrypt-hash-value-123456"
    assert manager.session_secret == "s" * 40


def test_auth_manager_env_wins_over_store(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    from web.auth import AuthManager
    monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "env-hash-123456789")
    monkeypatch.setenv("SESSION_SECRET", "e" * 40)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("web", {"password_hash": "stored-hash-12345678", "session_secret": "s" * 40})
    manager = AuthManager()
    assert manager.password_hash == "env-hash-123456789"
    assert manager.session_secret == "e" * 40


def test_oidc_provider_falls_back_to_store(store: SecretsStore, monkeypatch: pytest.MonkeyPatch):
    from web.oidc import OIDCProvider
    for var in ("OIDC_ENABLED", "OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI", "OIDC_SCOPES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SECRETS_PATH", str(store.filepath))
    store.save_section("oidc", {
        "enabled": "true",
        "issuer_url": "https://auth.example.com/application/o/crypto-bot/",
        "client_id": "my-client-id-123456789",
        "client_secret": "my-client-secret-123456789",
        "redirect_uri": "https://bot.example.com/auth/callback",
        "scopes": "openid email profile",
    })
    provider = OIDCProvider()
    assert provider.enabled is True
    assert provider.client_id == "my-client-id-123456789"
    assert provider.client_secret == "my-client-secret-123456789"
    assert provider.redirect_uri == "https://bot.example.com/auth/callback"
    assert provider.issuer_url.endswith("/")
