"""Rate/cost limiting, circuit breaker, and bounded background execution.

All limits are per-day (UTC). When a budget is exhausted the advisor
stops issuing calls — the base bot strategy is never affected.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, TypeVar

T = TypeVar("T")


class DailyBudget:
    """Per-day caps on requests, tokens, and estimated cost."""

    def __init__(
        self,
        request_limit: int = 50,
        token_limit: int = 100_000,
        cost_limit: float = 1.0,
    ):
        self.request_limit = max(0, request_limit)
        self.token_limit = max(0, token_limit)
        self.cost_limit = max(0.0, cost_limit)
        self._day = self._today()
        self.requests_used = 0
        self.tokens_used = 0
        self.cost_used = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _rollover(self) -> None:
        day = self._today()
        if day != self._day:
            self._day = day
            self.requests_used = 0
            self.tokens_used = 0
            self.cost_used = 0.0

    def allow(self) -> bool:
        with self._lock:
            self._rollover()
            return self.requests_used < self.request_limit

    def record(
        self,
        *,
        tokens: int | None = None,
        cost: float | None = None,
    ) -> bool:
        """Record usage; returns False when any budget is now exhausted."""
        with self._lock:
            self._rollover()
            self.requests_used += 1
            if tokens:
                self.tokens_used += tokens
            if cost:
                self.cost_used += cost
            return (
                self.requests_used < self.request_limit
                and self.tokens_used < self.token_limit
                and self.cost_used < self.cost_limit
            )

    def exhausted_reason(self) -> str | None:
        with self._lock:
            self._rollover()
            if self.requests_used >= self.request_limit:
                return "daily request limit reached"
            if self.tokens_used >= self.token_limit:
                return "daily token limit reached"
            if self.cost_used >= self.cost_limit:
                return "daily cost limit reached"
            return None

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            self._rollover()
            return {
                "requests_used": self.requests_used,
                "requests_limit": self.request_limit,
                "tokens_used": self.tokens_used,
                "tokens_limit": self.token_limit,
                "cost_used": round(self.cost_used, 6),
                "cost_limit": self.cost_limit,
            }


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-opens after the
    cooldown and closes again on the first success."""

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 300.0):
        self.threshold = max(1, threshold)
        self.cooldown_seconds = max(1.0, cooldown_seconds)
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                return True  # half-open: let one request through
            return False

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None


class BoundedExecutor:
    """Background execution with bounded parallelism.

    Advisor work always runs off the trading path; unexpected exceptions in
    a task are swallowed into the Future and surfaced via the advisor's
    error tracking, never propagated into bot code.
    """

    def __init__(self, max_workers: int = 2):
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="advisor",
        )

    def submit(self, fn: Callable[..., T], *args: object, **kwargs: object) -> Future[T]:
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)
