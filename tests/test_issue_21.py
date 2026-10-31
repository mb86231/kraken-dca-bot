"""Regression tests for issue #21 (GitHub).

Five findings from a robustness review of the dashboard:

1. HIGH — "0 to disable" limits were stored as a real limit of 0, which
   blocked every buy (a real incident for a test user).
2. MEDIUM — POST /api/preflight/run executed the blocking preflight inline
   and froze the event loop for the whole dashboard.
3. MEDIUM — the dashboard balance cache held its state lock while calling
   Kraken, serializing every dashboard reader behind a slow API.
4. LOW — the unrealized-P/L chart was hard-coded red regardless of sign.
5. LOW — range charts valued only the range-filtered transactions, so the
   7d/30d/90d charts started at zero and disagreed with the KPI card.
6. BUG — ``get_monthly_spent`` raised "day is out of range for month" for
   deposit_day values the previous month does not have (e.g. 31 after a
   30-day month), taking every budget check down on those days.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, cast

import bcrypt
import httpx
import pytest
from pydantic import ValidationError

from bot.config import Config
from bot.core import KrakenDCA
from bot.order_execution import OrderAttemptStore
from bot.state import BotState, RuntimeOverrides
from bot.store import Transaction, TransactionStore
from web.routers.api import _BalanceCache, _market_valuation_series
from web.schemas import SettingsUpdate


# -----------------------------------------------------------------------------
# 1. HIGH — zero/empty limits mean "disabled", not "limit of 0"
# -----------------------------------------------------------------------------


def _write_limits_config(
    tmp_path: Path, *, max_price: Any, max_monthly_amount: Any
) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "trading_pair": "XBTCHF",
                "deposit_day": 24,
                "crypto_amount": 0.0001,
                "dip_threshold_percent": 5.0,
                "poll_interval_seconds": 300,
                "buy_hour": 8,
                "dip_buy_cooldown_hours": 2.0,
                "max_price": max_price,
                "max_monthly_amount": max_monthly_amount,
            }
        )
    )
    return config_path


def _set_demo_env(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")


@pytest.mark.parametrize(
    "raw",
    [0, 0.0, -1, -0.5, "", None, "abc"],
)
def test_non_positive_limits_normalize_to_disabled(tmp_path: Path, monkeypatch, raw: Any):
    _set_demo_env(monkeypatch)
    config = Config(
        config_path=_write_limits_config(
            tmp_path, max_price=raw, max_monthly_amount=raw
        )
    )
    assert config.max_price is None
    assert config.max_monthly_amount is None


def test_positive_limits_are_kept(tmp_path: Path, monkeypatch):
    _set_demo_env(monkeypatch)
    config = Config(
        config_path=_write_limits_config(
            tmp_path, max_price=65000, max_monthly_amount="10000"
        )
    )
    assert config.max_price == 65000.0
    assert config.max_monthly_amount == 10000.0


@pytest.mark.parametrize("raw", [0, 0.0, -5, ""])
def test_settings_update_zero_means_disabled(raw: Any):
    # The strategy form labels both fields "0 to disable"; persisting 0 would
    # store a real limit of 0 and block every buy (issue #21).
    # (kwargs splat: mypy without the pydantic plugin treats Field(None, ...)
    # fields as required, production calls the model the same way)
    update = SettingsUpdate(**{"max_price": raw, "max_monthly_amount": raw})
    dumped = update.model_dump(exclude_unset=True)
    assert dumped["max_price"] is None
    assert dumped["max_monthly_amount"] is None


def test_settings_update_keeps_positive_limit():
    update = SettingsUpdate(**{"max_price": 65000, "max_monthly_amount": 10000})
    dumped = update.model_dump(exclude_unset=True)
    assert dumped["max_price"] == 65000
    assert dumped["max_monthly_amount"] == 10000


def test_settings_update_rejects_non_numeric_limit():
    with pytest.raises(ValidationError):
        SettingsUpdate(**{"max_price": "abc"})


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class _FakeAPI:
    def __init__(self, price: float):
        self.price = price

    def get_ticker(self, pair: str) -> float:
        return self.price

    def get_balance(self) -> dict[str, float]:
        return {"ZCHF": 100000.0, "CHF": 100000.0}


def _capture_alerts(monkeypatch) -> list[str]:
    alerts: list[str] = []
    monkeypatch.setattr(
        "bot.core.create_alert",
        lambda message, severity="warning", source="bot": alerts.append(message),
    )
    return alerts


def _make_bot(
    tmp_path: Path, monkeypatch, *, max_price: Any, max_monthly_amount: Any
) -> KrakenDCA:
    config_path = _write_limits_config(
        tmp_path, max_price=max_price, max_monthly_amount=max_monthly_amount
    )
    _set_demo_env(monkeypatch)
    config = Config(config_path=config_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    bot = KrakenDCA(config=config, store=store, state=state)
    bot.store.clear()
    api = _FakeAPI(price=80000.0)
    bot.api = cast(Any, api)
    bot.order_executor.api = cast(Any, api)
    bot.notifier = cast(Any, _FakeNotifier())
    bot.order_executor.attempt_store = OrderAttemptStore(
        filepath=tmp_path / "order_attempts.json"
    )
    return bot


def test_buy_executes_with_legacy_zero_limits(tmp_path: Path, monkeypatch):
    # A config.json saved with the old code ("0 to disable" persisted as 0)
    # must self-heal on load and let the buy through.
    bot = _make_bot(tmp_path, monkeypatch, max_price=0, max_monthly_amount=0)
    alerts = _capture_alerts(monkeypatch)
    monkeypatch.setattr(sys, "stdout", StringIO())

    bot.execute_buy(strategy="scheduled")

    assert bot.store.get_transaction_count("XBTCHF") == 1
    assert not alerts


def test_buy_still_blocked_by_real_price_limit(tmp_path: Path, monkeypatch):
    bot = _make_bot(tmp_path, monkeypatch, max_price=1.0, max_monthly_amount=0)
    alerts = _capture_alerts(monkeypatch)
    monkeypatch.setattr(sys, "stdout", StringIO())

    bot.execute_buy(strategy="scheduled")

    assert bot.store.get_transaction_count("XBTCHF") == 0
    assert any("above max_price" in a for a in alerts)


# -----------------------------------------------------------------------------
# 2. MEDIUM — POST /api/preflight/run must not block the event loop
# -----------------------------------------------------------------------------


class _InstantResult:
    def to_dict(self) -> dict:
        return {"status": "ok", "checks": []}


class _SlowPreflight:
    """Stand-in for ProductionPreflight whose run() blocks like Kraken does."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def run(self) -> _InstantResult:
        time.sleep(2.0)
        return _InstantResult()


def _set_web_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("WEB_UI_USERNAME", "admin")
    monkeypatch.setenv(
        "WEB_UI_PASSWORD_HASH",
        bcrypt.hashpw("testpass".encode(), bcrypt.gensalt()).decode(),
    )
    monkeypatch.setenv("SESSION_SECRET", "test-secret-32-bytes-long-value")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("WEB_UI_SECURE_COOKIE", "false")
    monkeypatch.setenv("DISABLE_RATE_LIMIT", "true")
    monkeypatch.setenv("OIDC_ENABLED", "false")
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000}'
    )
    transactions_path = tmp_path / "transactions.json"
    transactions_path.write_text("[]")

    from web.app import app

    app.state.config = Config(config_path=config_path)
    app.state.store = TransactionStore(filepath=transactions_path)
    app.state.attempt_store = OrderAttemptStore(filepath=tmp_path / "order_attempts.json")
    app.state.bot_state = BotState()
    app.state.overrides = RuntimeOverrides(filepath=tmp_path / "runtime_overrides.json")


def test_preflight_run_does_not_block_event_loop(tmp_path: Path, monkeypatch):
    from web.app import app
    from web.oidc import oidc_provider

    _set_web_env(monkeypatch, tmp_path)
    oidc_provider._discovery = None
    oidc_provider._jwks = None
    oidc_provider._discovered_at = None
    monkeypatch.setattr("web.routers.api.ProductionPreflight", _SlowPreflight)
    monkeypatch.setattr("web.routers.api.write_preflight_result", lambda *a, **k: None)

    async def scenario() -> tuple[float, int, int]:
        transport = httpx.ASGITransport(app=cast(Any, app))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            login_page = await ac.get("/login")
            assert login_page.status_code == 200
            csrf = ac.cookies.get("dca_csrf") or ""
            login = await ac.post(
                "/login",
                data={"username": "admin", "password": "testpass", "csrf_token": csrf},
                follow_redirects=False,
            )
            assert login.status_code == 303
            headers = {"X-CSRF-Token": ac.cookies.get("dca_csrf") or ""}

            preflight_task = asyncio.create_task(
                ac.post("/api/preflight/run", headers=headers)
            )
            await asyncio.sleep(0.2)  # let the preflight reach its blocking run()
            start = time.perf_counter()
            status = await ac.get("/api/status")
            elapsed = time.perf_counter() - start
            preflight = await preflight_task
            return elapsed, status.status_code, preflight.status_code

    elapsed, status_code, preflight_code = asyncio.run(scenario())

    assert preflight_code == 200
    assert status_code == 200
    # If run() executed inline on the event loop the status request would only
    # be processed after the full 2 s sleep.
    assert elapsed < 1.0


# -----------------------------------------------------------------------------
# 3. MEDIUM — balance cache: concurrent readers must not queue on Kraken
# -----------------------------------------------------------------------------


class _FakeSlowBalance:
    """Records call count/params and answers slowly, like a rate-limited API."""

    def __init__(self, delay: float = 0.6) -> None:
        self.delay = delay
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    def get_balance(self, **kwargs: Any) -> dict[str, float]:
        self.calls += 1
        self.kwargs = dict(kwargs)
        time.sleep(self.delay)
        return {"ZCHF": 100000.0, "CHF": 100000.0}


def test_balance_cache_serves_stale_while_revalidate(tmp_path: Path, monkeypatch):
    # Production path (real client), credentials from env like the live app.
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    config = Config(
        config_path=_write_limits_config(
            tmp_path, max_price=65000, max_monthly_amount=10000
        )
    )

    cache = _BalanceCache()
    fake = _FakeSlowBalance()
    cache._client = cast(Any, fake)
    cache._client_key = config.api_key
    cache._client_secret = config.api_secret

    barrier = threading.Barrier(6)
    results: list[tuple[float | None, str, str | None]] = []

    def reader() -> None:
        barrier.wait()
        results.append(cache.get(config))

    threads = [threading.Thread(target=reader) for _ in range(5)]
    for t in threads:
        t.start()
    barrier.wait()
    start = time.perf_counter()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    # All 5 readers returned while the single refresh was still sleeping.
    assert len(results) == 5
    assert elapsed < 1.5
    assert fake.calls == 1  # single-flight: exactly one Kraken call
    assert fake.kwargs.get("timeout") == _BalanceCache.DASHBOARD_TIMEOUT_SECONDS
    assert fake.kwargs.get("max_retries") == 1

    # Once the refresh finished, readers get the fresh balance.
    available, fiat, error = cache.get(config)
    assert available == 100000.0
    assert fiat == "CHF"
    assert error is None


# -----------------------------------------------------------------------------
# 4. LOW — P/L chart colours segments by sign
# -----------------------------------------------------------------------------


def test_pl_chart_uses_sign_based_segment_colours():
    template = (
        Path(__file__).resolve().parent.parent
        / "web" / "templates" / "performance.html"
    ).read_text(encoding="utf-8")
    assert "if (name === 'pl_over_time')" in template
    assert "segment:" in template
    assert "colors.segment.positive" in template
    assert "colors.segment.negative" in template


# -----------------------------------------------------------------------------
# 5. LOW — range charts carry holdings bought before the range start
# -----------------------------------------------------------------------------


class _FakeOhlc:
    """DemoKrakenAPI stand-in returning fixed 4h candles at a constant close."""

    def __init__(self, key: str, secret: str) -> None:
        pass

    def get_ohlc(
        self, pair: str, interval: int = 60, since: int | None = None
    ) -> list[list[float]]:
        now = int(time.time())
        start = now - 31 * 86400
        step = 4 * 3600
        close = 60000.0
        return [
            [float(t), close, close, close, close, close, 1.0, 1]
            for t in range(start, now, step)
        ]


def _buy(days_ago: int, amount: float, price: float) -> Transaction:
    date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return Transaction.from_dict(
        {
            "date": date,
            "trading_pair": "XBTCHF",
            "amount": amount,
            "price": price,
        }
    )


def test_range_series_carries_pre_range_holdings(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setattr("web.routers.api.DemoKrakenAPI", _FakeOhlc)

    # Buy A: 40 days ago (before the 30d range), 0.001 @ 50'000 = 50 CHF.
    # Buy B: 5 days ago (inside the range), 0.001 @ 55'000 = 55 CHF.
    txns = [_buy(40, 0.001, 50000.0), _buy(5, 0.001, 55000.0)]

    series = _market_valuation_series("XBTCHF", txns, "30d", "key", "secret")

    assert series is not None
    # The series starts from the full position, not from zero.
    assert series["invested"][0] == 50.0
    # End value matches the KPI card: total invested 105, value 2 * 60000.
    assert series["invested"][-1] == 105.0
    assert series["value"][-1] == 120.0
    assert series["pl"][-1] == 15.0


# -----------------------------------------------------------------------------
# 6. Store — deposit_day beyond the previous month's length
# -----------------------------------------------------------------------------


def test_monthly_spent_clamps_deposit_day_to_month_length(tmp_path: Path, monkeypatch):
    # deposit_day=31 with a previous month of only 28/30 days raised
    # "day is out of range for month", breaking every budget check on those
    # days (found via a test run on the 31st, issue #21).
    fixed_now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("bot.store.utc_now", lambda: fixed_now)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    store.add_transaction("XBTCHF", 0.0001, 50000.0)

    spent = store.get_monthly_spent("XBTCHF", deposit_day=31, buy_hour=0)

    # February 2026 has 28 days: the cycle start clamps to Feb 28 00:00, so a
    # transaction from March 10 belongs to the current cycle.
    assert spent == 5.0


def test_monthly_spent_clamped_cycle_start_excludes_older_transactions(
    tmp_path: Path, monkeypatch
):
    fixed_now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("bot.store.utc_now", lambda: fixed_now)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    store.transactions.append(
        Transaction.from_dict(
            {
                "date": "2026-02-27T12:00:00+00:00",  # before the clamped start
                "trading_pair": "XBTCHF",
                "amount": 0.0001,
                "price": 50000.0,
            }
        )
    )

    spent = store.get_monthly_spent("XBTCHF", deposit_day=31, buy_hour=0)

    assert spent == 0.0
