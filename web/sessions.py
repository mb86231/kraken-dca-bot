"""Server-side session registry with revocation.

Sessions are still signed cookies, but the cookie value carries a session ID
(jti) that must exist in this registry. That makes sessions revocable:

* ``logout`` revokes the individual session, so a copied cookie stops
  working instead of living out its TTL.
* Credential changes (admin password set/changed/cleared) revoke every
  session, forcing re-authentication on all devices.

Storage is a cross-process locked JSON file (``bot.persistence.JsonFile``)
next to the secrets store, so sessions survive restarts. Expired entries are
pruned opportunistically on issue and validate.

The recent-authentication requirement for sensitive operations (credential
changes, live-trading toggle) is deliberately deferred: it needs a re-auth
UI flow and is tracked as a follow-up in the security backlog.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from bot.persistence import JsonFile, LockAcquisitionError

logger = logging.getLogger("dca_bot.web.sessions")


def _default_path() -> Path:
    """Resolve the registry path lazily so tests can isolate via env vars.

    ``SESSIONS_PATH`` wins explicitly; otherwise the file lives next to the
    secrets store (same directory, same volume, same lifecycle).
    """
    explicit = os.environ.get("SESSIONS_PATH", "").strip()
    if explicit:
        return Path(explicit)
    secrets_path = os.environ.get("SECRETS_PATH", "").strip()
    if secrets_path:
        return Path(secrets_path).parent / "sessions.json"
    return Path("data/sessions.json")


class SessionRegistry:
    """Cross-process safe store of active session IDs (jti -> user, expiry)."""

    def __init__(self, path: Path | None = None):
        self._fixed_path = path

    @property
    def _file(self) -> JsonFile:
        return JsonFile(self._fixed_path or _default_path())

    @staticmethod
    def _as_dict(data: object) -> dict:
        return data if isinstance(data, dict) else {}

    def issue(self, username: str, ttl_seconds: int) -> str:
        """Register a new session ID and return it."""
        jti = secrets.token_urlsafe(24)
        now = time.time()

        def transform(data: object) -> dict:
            sessions = {
                key: value
                for key, value in self._as_dict(data).items()
                if isinstance(value, dict) and float(value.get("exp", 0)) > now
            }
            sessions[jti] = {"u": username, "iat": now, "exp": now + ttl_seconds}
            return sessions

        self._file.update(transform)
        return jti

    def validate(self, jti: str) -> Optional[str]:
        """Return the session's username, or None when unknown/expired.

        Fails closed: any registry error (lock timeout, unreadable file)
        rejects the session rather than admitting it without a check.
        """
        if not jti:
            return None
        try:
            entry = self._as_dict(self._file.load()).get(jti)
        except (LockAcquisitionError, OSError) as exc:
            logger.warning("Session registry unreadable; failing closed: %s", exc)
            return None
        if not isinstance(entry, dict):
            return None
        if float(entry.get("exp", 0)) < time.time():
            return None
        username = str(entry.get("u") or "")
        return username or None

    def revoke(self, jti: str) -> None:
        """Revoke a single session ID (logout)."""
        if not jti:
            return
        self._file.update(lambda data: self._pop(self._as_dict(data), jti))

    def revoke_all(self) -> None:
        """Revoke every session (credential change)."""
        self._file.save({})

    @staticmethod
    def _pop(sessions: dict, jti: str) -> dict:
        sessions.pop(jti, None)
        return sessions


session_registry = SessionRegistry()
