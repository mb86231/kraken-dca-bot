"""Rate-limiting configuration and helpers for the web dashboard."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status
from slowapi.util import get_remote_address


# Boolean values that explicitly enable or disable rate limiting.
_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def _is_true(value: str) -> bool:
    return value.strip().lower() in _TRUE_VALUES


def _is_false(value: str) -> bool:
    return value.strip().lower() in _FALSE_VALUES


def rate_limiting_enabled() -> bool:
    """Return False only when rate limiting is explicitly disabled.

    This is the single source of truth for the runtime rate-limit state. An
    unset variable keeps rate limiting enabled.
    """
    if _is_true(os.environ.get("DISABLE_RATE_LIMIT", "")):
        return False
    rate_limit_enabled = os.environ.get("RATE_LIMIT_ENABLED", "")
    if rate_limit_enabled == "":
        return True
    return _is_true(rate_limit_enabled)


def rate_limiting_disabled_for_current_env() -> bool:
    """Return True only when rate limiting may be safely disabled.

    Rate limiting is a safety control in production and staging. It may only be
    disabled in development or test environments.
    """
    if rate_limiting_enabled():
        return False
    app_env = os.environ.get("APP_ENV", "production").lower()
    return app_env in {"development", "test"}


@dataclass
class _Bucket:
    requests: list[float] = field(default_factory=list)


class RateLimitConfig:
    """Environment-driven rate limit configuration."""

    def __init__(self) -> None:
        self.enabled = rate_limiting_enabled()
        self.login = os.environ.get("RATE_LIMIT_LOGIN", "5/minute")
        self.api = os.environ.get("RATE_LIMIT_API", "60/minute")
        self.manual_buy = os.environ.get("RATE_LIMIT_MANUAL_BUY", "3/minute")
        self.settings = os.environ.get("RATE_LIMIT_SETTINGS", "10/minute")


class RateLimiter:
    """Simple in-memory sliding-window rate limiter.

    This is intentionally single-process. The dashboard currently runs as a single
    Python process; a distributed deployment would need Redis or similar.
    """

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket)
        self._cleanup_interval = 60.0
        self._last_cleanup = time.monotonic()

    @staticmethod
    def _parse(limit_string: str) -> tuple[int, float]:
        try:
            count_str, period = limit_string.split("/", 1)
            count = int(count_str.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid rate limit format: {limit_string!r}") from exc

        unit = period.strip().lower()
        if unit in ("s", "sec", "second", "seconds"):
            window = 1.0
        elif unit in ("m", "min", "minute", "minutes"):
            window = 60.0
        elif unit in ("h", "hr", "hour", "hours"):
            window = 3600.0
        elif unit in ("d", "day", "days"):
            window = 86400.0
        else:
            raise ValueError(f"Unsupported rate limit unit: {unit!r}")
        return count, window

    def _cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        cutoff = now - 86400.0
        for key, bucket in list(self._buckets.items()):
            bucket.requests = [t for t in bucket.requests if t > cutoff]
            if not bucket.requests:
                del self._buckets[key]
        self._last_cleanup = now

    @staticmethod
    def disabled() -> bool:
        """Return True when rate limiting is safely disabled (development/test only)."""
        return rate_limiting_disabled_for_current_env()

    def is_allowed(self, key: str, limit_string: str) -> bool:
        if not self._config.enabled or self.disabled():
            return True
        self._cleanup()
        count, window = self._parse(limit_string)
        bucket_key = f"{limit_string}:{key}"
        now = time.monotonic()
        bucket = self._buckets[bucket_key]
        bucket.requests = [t for t in bucket.requests if now - t < window]
        if len(bucket.requests) >= count:
            return False
        bucket.requests.append(now)
        return True

    def clear(self) -> None:
        """Clear all rate-limit state. Intended for tests."""
        self._buckets.clear()


def _rate_limit_key(request: Request) -> str:
    return get_remote_address(request)


def _raise_if_limited(request: Request, limit_string: str) -> None:
    key = _rate_limit_key(request)
    if not rate_limiter.is_allowed(key, limit_string):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )


def login_rate_limit(request: Request) -> None:
    """Dependency for login/OIDC initiation routes."""
    _raise_if_limited(request, rate_limit_config.login)


def api_rate_limit(request: Request) -> None:
    """Dependency for general authenticated API routes."""
    _raise_if_limited(request, rate_limit_config.api)


def manual_buy_rate_limit(request: Request) -> None:
    """Dependency for manual buy route."""
    _raise_if_limited(request, rate_limit_config.manual_buy)


def settings_rate_limit(request: Request) -> None:
    """Dependency for settings/backup/restore routes."""
    _raise_if_limited(request, rate_limit_config.settings)


rate_limit_config = RateLimitConfig()
rate_limiter = RateLimiter(rate_limit_config)
