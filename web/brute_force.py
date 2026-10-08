"""Brute-force login protection for the web dashboard."""

from __future__ import annotations

import os
import time
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

    Lockout semantics:

    - Failures only count within ``failure_window_seconds``; older failures
      decay, so a stale count can never lock an account on its own.
    - When a lockout expires the record is reset, so the account owner can
      log in again — the lockout must not renew itself indefinitely.
    - Failures recorded *during* an active lockout do not extend it.
    - The tracked-key map is pruned so unauthenticated probing of random
      usernames cannot grow memory without bound.
    """

    def __init__(self) -> None:
        self.max_failures = int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
        self.lockout_seconds = float(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))
        self.failure_window_seconds = float(
            os.environ.get("LOGIN_FAILURE_WINDOW_SECONDS", str(self.lockout_seconds))
        )
        self._max_tracked = int(os.environ.get("LOGIN_MAX_TRACKED_KEYS", "10000"))
        self._failures: dict[str, _FailureRecord] = {}

    @staticmethod
    def _ip_key(request: Request) -> str:
        return f"ip:{get_remote_address(request)}"

    @staticmethod
    def _user_key(username: str) -> str:
        return f"user:{username.lower().strip()}"

    def _is_stale(self, record: _FailureRecord, now: float) -> bool:
        """A record is stale once it is no longer locked and its failures
        (if any) have all decayed out of the counting window."""
        if record.locked_until and now < record.locked_until:
            return False
        if record.count == 0:
            return True
        return (now - record.first_failure_at) > self.failure_window_seconds

    def _prune(self, now: float) -> None:
        if len(self._failures) <= self._max_tracked:
            return
        stale_keys = [
            key for key, record in self._failures.items()
            if self._is_stale(record, now)
        ]
        for key in stale_keys:
            del self._failures[key]

    def is_locked_out(self, request: Request, username: str) -> bool:
        """Return True if the IP or username is currently locked out."""
        now = time.monotonic()
        for key in (self._ip_key(request), self._user_key(username)):
            record = self._failures.get(key)
            if record is None:
                continue
            if record.locked_until:
                if now < record.locked_until:
                    return True
                # Lockout expired: reset the record so the next login attempt
                # starts clean instead of renewing the lockout forever.
                record.count = 0
                record.first_failure_at = 0.0
                record.locked_until = 0.0
                continue
            if record.count and (now - record.first_failure_at) > self.failure_window_seconds:
                # Failures have decayed; drop the stale count.
                record.count = 0
                record.first_failure_at = 0.0
                continue
            if record.count >= self.max_failures:
                record.locked_until = now + self.lockout_seconds
                return True
        return False

    def record_failure(self, request: Request, username: str) -> None:
        """Increment failure counters for the IP and username."""
        now = time.monotonic()
        for key in (self._ip_key(request), self._user_key(username)):
            record = self._failures.get(key)
            if record is None:
                record = _FailureRecord()
                self._failures[key] = record
            if record.locked_until and now < record.locked_until:
                # Already locked; do not extend the lockout from behind it.
                continue
            if record.count == 0 or (now - record.first_failure_at) > self.failure_window_seconds:
                record.first_failure_at = now
                record.count = 1
            else:
                record.count += 1
            if record.count >= self.max_failures:
                record.locked_until = now + self.lockout_seconds
        self._prune(now)

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
