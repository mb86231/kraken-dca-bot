"""Regression tests for audit findings F1-F5 (2026-10-25)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.order_execution import OrderAttempt, OrderAttemptStore, OrderExecutor, OrderState
from bot.store import TransactionStore


# ---------------------------------------------------------------------------
# F1: calculate_next_buy must survive short months (deposit_day > month days)
# ---------------------------------------------------------------------------

def test_calculate_next_buy_short_month_with_deposit_day_31(tmp_path, monkeypatch):
    from bot.core import KrakenDCA

    class _Api:
        def get_ticker(self, pair):
            return 50000.0

        def get_balance(self):
            return {"ZCHF": 10000.0}

    bot = KrakenDCA.__new__(KrakenDCA)
    bot.config = SimpleNamespace(
        mode="recurring",
        deposit_day=31,
        buy_hour=8,
        trading_pair="XBTCHF",
        crypto_amount=0.0001,
        max_monthly_amount=None,
    )
    bot.state = SimpleNamespace(last_cycle_at=None)
    bot.api = _Api()
    bot.store = TransactionStore(filepath=tmp_path / "tx.json")
    # 30th of November — November has 30 days, so an unclamped
    # replace(day=31) would raise ValueError and kill the trading loop.
    monkeypatch.setattr(
        "bot.core.now_tz",
        lambda: datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc),
    )

    next_buy_time, hours_until_buy, remaining_hours, max_buys = bot.calculate_next_buy()

    assert isinstance(next_buy_time, datetime)
    assert next_buy_time >= datetime(2026, 11, 30, tzinfo=timezone.utc)
    assert max_buys > 0


def test_next_cycle_after_rolls_to_february_with_deposit_day_31():
    """A January 31 cycle must roll to Feb 28/29, not raise or overflow."""
    from bot.core import KrakenDCA

    class _Api:
        def get_ticker(self, pair):
            return 50000.0

        def get_balance(self):
            return {"ZCHF": 10000.0}

    bot = KrakenDCA.__new__(KrakenDCA)
    bot.config = SimpleNamespace(
        mode="recurring",
        deposit_day=31,
        buy_hour=8,
        trading_pair="XBTCHF",
        crypto_amount=0.0001,
        max_monthly_amount=None,
    )
    bot.api = _Api()
    last_cycle = datetime(2026, 1, 31, 8, 0, tzinfo=timezone.utc)

    next_cycle = bot._next_cycle_after(last_cycle)

    assert next_cycle.year == 2026
    assert next_cycle.month == 2
    assert next_cycle.day <= 28  # February 2026 has 28 days


def test_normalize_cycle_time_clamps_short_month():
    from bot.core import KrakenDCA

    bot = KrakenDCA.__new__(KrakenDCA)
    bot.config = SimpleNamespace(deposit_day=31, buy_hour=8)
    # April 2026 has 30 days.
    dt = datetime(2026, 4, 15, 22, 30, tzinfo=timezone.utc)
    normalized = bot._normalize_cycle_time(dt)
    assert normalized.day == 30
    assert normalized.hour == 8 and normalized.minute == 0


# ---------------------------------------------------------------------------
# F2: SUBMITTED attempts are reconciled by process_pending
# ---------------------------------------------------------------------------

class _ReconcileApi:
    def __init__(self):
        self.queries = 0

    def query_orders(self, userref):
        self.queries += 1
        return {"REAL-TXID": {"status": "closed", "price": "51000.0"}}


def _executor(tmp_path, api):
    store = TransactionStore(filepath=tmp_path / "tx.json")
    attempts = OrderAttemptStore(filepath=tmp_path / "attempts.json")
    state = SimpleNamespace(paused=False, status="running", update=lambda **kw: None)
    return OrderExecutor(api=api, store=store, attempt_store=attempts, state=state)


def test_submitted_attempt_is_reconciled_and_booked(tmp_path):
    api = _ReconcileApi()
    executor = _executor(tmp_path, api)
    attempt = OrderAttempt(
        attempt_id="oa-f2-1",
        cycle_id="c1",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=True,
        userref=12345,
        state=OrderState.SUBMITTED.value,
        kraken_ref="REAL-TXID",
        request_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    executor.attempt_store.save(attempt)

    executor.process_pending()

    after = executor.attempt_store.get("oa-f2-1")
    assert api.queries >= 1, "SUBMITTED attempt was never reconciled"
    assert after.state == OrderState.CONFIRMED.value
    assert executor.store.get_transaction_count("XBTCHF") == 1


# ---------------------------------------------------------------------------
# F3: simulated transactions must not count against the monthly budget
# ---------------------------------------------------------------------------

def test_monthly_spent_ignores_simulated_transactions(tmp_path):
    store = TransactionStore(filepath=tmp_path / "tx.json")
    store.add_transaction("XBTCHF", 0.001, 50000.0, simulated=True, notes="dry-run")
    assert store.get_monthly_spent("XBTCHF", deposit_day=1, buy_hour=0) == 0.0

    store.add_transaction("XBTCHF", 0.001, 40000.0, simulated=False)
    assert store.get_monthly_spent("XBTCHF", deposit_day=1, buy_hour=0) == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# F4: resolve_unknown rejects illegal state transitions
# ---------------------------------------------------------------------------

def test_resolve_cancel_on_confirmed_attempt_rejected(tmp_path):
    executor = _executor(tmp_path, _ReconcileApi())
    attempt = OrderAttempt(
        attempt_id="oa-f4-1",
        cycle_id="c1",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=True,
        userref=1,
        state=OrderState.CONFIRMED.value,
        final_outcome="confirmed",
    )
    executor.attempt_store.save(attempt)

    with pytest.raises(ValueError):
        executor.resolve_unknown("oa-f4-1", "cancel")

    assert executor.attempt_store.get("oa-f4-1").state == OrderState.CONFIRMED.value


def test_resolve_confirm_on_non_unknown_rejected(tmp_path):
    executor = _executor(tmp_path, _ReconcileApi())
    attempt = OrderAttempt(
        attempt_id="oa-f4-2",
        cycle_id="c1",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=True,
        userref=2,
        state=OrderState.RETRY_SCHEDULED.value,
    )
    executor.attempt_store.save(attempt)

    with pytest.raises(ValueError):
        executor.resolve_unknown("oa-f4-2", "confirm")


def test_resolve_legal_transitions_still_work(tmp_path):
    executor = _executor(tmp_path, _ReconcileApi())
    attempt = OrderAttempt(
        attempt_id="oa-f4-3",
        cycle_id="c1",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=True,
        userref=3,
        state=OrderState.UNKNOWN.value,
        kraken_ref="TX9",
    )
    executor.attempt_store.save(attempt)

    result = executor.resolve_unknown("oa-f4-3", "confirm")
    assert result.state == OrderState.CONFIRMED.value
    assert executor.store.get_transaction_count("XBTCHF") == 1

    # Second confirm must not double-book (guard raises before booking).
    with pytest.raises(ValueError):
        executor.resolve_unknown("oa-f4-3", "confirm")
    assert executor.store.get_transaction_count("XBTCHF") == 1


# ---------------------------------------------------------------------------
# F5: placement-time budget check (retry-safe), honouring the one-time override
# ---------------------------------------------------------------------------

class _LiveApi:
    def __init__(self):
        self.placed = 0

    def get_asset_pair_info(self, pair, timeout=None):
        return {"ordermin": "0.00001"}

    def get_ticker(self, pair):
        return 50000.0

    def get_balance(self):
        return {"ZCHF": 100000.0}

    def place_market_order(self, pair, volume, userref=None):
        self.placed += 1
        return {"txid": [f"TX-{self.placed}"]}


def _live_executor(tmp_path, api, monthly_limit):
    store = TransactionStore(filepath=tmp_path / "tx.json")
    attempts = OrderAttemptStore(filepath=tmp_path / "attempts.json")
    state = SimpleNamespace(paused=False, status="running", update=lambda **kw: None)
    config = SimpleNamespace(
        live_trading_enabled=True,
        max_monthly_amount=monthly_limit,
        deposit_day=1,
        buy_hour=0,
    )
    return OrderExecutor(
        api=api, store=store, attempt_store=attempts, state=state, config=config
    )


def test_placement_rejects_over_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr("bot.order_execution.is_demo_mode", lambda: False)
    api = _LiveApi()
    executor = _live_executor(tmp_path, api, monthly_limit=100.0)
    # Existing real spend: 80 CHF. A 0.001 BTC buy at 50'000 costs 50 CHF.
    executor.store.add_transaction("XBTCHF", 0.0016, 50000.0, simulated=False)  # 80 CHF

    attempt = executor.submit_buy(
        pair="XBTCHF", amount=0.001, price=50000.0, strategy="scheduled", simulated=False
    )

    assert attempt.state == OrderState.HOLD.value
    assert "monthly budget" in (attempt.final_outcome or "")
    assert api.placed == 0, "over-budget order must never reach the exchange"


def test_placement_honours_budget_override(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr("bot.order_execution.is_demo_mode", lambda: False)
    api = _LiveApi()
    executor = _live_executor(tmp_path, api, monthly_limit=100.0)
    executor.store.add_transaction("XBTCHF", 0.0016, 50000.0, simulated=False)  # 80 CHF

    attempt = executor.submit_buy(
        pair="XBTCHF",
        amount=0.001,
        price=50000.0,
        strategy="manual",
        simulated=False,
        budget_overridden=True,
    )

    assert attempt.state == OrderState.CONFIRMED.value
    assert api.placed == 1, "operator-approved over-budget buy must be placed"


# ---------------------------------------------------------------------------
# F6: unexpected loop errors are survived, not fatal (throttled alert + backoff)
# ---------------------------------------------------------------------------

def test_survive_loop_error_records_throttles_and_backs_off(monkeypatch):
    import threading

    from bot.core import KrakenDCA

    updates = []
    warnings = []
    alerts = []
    bot = KrakenDCA.__new__(KrakenDCA)
    bot.state = SimpleNamespace(
        update=lambda **kw: updates.append(kw),
        warning=lambda msg: warnings.append(msg),
    )
    bot.config = SimpleNamespace(poll_interval_seconds=60)
    bot._stop_event = threading.Event()
    bot._stop_event.set()  # make the backoff wait return immediately
    bot._loop_error_alerts = {}
    monkeypatch.setattr("bot.core.create_alert", lambda *a, **kw: alerts.append(a))

    bot._survive_loop_error(RuntimeError("boom"), "Cycle calculation error")

    assert updates[-1]["last_error"] == "boom"
    assert warnings == ["boom"]
    assert len(alerts) == 1

    bot._survive_loop_error(RuntimeError("boom"), "Cycle calculation error")
    assert len(alerts) == 1, "an identical error must be throttled"

    bot._survive_loop_error(RuntimeError("other failure"), "Cycle calculation error")
    assert len(alerts) == 2, "a different error alerts again"
