"""Authentication, sessions, and CSRF protection for the web dashboard."""

from __future__ import annotations

import functools
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, TimestampSigner

from bot.secrets_store import SecretsStore
from web.brute_force import brute_force_protector, check_login_allowed
from web.flags import env_flag
from web.oidc import oidc_provider
from web.sessions import session_registry

logger = logging.getLogger("dca_bot.web.auth")


class AuthManager:
    """Simple session-based auth manager with optional OIDC fallback.

    Credentials are read from environment variables on each access so that
    tests can override them without module re-import.
    """

    SESSION_COOKIE = "dca_session"
    CSRF_COOKIE = "dca_csrf"

    def __init__(self):
        self._signer: Optional[TimestampSigner] = None

    @property
    def oidc_enabled(self) -> bool:
        """Whether OIDC authentication is configured and enabled."""
        return oidc_provider.enabled

    @property
    def username(self) -> str:
        name = os.environ.get("WEB_UI_USERNAME", "").strip()
        if not name:
            name = (SecretsStore().resolve("web").get("username") or "").strip()
        return name or "admin"

    @property
    def local_password_configured(self) -> bool:
        """Whether a local admin password is configured (env or secrets store)."""
        if os.environ.get("WEB_UI_PASSWORD_HASH", "").strip():
            return True
        return bool((SecretsStore().resolve("web").get("password_hash") or "").strip())

    @property
    def password_hash(self) -> str:
        pwd = os.environ.get("WEB_UI_PASSWORD_HASH", "").strip()
        if not pwd:
            pwd = (SecretsStore().resolve("web").get("password_hash") or "").strip()
        if pwd:
            return pwd
        # No password configured anywhere: the first-run setup flow intercepts
        # before any login attempt. Return an unmatchable hash as a safety net.
        return bcrypt.hashpw(secrets.token_hex(32).encode(), bcrypt.gensalt()).decode()

    @property
    def session_secret(self) -> str:
        secret = os.environ.get("SESSION_SECRET", "").strip()
        if not secret:
            section = SecretsStore().resolve("web")
            secret = (section.get("session_secret") or "").strip()
            if not secret:
                # First boot: generate a stable secret and persist it in the
                # secrets store so sessions survive restarts.
                secret = secrets.token_hex(32)
                SecretsStore().save_section("web", {"session_secret": secret})
                logger.info("SESSION_SECRET was not set; generated one and stored it in the secrets store")
        return secret

    @property
    def session_ttl_hours(self) -> int:
        return int(os.environ.get("SESSION_TTL_HOURS", "24"))

    @property
    def signer(self) -> TimestampSigner:
        if self._signer is None:
            self._signer = TimestampSigner(self.session_secret)
        return self._signer

    def verify_password(self, password: str) -> bool:
        """Check a password against the stored bcrypt hash."""
        return bcrypt.checkpw(password.encode(), self.password_hash.encode())

    @property
    def secure_cookies(self) -> bool:
        """Whether session/CSRF cookies get the Secure flag.

        Uses the shared boolean truth table (web.flags), so validation and
        every cookie setter agree on true/1/yes/on vs false/0/no/off.
        """
        return env_flag("WEB_UI_SECURE_COOKIE")

    def create_session(self, response: Response, username: str) -> None:
        """Create a signed session cookie backed by a revocable server-side ID."""
        ttl_seconds = int(timedelta(hours=self.session_ttl_hours).total_seconds())
        jti = session_registry.issue(username, ttl_seconds)
        value = f"{username}:{jti}:{datetime.now(timezone.utc).isoformat()}"
        signed = self.signer.sign(value).decode("utf-8")
        response.set_cookie(
            self.SESSION_COOKIE,
            signed,
            httponly=True,
            secure=self.secure_cookies,
            samesite="lax",
            max_age=ttl_seconds,
        )

    def clear_session(self, response: Response) -> None:
        """Clear the session cookie."""
        response.delete_cookie(self.SESSION_COOKIE)

    def revoke_session(self, request: Request) -> None:
        """Revoke the request's server-side session ID (logout).

        A copied cookie stops working instead of living out its TTL.
        """
        cookie = request.cookies.get(self.SESSION_COOKIE)
        if not cookie:
            return
        try:
            unsigned = self.signer.unsign(cookie)
        except BadSignature:
            return
        parts = unsigned.decode("utf-8").split(":")
        if len(parts) >= 2:
            session_registry.revoke(parts[1])

    def clear_csrf_cookie(self, response: Response) -> None:
        """Clear the CSRF cookie with the same attributes used to set it."""
        response.delete_cookie(
            self.CSRF_COOKIE,
            path="/",
            secure=self.secure_cookies,
            samesite="lax",
        )

    def get_session_username(self, request: Request) -> Optional[str]:
        """Validate session cookie and return username.

        The signed cookie must carry a session ID that is still registered
        server-side, so revoked (logged-out or credential-changed) sessions
        stop working immediately. Legacy cookies without a session ID are
        rejected; the user simply logs in again.
        """
        cookie = request.cookies.get(self.SESSION_COOKIE)
        if not cookie:
            return None
        try:
            unsigned = self.signer.unsign(cookie, max_age=int(timedelta(hours=self.session_ttl_hours).total_seconds()))
            parts = unsigned.decode("utf-8").split(":")
            if len(parts) < 2 or not parts[1]:
                return None
            username, jti = parts[0], parts[1]
        except (BadSignature, ValueError):
            return None
        if session_registry.validate(jti) != username:
            return None
        return username

    def generate_csrf_token(self) -> str:
        """Generate a new CSRF token."""
        return secrets.token_urlsafe(32)

    def validate_csrf(self, request: Request, token: Optional[str] = None) -> None:
        """Validate CSRF token from header or form against cookie."""
        expected = request.cookies.get(self.CSRF_COOKIE)
        provided = token or request.headers.get("X-CSRF-Token")
        if not expected or not provided:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing")
        if not hmac.compare_digest(expected, provided):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token invalid")

    def set_csrf_cookie(self, response: Response, token: Optional[str] = None) -> str:
        """Set a CSRF cookie and return the token.

        When `token` is given (e.g. already rendered into a form), that exact
        value is written to the cookie so form and cookie always match.
        Otherwise a fresh token is generated.
        """
        if token is None:
            token = self.generate_csrf_token()
        response.set_cookie(
            self.CSRF_COOKIE,
            token,
            httponly=False,
            secure=self.secure_cookies,
            samesite="lax",
            max_age=int(timedelta(hours=self.session_ttl_hours).total_seconds()),
        )
        return token


auth_manager = AuthManager()


def require_auth(request: Request):
    """FastAPI dependency that requires a valid session.

    API routes get a 401 JSON response; page routes get a redirect to /login.
    """
    username = auth_manager.get_session_username(request)
    if not username:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"}, detail="Not authenticated")
    return username


def login_post(request: Request, username: str, password: str, response: Response) -> bool:
    """Authenticate and set session cookie."""
    # Generic lockout check. Do not reveal whether username or password was wrong.
    check_login_allowed(request, username)
    if username != auth_manager.username:
        brute_force_protector.record_failure(request, username)
        return False
    if not auth_manager.verify_password(password):
        brute_force_protector.record_failure(request, username)
        return False
    brute_force_protector.record_success(request, username)
    auth_manager.create_session(response, username)
    return True


def logout(request: Request, response: Response) -> None:
    auth_manager.revoke_session(request)
    auth_manager.clear_session(response)
    auth_manager.clear_csrf_cookie(response)


def revoke_all_sessions() -> None:
    """Invalidate every dashboard session (called on credential changes)."""
    session_registry.revoke_all()


def csrf_protect(func):
    """Decorator for route handlers that require CSRF validation."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        request: Optional[Request] = kwargs.get("request")
        if request is None:
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break
        if request is None:
            raise HTTPException(status_code=500, detail="Request object not found")
        token = None
        try:
            token = (await request.form()).get("csrf_token") or request.headers.get("X-CSRF-Token")
        except Exception:
            token = request.headers.get("X-CSRF-Token")
        auth_manager.validate_csrf(request, token)
        return await func(*args, **kwargs)
    return wrapper


class FirstRunSetup:
    """One-time admin bootstrap shown when no local password is configured.

    A setup token is generated lazily, printed to the container log exactly
    once, and must be entered together with the new admin credentials on the
    login page. After a successful setup the token is discarded and the
    normal login form is shown. Environment variables always win: if
    ``WEB_UI_PASSWORD_HASH`` is set, the setup flow never activates.
    """

    def __init__(self) -> None:
        self._token: Optional[str] = None

    def required(self) -> bool:
        return not auth_manager.local_password_configured

    def ensure_token(self) -> Optional[str]:
        """Create and announce the setup token if setup is still pending."""
        if not self.required():
            return None
        if self._token is None:
            self._token = secrets.token_urlsafe(24)
            logger.warning(
                "========================================================================\n"
                "FIRST-RUN SETUP: no admin password configured.\n"
                "Open the dashboard login page and enter this setup token together\n"
                "with your new admin credentials:\n\n"
                "    %s\n\n"
                "The token is valid until the admin account is created.\n"
                "========================================================================",
                self._token,
            )
        return self._token

    def verify(self, provided: str) -> bool:
        if not self._token or not provided:
            return False
        return hmac.compare_digest(self._token, provided.strip())

    def complete(self) -> None:
        self._token = None


first_run_setup = FirstRunSetup()
