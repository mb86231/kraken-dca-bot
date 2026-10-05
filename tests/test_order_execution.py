"""Tests for the order-attempt state machine, retry logic, and reconciliation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import bot.order_execution as oe_module
from bot.config import Config
from bot.demo import DemoKrakenAPI
from bot.order_execution import ErrorCategory, OrderAttempt, OrderAttemptStore, OrderExecutor, OrderState
from bot.state import BotState
from bot.store import TransactionStore
from bot.utils import utc_now


class _FakeAPI(DemoKrakenAPI):
    """In-memory exchange API for order-execution tests."""

    def __init__(self, price: float = 50000.0):
        super().__init__(api_key="test-key", api_secret="test-secret")
        self.price = price
        self._orders: list[dict[str, Any]] = []
        self._next_error: Exception | None = None
        self._next_status: str = "closed"
        self.place_calls: list[dict[str, Any]] = []
        self.query_calls: list[int] = []

    def get_ticker(self, pair: str) -> float:
        return self.price

    def get_balance(self) -> dict[str, float]:
        return {"ZCHF": 100000.0, "CHF": 100000.0}

    def get_asset_pair_info(self, pair: str) -> dict[str, Any]:
        return {
            "ordermin": "0.0001",
            "costmin": "1",
            "lot_decimals": 8,
            "pair_decimals": 2,
        }

    def test_connection(self) -> bool:
        return True

    def place_market_order(
        self,
        pair: str,
        volume: str,
        order_type: str = "buy",
        userref: int | None = None,
    ) -> dict[str, Any]:
        self.place_calls.append({"pair": pair, "volume": volume, "userref": userref})
        if self._next_error is not None:
            err = self._next_error
            self._next_error = None
            raise err
        order_id = f"FAKE-ORDER-{len(self._orders) + 1:04d}"
        order = {
            "order_id": order_id,
            "pair": pair,
            "amount": float(volume),
            "price": self.price,
            "cost": float(volume) * self.price,
            "userref": userref,
            "status": self._next_status,
        }
        self._orders.append(order)
        return {
            "descr": {"order": f"buy {volume} {pair} @ market"},
            "txid": [order_id],
            "userref": userref,
        }

    def query_orders(self, userref: int) -> dict[str, dict[str, Any]]:
        self.query_calls.append(userref)
        matches = {
            o["order_id"]: o
            for o in self._orders
            if o.get("userref") == userref
        }
        return matches

    def queue_error(self, exc: Exception) -> None:
        self._next_error = exc

    def set_next_status(self, status: str) -> None:
        self._next_status = status


@pytest.fixture
def temp_dir(tmp_path: Path):
    return tmp_path


@pytest.fixture
def fake_api():
    return _FakeAPI()


@pytest.fixture
def attempt_store(temp_dir: Path):
    return OrderAttemptStore(filepath=temp_dir / "order_attempts.json")


@pytest.fixture
def tx_store(temp_dir: Path):
    return TransactionStore(filepath=temp_dir / "transactions.json")


@pytest.fixture
def bot_state(temp_dir: Path):
    return BotState(filepath=temp_dir / "state.json", persist=False, status="running")


@pytest.fixture
def executor(
    fake_api: _FakeAPI,
    tx_store: TransactionStore,
    attempt_store: OrderAttemptStore,
    bot_state: BotState,
    temp_dir: Path,
    monkeypatch,
):
    config_path = temp_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "trading_pair": "XBTCHF",
                "deposit_day": 15,
                "crypto_amount": 0.0001,
                "dip_threshold_percent": 5.0,
                "poll_interval_seconds": 600,
                "buy_hour": 8,
                "dip_buy_cooldown_hours": 24.0,
            }
        )
    )
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", "test-api-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "test-api-secret")
    config = Config(config_path=config_path)
    return OrderExecutor(
        api=fake_api,
        store=tx_store,
        attempt_store=attempt_store,
        notifier=MagicMock(),
        state=bot_state,
        config=config,
    )


# -----------------------------------------------------------------------------
# Basic success and state transitions
# -----------------------------------------------------------------------------


def test_submit_buy_confirms_success(executor: OrderExecutor, tx_store: TransactionStore):
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, strategy="scheduled", simulated=False)
    assert attempt.state == OrderState.CONFIRMED.value
    assert attempt.kraken_ref is not None
    assert tx_store.get_transaction_count("XBTCHF") == 1


def test_submit_buy_is_idempotent(executor: OrderExecutor, fake_api: _FakeAPI, tx_store: TransactionStore):
    cycle_time = utc_now()
    attempt1 = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, strategy="scheduled", simulated=False, cycle_time=cycle_time)
    attempt2 = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, strategy="scheduled", simulated=False, cycle_time=cycle_time)
    assert attempt1.attempt_id == attempt2.attempt_id
    assert len(fake_api.place_calls) == 1
    assert tx_store.get_transaction_count("XBTCHF") == 1


def test_different_cycle_gets_distinct_attempt(executor: OrderExecutor, fake_api: _FakeAPI):
    """Distinct userrefs for distinct logical cycles prevent cross-cycle collisions."""
    a1 = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, strategy="scheduled", simulated=False)
    a2 = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, strategy="manual", simulated=False)
    assert a1.attempt_id != a2.attempt_id
    assert a1.userref != a2.userref
    assert len(fake_api.place_calls) == 2


# -----------------------------------------------------------------------------
# Error classification and retry
# -----------------------------------------------------------------------------


def test_transient_failure_schedules_retry(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("HTTP Error 503: Service Unavailable"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)

    assert attempt.state == OrderState.FAILED_TRANSIENT.value
    assert attempt.error_category == ErrorCategory.TRANSIENT.value

    # process_pending should move it to RETRY_SCHEDULED.
    executor.process_pending()
    loaded = attempt_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.RETRY_SCHEDULED.value
    assert loaded.next_retry_at is not None


def test_retry_succeeds(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, tx_store: TransactionStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("HTTP Error 502: Bad Gateway"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.FAILED_TRANSIENT.value

    executor.process_pending()
    loaded = attempt_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.RETRY_SCHEDULED.value

    # Advance past the scheduled retry time.
    assert loaded.next_retry_at is not None
    retry_at = datetime.fromisoformat(loaded.next_retry_at)
    monkeypatch.setattr(oe_module, "utc_now", lambda: retry_at + timedelta(seconds=1))
    executor.process_pending()

    loaded = attempt_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.CONFIRMED.value
    assert tx_store.get_transaction_count("XBTCHF") == 1


def test_permanent_failure_enters_hold(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Kraken API Error: EOrder:Insufficient funds"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)

    assert attempt.state == OrderState.HOLD.value
    assert attempt.error_category == ErrorCategory.PERMANENT.value


def test_retries_exhausted_then_hold(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    # All attempts fail transiently.
    def _always_fail(*_args, **_kwargs):
        raise Exception("HTTP Error 504: Gateway Timeout")

    fake_api.place_market_order = _always_fail  # type: ignore[assignment]

    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.FAILED_TRANSIENT.value

    # Advance through retries until the executor gives up and places the attempt on hold.
    for _ in range(20):
        loaded = attempt_store.get(attempt.attempt_id)
        assert loaded is not None
        if loaded.state == OrderState.HOLD.value:
            break
        if loaded.next_retry_at:
            retry_at = datetime.fromisoformat(loaded.next_retry_at)
            monkeypatch.setattr(oe_module, "utc_now", lambda t=retry_at: t + timedelta(seconds=1))
        executor.process_pending()

    loaded = attempt_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.HOLD.value
    assert "exhausted" in (loaded.final_outcome or "").lower() or "max retry" in (loaded.final_outcome or "").lower()


# -----------------------------------------------------------------------------
# Unknown outcome and reconciliation
# -----------------------------------------------------------------------------


def test_timeout_creates_unknown_and_reconciles_found(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, tx_store: TransactionStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Read timed out"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.UNKNOWN.value
    assert attempt.error_category == ErrorCategory.UNKNOWN.value

    # Kraken actually accepted the order. Simulate it appearing on query.
    order_id = f"RECONCILED-{attempt.attempt_id}"
    fake_api._orders.append({
        "order_id": order_id,
        "pair": "XBTCHF",
        "amount": attempt.amount,
        "price": attempt.price,
        "userref": attempt.userref,
        "status": "closed",
    })

    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now + timedelta(seconds=60))
    executor.process_pending()

    loaded = attempt_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.CONFIRMED.value
    assert tx_store.get_transaction_count("XBTCHF") == 1


def test_unknown_not_found_after_window_becomes_permanent(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Read timed out"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.UNKNOWN.value

    window = executor._policy["reconcile_window_seconds"]
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now + timedelta(seconds=window + 1))
    executor.process_pending()

    loaded = attempt_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.FAILED_PERMANENT.value
    assert "not found" in (loaded.final_outcome or "").lower()


# -----------------------------------------------------------------------------
# Persistence and restart
# -----------------------------------------------------------------------------


def test_persisted_retry_survives_restart(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, tx_store: TransactionStore, temp_dir: Path, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("HTTP Error 429: Too Many Requests"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    executor.process_pending()

    # Simulate restart by creating a new executor from the same stores.
    new_store = OrderAttemptStore(filepath=attempt_store.filepath)
    new_tx_store = TransactionStore(filepath=tx_store.filepath)
    new_executor = OrderExecutor(
        api=fake_api,
        store=new_tx_store,
        attempt_store=new_store,
        notifier=MagicMock(),
        config=executor.config,
    )

    loaded = new_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.RETRY_SCHEDULED.value

    assert loaded.next_retry_at is not None
    retry_at = datetime.fromisoformat(loaded.next_retry_at)
    monkeypatch.setattr(oe_module, "utc_now", lambda: retry_at + timedelta(seconds=1))
    new_executor.process_pending()

    loaded = new_store.get(attempt.attempt_id)
    assert loaded is not None
    assert loaded.state == OrderState.CONFIRMED.value


# -----------------------------------------------------------------------------
# Notifier and operator actions
# -----------------------------------------------------------------------------


def test_notifier_failure_does_not_hide_state(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    from typing import cast

    notifier_mock = cast(MagicMock, executor.notifier)
    notifier_mock.send.side_effect = Exception("Telegram unreachable")
    fake_api.queue_error(Exception("HTTP Error 503: Service Unavailable"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)

    assert attempt.state == OrderState.FAILED_TRANSIENT.value


def test_manual_acknowledge(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Kraken API Error: EOrder:Insufficient funds"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value

    updated = executor.acknowledge_hold(attempt.attempt_id)
    assert updated is not None
    assert updated.acknowledged is True
    assert updated.notes is not None and "acknowledged" in updated.notes


def test_acknowledged_hold_does_not_block_new_order(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, tx_store: TransactionStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Kraken API Error: EOrder:Insufficient funds"))
    held = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert held.state == OrderState.HOLD.value

    executor.acknowledge_hold(held.attempt_id)

    new_attempt = executor.submit_buy(pair="XBTCHF", amount=0.0002, price=50000.0, strategy="manual", simulated=False)
    assert new_attempt.state == OrderState.CONFIRMED.value
    assert tx_store.get_transaction_count("XBTCHF") == 1


def test_cancel_attempt(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Kraken API Error: EOrder:Insufficient funds"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value

    cancelled = executor.cancel_attempt(attempt.attempt_id)
    assert cancelled is not None
    assert cancelled.state == OrderState.CANCELLED.value


def test_resolve_unknown_confirm_records_transaction(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, tx_store: TransactionStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Read timed out"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.UNKNOWN.value

    resolved = executor.resolve_unknown(attempt.attempt_id, "confirm")
    assert resolved is not None
    assert resolved.state == OrderState.CONFIRMED.value
    assert tx_store.get_transaction_count("XBTCHF") == 1


def test_resolve_unknown_cancel(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    fixed_now = utc_now()
    monkeypatch.setattr(oe_module, "utc_now", lambda: fixed_now)

    fake_api.queue_error(Exception("Read timed out"))
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.UNKNOWN.value

    resolved = executor.resolve_unknown(attempt.attempt_id, "cancel")
    assert resolved is not None
    assert resolved.state == OrderState.CANCELLED.value


# -----------------------------------------------------------------------------
# Demo API reconciliation support
# -----------------------------------------------------------------------------


def test_demo_api_query_orders_matches_userref():
    api = DemoKrakenAPI()
    result = api.place_market_order("XBTCHF", "0.0001", userref=12345)
    assert result["userref"] == 12345
    orders = api.query_orders(userref=12345)
    assert len(orders) == 1
    assert result["txid"][0] in orders


# -----------------------------------------------------------------------------
# Security: no secrets in persisted attempts
# -----------------------------------------------------------------------------


def test_attempt_store_does_not_include_secrets(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, temp_dir: Path):
    executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    raw = json.loads((temp_dir / "order_attempts.json").read_text())
    assert len(raw) == 1
    attempt = raw[0]
    # Keys, tokens, and session data must not appear in persisted order attempts.
    assert "api_key" not in attempt
    assert "api_secret" not in attempt
    assert "bot_token" not in attempt
    assert "session" not in attempt


# -----------------------------------------------------------------------------
# Final placement-time validation
# -----------------------------------------------------------------------------


def test_validation_fails_when_balance_drops_after_preflight(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    monkeypatch.setattr(fake_api, "get_balance", lambda: {"ZCHF": 0.0, "CHF": 0.0})
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value
    assert "balance" in (attempt.final_outcome or "").lower()
    assert len(fake_api.place_calls) == 0


def test_validation_fails_when_price_moves_above_buffer(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    # A large order combined with a high price exhausts the available balance.
    monkeypatch.setattr(fake_api, "get_ticker", lambda pair: 1_000_000.0)
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.1, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value
    assert "balance" in (attempt.final_outcome or "").lower()
    assert len(fake_api.place_calls) == 0


def test_validation_fails_when_minimum_order_increases(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    monkeypatch.setattr(
        fake_api, "get_asset_pair_info", lambda pair: {"ordermin": "0.001", "costmin": "1", "lot_decimals": 8, "pair_decimals": 2}
    )
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value
    assert "ordermin" in (attempt.final_outcome or "").lower()
    assert len(fake_api.place_calls) == 0


def test_validation_fails_when_precision_changes(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    monkeypatch.setattr(
        fake_api, "get_asset_pair_info", lambda pair: {"ordermin": "0.0001", "costmin": "1", "lot_decimals": 3, "pair_decimals": 2}
    )
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.00012345, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value
    assert "lot_decimals" in (attempt.final_outcome or "").lower()
    assert len(fake_api.place_calls) == 0


def test_validation_fails_when_bot_is_on_hold(executor: OrderExecutor, fake_api: _FakeAPI, bot_state: BotState):
    bot_state.update(status="hold")
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value
    assert "bot is in" in (attempt.final_outcome or "").lower()
    assert len(fake_api.place_calls) == 0


def test_validation_fails_when_unknown_order_exists(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore):
    unknown = OrderAttempt(
        attempt_id="oa-unknown",
        cycle_id="XBTCHF:scheduled:2025-01-01T00:00:00",
        pair="XBTCHF",
        amount=0.0001,
        price=50000.0,
        strategy="scheduled",
        simulated=False,
        state=OrderState.UNKNOWN.value,
    )
    attempt_store.save(unknown)
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value
    assert "unknown" in (attempt.final_outcome or "").lower()
    assert len(fake_api.place_calls) == 0


def test_validation_fails_on_duplicate_confirmed_order(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore):
    cycle_time = utc_now()
    first = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False, cycle_time=cycle_time)
    assert first.state == OrderState.CONFIRMED.value
    # Re-submitting the same logical cycle returns the existing attempt before calling place_market_order.
    second = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False, cycle_time=cycle_time)
    assert second.attempt_id == first.attempt_id
    assert len(fake_api.place_calls) == 1


def test_validation_fails_when_pair_metadata_unavailable(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    def _raise(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise Exception("Network unreachable")

    monkeypatch.setattr(fake_api, "get_asset_pair_info", _raise)
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.HOLD.value
    assert "pair metadata" in (attempt.final_outcome or "").lower()
    assert len(fake_api.place_calls) == 0


def test_validation_passes_and_submits_exactly_one_order(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore):
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=False)
    assert attempt.state == OrderState.CONFIRMED.value
    assert len(fake_api.place_calls) == 1
    assert attempt.kraken_ref is not None


def test_simulated_order_bypasses_live_validation(executor: OrderExecutor, fake_api: _FakeAPI, attempt_store: OrderAttemptStore, monkeypatch):
    # Even with an impossible live environment, a simulated order succeeds.
    monkeypatch.setenv("APP_ENV", "staging")
    attempt = executor.submit_buy(pair="XBTCHF", amount=0.0001, price=50000.0, simulated=True)
    assert attempt.state == OrderState.CONFIRMED.value
    assert attempt.simulated is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
