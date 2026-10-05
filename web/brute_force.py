"""Brute-force login protection for the web dashboard."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from slowapi.util import get_remote_address


@dataclass
class _FailureRecord:
    count: int = 0
    first_failure_at: float = 0.0
    locked_until: float = 0.0


class BruteForceProtector:
    """Track failed login attempts by IP and by username.

    The current deployment is single-process, so an in-memory store is sufficient.
    A multi-process or multi-node deployment would need a shared store such as Redis.
    """

    def __init__(self) -> None:
        self.max_failures = int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
        self.lockout_seconds = float(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))
        self._failures: dict[str, _FailureRecord] = defaultdict(_FailureRecord)

    @staticmethod
    def _ip_key(request: Request) -> str:
        return f"ip:{get_remote_address(request)}"

    @staticmethod
    def _user_key(username: str) -> str:
        return f"user:{username.lower().strip()}"

    def is_locked_out(self, request: Request, username: str) -> bool:
        """Return True if the IP or username is currently locked out."""
        now = time.monotonic()
        for key in (self._ip_key(request), self._user_key(username)):
            record = self._failures[key]
            if record.locked_until and now < record.locked_until:
                return True
            if record.count >= self.max_failures:
                record.locked_until = now + self.lockout_seconds
                return True
        return False

    def record_failure(self, request: Request, username: str) -> None:
        """Increment failure counters for the IP and username."""
        now = time.monotonic()
        for key in (self._ip_key(request), self._user_key(username)):
            record = self._failures[key]
            if record.count == 0:
                record.first_failure_at = now
            record.count += 1
            if record.count >= self.max_failures:
                record.locked_until = now + self.lockout_seconds

    def record_success(self, request: Request, username: str) -> None:
        """Clear failure records after a successful login."""
        for key in (self._ip_key(request), self._user_key(username)):
            self._failures.pop(key, None)

    def clear(self) -> None:
        """Clear all lockout state. Intended for tests."""
        self._failures.clear()


brute_force_protector = BruteForceProtector()


def check_login_allowed(request: Request, username: str) -> None:
    """Raise 401 if the request is locked out; callers return a generic error."""
    if brute_force_protector.is_locked_out(request, username):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
