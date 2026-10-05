"""Production security validation and security headers middleware."""

from __future__ import annotations

import logging
import os

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from bot.secrets_store import SecretsStore

logger = logging.getLogger("dca_bot.web.security")


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "production").lower() == "production"


def _rate_limiting_disabled() -> bool:
    """Return True if any supported variable explicitly disables rate limiting."""
    from web.rate_limit import rate_limiting_enabled

    return not rate_limiting_enabled()


def validate_production_security() -> None:
    """Fail startup if production is missing security-critical configuration."""
    if not _is_production():
        return

    # Stable session secret: environment variable or secrets store. When
    # neither is set, one is generated and persisted at first use so a fresh
    # container can boot without manual secrets configuration.
    session_secret = os.environ.get("SESSION_SECRET", "").strip()
    if not session_secret:
        session_secret = (SecretsStore().resolve("web").get("session_secret") or "").strip()
    if session_secret and len(session_secret) < 16:
        raise RuntimeError("SESSION_SECRET must be at least 16 characters long")

    # Password login is always available as a fallback, even when OIDC is
    # enabled. When no password is configured anywhere, the first-run setup
    # flow on the login page takes over (token printed in the container logs).
    password_hash = os.environ.get("WEB_UI_PASSWORD_HASH", "").strip()
    if not password_hash:
        password_hash = (SecretsStore().resolve("web").get("password_hash") or "").strip()
    if not password_hash:
        logger.warning(
            "WEB_UI_PASSWORD_HASH is not set — first-run setup is active. "
            "The setup token is printed in the container logs; create the "
            "admin account via the login page, then this warning disappears."
        )

    # Cookies should be marked Secure in production. The dashboard is intended
    # to run behind an HTTPS reverse proxy; an explicit false is honoured for
    # trusted plain-HTTP networks (the localhost/LAN quick-start) but must be a
    # deliberate choice — an unset variable is rejected.
    secure_cookie = os.environ.get("WEB_UI_SECURE_COOKIE", "").strip().lower()
    if secure_cookie == "":
        raise RuntimeError(
            "WEB_UI_SECURE_COOKIE must be set explicitly when APP_ENV=production "
            "(true behind an HTTPS proxy; false only on a trusted plain-HTTP network)"
        )
    if secure_cookie not in ("true", "1", "yes", "on"):
        logger.warning(
            "WEB_UI_SECURE_COOKIE=false in production — session cookies are not "
            "marked Secure. Only use this on a trusted plain-HTTP network; put "
            "the dashboard behind an HTTPS reverse proxy otherwise."
        )

    # Rate limiting is mandatory in production.
    if _rate_limiting_disabled():
        raise RuntimeError(
            "Rate limiting must be enabled when APP_ENV=production "
            "(set RATE_LIMIT_ENABLED=true and do not set DISABLE_RATE_LIMIT=true)"
        )


def _static_path(path: str) -> bool:
    return path.startswith("/static/") or path == "/favicon.ico"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every HTTP response.

    FastAPI is the source of truth for these headers. Nginx should only add
    transport-level headers (Strict-Transport-Security) and should not duplicate
    the headers below to avoid conflicting or duplicated values.
    """

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        )

        # HSTS is only meaningful when the request was actually served over HTTPS.
        forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if forwarded_proto == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        # Prevent caching of authenticated/sensitive content. Static assets are
        # allowed to cache because their URLs are versioned by filename.
        path = request.url.path
        if not _static_path(path):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, private"
            )

        return response
