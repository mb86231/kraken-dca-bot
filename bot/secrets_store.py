"""Storage for dashboard-managed secrets.

Secrets (exchange credentials, Telegram tokens, web session keys, OIDC client
settings) are kept out of ``config.json`` (which may be backed up, shared, or
committed) and instead live in a dedicated JSON file with 0600 permissions.
Environment variables always take precedence over file-stored values at load
time, so an operator-managed env file remains authoritative.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Dict, Optional, Tuple

from bot.utils import atomic_write_json, mask_secret

DEFAULT_SECRETS_PATH = Path("data/secrets.json")

_KEY = "kraken_api_key"
_SECRET = "kraken_api_secret"

# Section-based secrets managed via the dashboard. Each field maps to an
# environment variable (authoritative when set and non-empty) and a key in the
# secrets file (fallback when the env var is unset or empty).
SECTION_ENV_VARS: Dict[str, Dict[str, str]] = {
    "telegram": {
        "bot_token": "TELEGRAM_BOT_TOKEN",
        "chat_id": "TELEGRAM_CHAT_ID",
        "allowed_user_ids": "TELEGRAM_ALLOWED_USER_IDS",
    },
    "web": {
        "username": "WEB_UI_USERNAME",
        "session_secret": "SESSION_SECRET",
        "password_hash": "WEB_UI_PASSWORD_HASH",
    },
    "oidc": {
        "enabled": "OIDC_ENABLED",
        "issuer_url": "OIDC_ISSUER_URL",
        "client_id": "OIDC_CLIENT_ID",
        "client_secret": "OIDC_CLIENT_SECRET",
        "redirect_uri": "OIDC_REDIRECT_URI",
        "scopes": "OIDC_SCOPES",
        "allowed_subjects": "OIDC_ALLOWED_SUBJECTS",
    },
}

SECTION_STORE_KEYS: Dict[str, Dict[str, str]] = {
    "telegram": {
        "bot_token": "telegram_bot_token",
        "chat_id": "telegram_chat_id",
        "allowed_user_ids": "telegram_allowed_user_ids",
    },
    "web": {
        "username": "web_ui_username",
        "session_secret": "session_secret",
        "password_hash": "web_ui_password_hash",
    },
    "oidc": {
        "enabled": "oidc_enabled",
        "issuer_url": "oidc_issuer_url",
        "client_id": "oidc_client_id",
        "client_secret": "oidc_client_secret",
        "redirect_uri": "oidc_redirect_uri",
        "scopes": "oidc_scopes",
        "allowed_subjects": "oidc_allowed_subjects",
    },
}

# Values that are safe to expose (not masked) in status output.
_UNMASKED_FIELDS = {
    "enabled",
    "username",
    "issuer_url",
    "client_id",
    "redirect_uri",
    "scopes",
    "chat_id",
    "allowed_subjects",
    "allowed_user_ids",
}


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


class SecretsStore:
    """Read and write dashboard-managed secrets as a permission-locked JSON file."""

    def __init__(self, filepath: str | Path | None = None):
        if filepath is None:
            env_path = os.environ.get("SECRETS_PATH")
            filepath = env_path if env_path else DEFAULT_SECRETS_PATH
        self.filepath = Path(filepath)

    def load(self) -> dict:
        """Return the stored secrets dict, or an empty dict if unreadable."""
        try:
            data = json.loads(self.filepath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        atomic_write_json(self.filepath, data)
        try:
            os.chmod(self.filepath, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Best effort: restrictive permissions are important but must never
            # crash the request that saved the credentials.
            pass

    # ------------------------------------------------------------------
    # Exchange credentials (Kraken)
    # ------------------------------------------------------------------

    def get_exchange_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Return (api_key, api_secret) stored in the file, or (None, None)."""
        data = self.load()
        key = data.get(_KEY)
        secret = data.get(_SECRET)
        return (
            key if isinstance(key, str) and key else None,
            secret if isinstance(secret, str) and secret else None,
        )

    def save_exchange_credentials(self, api_key: str, api_secret: str) -> None:
        """Persist Kraken credentials, replacing any previously stored values."""
        data = self.load()
        data[_KEY] = api_key
        data[_SECRET] = api_secret
        self._save(data)

    def clear_exchange_credentials(self) -> bool:
        """Remove stored Kraken credentials. Returns True if anything was removed."""
        data = self.load()
        removed = _KEY in data or _SECRET in data
        data.pop(_KEY, None)
        data.pop(_SECRET, None)
        if removed or self.filepath.exists():
            self._save(data)
        return removed

    def public_status(self) -> dict:
        """Return a masked, API-safe view of the credential state."""
        key, secret = self.get_exchange_credentials()
        env_key = bool(os.environ.get("KRAKEN_API_KEY"))
        env_secret = bool(os.environ.get("KRAKEN_API_SECRET"))
        if env_key:
            source = "environment"
        elif key:
            source = "secrets_file"
        else:
            source = "unset"
        return {
            "file_path": str(self.filepath),
            "file_exists": self.filepath.exists(),
            "stored_key_set": bool(key),
            "stored_secret_set": bool(secret),
            "stored_key_masked": mask_secret(key) if key else "",
            "env_key_set": env_key,
            "env_secret_set": env_secret,
            "effective_source": source,
        }

    # ------------------------------------------------------------------
    # Generic sections: telegram, web, oidc
    # ------------------------------------------------------------------

    def resolve(self, section: str) -> Dict[str, Optional[str]]:
        """Return effective values for a section.

        Per field: a non-empty environment variable wins; otherwise the stored
        file value is used. Missing values come back as None. The ``enabled``
        field is normalized to a boolean.
        """
        if section not in SECTION_ENV_VARS:
            raise KeyError(f"unknown secrets section: {section}")
        stored = self.load()
        result: Dict[str, Optional[str]] = {}
        for field, env_var in SECTION_ENV_VARS[section].items():
            env_value = os.environ.get(env_var, "").strip()
            if env_value:
                value: Optional[str] = env_value
            else:
                raw = stored.get(SECTION_STORE_KEYS[section][field])
                value = raw.strip() if isinstance(raw, str) and raw.strip() else None
            if field == "enabled":
                result[field] = "true" if _as_bool(value) else "false"
            else:
                result[field] = value
        return result

    def save_section(self, section: str, values: Dict[str, str]) -> None:
        """Persist the given fields of a section, replacing previous stored values."""
        if section not in SECTION_STORE_KEYS:
            raise KeyError(f"unknown secrets section: {section}")
        data = self.load()
        for field, value in values.items():
            if field not in SECTION_STORE_KEYS[section]:
                raise KeyError(f"unknown field {field!r} for section {section!r}")
            data[SECTION_STORE_KEYS[section][field]] = str(value).strip()
        self._save(data)

    def clear_section(self, section: str) -> bool:
        """Remove all stored values of a section. Returns True if anything was removed."""
        if section not in SECTION_STORE_KEYS:
            raise KeyError(f"unknown secrets section: {section}")
        data = self.load()
        removed = False
        for store_key in SECTION_STORE_KEYS[section].values():
            if store_key in data:
                removed = True
            data.pop(store_key, None)
        if removed:
            self._save(data)
        return removed

    def section_status(self, section: str) -> dict:
        """Return a masked, API-safe status view of a section."""
        if section not in SECTION_ENV_VARS:
            raise KeyError(f"unknown secrets section: {section}")
        stored = self.load()
        fields = {}
        any_env = False
        any_stored = False
        for field, env_var in SECTION_ENV_VARS[section].items():
            env_value = os.environ.get(env_var, "").strip()
            store_key = SECTION_STORE_KEYS[section][field]
            raw = stored.get(store_key)
            stored_value = raw.strip() if isinstance(raw, str) and raw.strip() else None
            if env_value:
                any_env = True
            if stored_value:
                any_stored = True
            value = env_value or stored_value
            if field in _UNMASKED_FIELDS:
                shown = value
            else:
                shown = mask_secret(value) if value else ""
            fields[field] = {
                "set": bool(value),
                "source": "environment" if env_value else ("secrets_file" if stored_value else "unset"),
                "value": shown,
            }
        return {
            "section": section,
            "effective_source": "environment" if any_env else ("secrets_file" if any_stored else "unset"),
            "fields": fields,
        }
