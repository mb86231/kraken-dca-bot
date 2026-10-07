"""Order-attempt state machine, retry logic, and reconciliation.

This module wraps the Kraken/Demo API and turns every buy attempt into a
persisted, idempotent, auditable order attempt. It is the only place that
should call ``place_market_order``.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from bot.alerts import create_alert
from bot.api_client import KrakenAPI, LiveOrderBlockedError
from bot.config import Config
from bot.demo import DemoKrakenAPI, is_demo_mode
from bot.notifier import Notifier
from bot.preflight import (
    DEFAULT_FEE_BUFFER_PERCENT,
    KRAKEN_TAKER_FEE_RATE,
    _quote_currency,
)
from bot.state import BotState
from bot.store import TransactionStore
from bot.persistence import JsonFile
from bot.utils import APP_VERSION, redact_sensitive, utc_now


class OrderState(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED_TRANSIENT = "FAILED_TRANSIENT"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    UNKNOWN = "UNKNOWN"
    HOLD = "HOLD"
    CANCELLED = "CANCELLED"


class ErrorCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    UNKNOWN = "UNKNOWN"


# Default retry / reconciliation configuration. Overridable via environment.
DEFAULT_RETRY_POLICY = {
    "max_attempts": 4,
    "base_seconds": 30,
    "multiplier": 4,
    "max_jitter_seconds": 10,
    "reconcile_window_seconds": 300,
    "unknown_hold_seconds": 1800,
    "reconcile_interval_seconds": 30,
}


def _retry_policy() -> dict[str, int]:
    """Load retry policy from environment with safe defaults."""
    return {
        "max_attempts": int(os.environ.get("ORDER_RETRY_MAX_ATTEMPTS", DEFAULT_RETRY_POLICY["max_attempts"])),
        "base_seconds": int(os.environ.get("ORDER_RETRY_BASE_SECONDS", DEFAULT_RETRY_POLICY["base_seconds"])),
        "multiplier": int(os.environ.get("ORDER_RETRY_MULTIPLIER", DEFAULT_RETRY_POLICY["multiplier"])),
        "max_jitter_seconds": int(os.environ.get("ORDER_RETRY_MAX_JITTER_SECONDS", DEFAULT_RETRY_POLICY["max_jitter_seconds"])),
        "reconcile_window_seconds": int(os.environ.get("ORDER_RECONCILE_WINDOW_SECONDS", DEFAULT_RETRY_POLICY["reconcile_window_seconds"])),
        "unknown_hold_seconds": int(os.environ.get("ORDER_UNKNOWN_HOLD_SECONDS", DEFAULT_RETRY_POLICY["unknown_hold_seconds"])),
        "reconcile_interval_seconds": int(os.environ.get("ORDER_RECONCILE_INTERVAL_SECONDS", DEFAULT_RETRY_POLICY["reconcile_interval_seconds"])),
    }


def _userref(pair: str, strategy: str, cycle_id: str, attempt_number: int) -> int:
    """Return a deterministic 31-bit unsigned userref for Kraken AddOrder.

    Kraken userref is a 32-bit signed integer. We use the low 31 bits to stay
    positive and deterministic across restarts.
    """
    payload = f"{pair}:{strategy}:{cycle_id}:{attempt_number}:{APP_VERSION}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest, 16) % 2_147_483_647


def _cycle_id(pair: str, strategy: str, cycle_time: datetime | None) -> str:
    """Stable identifier for the logical buy cycle that triggered the attempt."""
    if cycle_time is not None:
        return f"{pair}:{strategy}:{cycle_time.isoformat()}"
    return f"{pair}:{strategy}:{utc_now().isoformat()}"


@dataclass
class OrderAttempt:
    """A single, idempotent attempt to place a buy order."""

    attempt_id: str
    cycle_id: str
    pair: str
    amount: float
    price: float
    strategy: str
    simulated: bool
    attempt_number: int = 1
    userref: int = 0
    state: str = OrderState.CREATED.value
    error_category: str | None = None
    error_message: str | None = None
    kraken_ref: str | None = None
    request_timestamp: str | None = None
    response_timestamp: str | None = None
    next_retry_at: str | None = None
    last_reconciled_at: str | None = None
    reconcile_count: int = 0
    final_outcome: str | None = None
    acknowledged: bool = False
    dynamic_tier: float | None = None
    created_at: str = field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = field(default_factory=lambda: utc_now().isoformat())
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderAttempt":
        # Drop unknown fields so schema evolution does not break loading.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class OrderAttemptStore:
    """Atomic JSON persistence for order attempts."""

    def __init__(self, filepath: str | Path = "data/order_attempts.json", json_file: JsonFile | None = None):
        self.filepath = Path(filepath)
        self._json = json_file or JsonFile(self.filepath)
        self._attempts: dict[str, OrderAttempt] = {}
        self._load()

    def _load(self) -> None:
        raw = self._json.load()
        if not isinstance(raw, list):
            self._attempts = {}
            return
        self._attempts = {
            item["attempt_id"]: OrderAttempt.from_dict(item)
            for item in raw
            if isinstance(item, dict) and "attempt_id" in item
        }

    def _save(self) -> None:
        data = [a.to_dict() for a in self._attempts.values()]
        self._json.save(data, indent=2)

    def list_all(self) -> list[OrderAttempt]:
        return list(self._attempts.values())

    def get(self, attempt_id: str) -> OrderAttempt | None:
        return self._attempts.get(attempt_id)

    def by_userref(self, userref: int) -> OrderAttempt | None:
        """Return the most recent attempt with the given userref, if any."""
        matches = [a for a in self._attempts.values() if a.userref == userref]
        if not matches:
            return None
        return sorted(matches, key=lambda a: a.created_at, reverse=True)[0]

    def non_terminal(self) -> list[OrderAttempt]:
        terminal = {
            OrderState.CONFIRMED.value,
            OrderState.CANCELLED.value,
            OrderState.HOLD.value,
        }
        return [a for a in self._attempts.values() if a.state not in terminal]

    def save(self, attempt: OrderAttempt) -> None:
        attempt.updated_at = utc_now().isoformat()
        attempt_id = attempt.attempt_id

        def _upsert(data):
            if not isinstance(data, list):
                data = []
            data = [item for item in data if isinstance(item, dict) and item.get("attempt_id") != attempt_id]
            data.append(attempt.to_dict())
            return data

        updated = self._json.update(_upsert, indent=2)
        self._attempts = {
            item["attempt_id"]: OrderAttempt.from_dict(item)
            for item in updated
            if isinstance(item, dict) and "attempt_id" in item
        }


class OrderExecutor:
    """Execute buys with retry, reconciliation, and hold state."""

    def __init__(
        self,
        api: KrakenAPI | DemoKrakenAPI | None = None,
        store: TransactionStore | None = None,
        attempt_store: OrderAttemptStore | None = None,
        notifier: Notifier | None = None,
        state: BotState | None = None,
        config: Config | None = None,
        preflight_result_path: str | Path | None = None,
    ):
        self.api = api
        self.store = store
        self.attempt_store = attempt_store or OrderAttemptStore()
        self.notifier = notifier or Notifier()
        self.state = state
        self.config = config
        self.preflight_result_path = preflight_result_path
        self._policy = _retry_policy()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def submit_buy(
        self,
        pair: str,
        amount: float,
        price: float,
        strategy: str = "scheduled",
        simulated: bool = True,
        cycle_time: datetime | None = None,
        dynamic_tier: float | None = None,
        on_confirmed: Callable[[OrderAttempt], None] | None = None,
    ) -> OrderAttempt:
        """Submit a single buy attempt.

        If a non-terminal attempt already exists for the same logical cycle,
        it is returned instead of creating a duplicate.
        """
        cycle_id = _cycle_id(pair, strategy, cycle_time)
        attempt_number = 1
        userref = _userref(pair, strategy, cycle_id, attempt_number)

        existing = self.attempt_store.by_userref(userref)
        if existing:
            # Never create a duplicate attempt for the same logical cycle.
            return existing

        attempt = OrderAttempt(
            attempt_id=f"oa-{userref}-{utc_now().strftime('%Y%m%d%H%M%S%f')}",
            cycle_id=cycle_id,
            pair=pair,
            amount=amount,
            price=price,
            strategy=strategy,
            simulated=simulated,
            attempt_number=attempt_number,
            userref=userref,
            dynamic_tier=dynamic_tier,
        )
        self.attempt_store.save(attempt)
        return self._execute(attempt, on_confirmed=on_confirmed)

    def process_pending(self) -> list[OrderAttempt]:
        """Advance retries and reconciliation for all non-terminal attempts.

        Called periodically from the main trading loop. Returns the attempts
        that changed state.
        """
        changed: list[OrderAttempt] = []
        now = utc_now()
        for attempt in self.attempt_store.non_terminal():
            before = attempt.state

            if attempt.state == OrderState.FAILED_TRANSIENT.value:
                self._schedule_retry(attempt)
            elif attempt.state == OrderState.FAILED_PERMANENT.value:
                self._enter_hold(attempt, "permanent error")
            elif attempt.state == OrderState.UNKNOWN.value:
                self._process_unknown(attempt, now)
            elif attempt.state == OrderState.RETRY_SCHEDULED.value:
                retry_at = datetime.fromisoformat(attempt.next_retry_at) if attempt.next_retry_at else now
                if now >= retry_at:
                    self._execute(attempt)

            if attempt.state != before:
                self.attempt_store.save(attempt)
                changed.append(attempt)

        # Surface a bot-level hold only if any unacknowledged attempt is on hold.
        if self.state and any(
            a.state == OrderState.HOLD.value and not a.acknowledged
            for a in self.attempt_store.list_all()
        ):
            if self.state.status not in ("stopped", "error", "hold"):
                self.state.update(status="hold")

        return changed

    def cancel_attempt(self, attempt_id: str) -> OrderAttempt | None:
        """Cancel any non-terminal order attempt.

        Sets the attempt state to CANCELLED and records the operator action.
        Cancelled attempts do not block new orders.
        """
        attempt = self.attempt_store.get(attempt_id)
        if not attempt:
            return None
        if attempt.state in (
            OrderState.CONFIRMED.value,
            OrderState.CANCELLED.value,
        ):
            return attempt
        attempt.state = OrderState.CANCELLED.value
        attempt.final_outcome = "cancelled by operator"
        self.attempt_store.save(attempt)
        self._safe_notify(f"🚫 Order attempt {attempt_id} cancelled by operator")
        return attempt

    def acknowledge_hold(self, attempt_id: str) -> OrderAttempt | None:
        """Operator acknowledgement for a held order."""
        attempt = self.attempt_store.get(attempt_id)
        if not attempt:
            return None
        attempt.acknowledged = True
        attempt.notes = (attempt.notes or "") + " | acknowledged by operator"
        self.attempt_store.save(attempt)
        self._safe_notify(f"🤖 Order attempt {attempt_id} acknowledged by operator")
        return attempt

    def resolve_unknown(
        self,
        attempt_id: str,
        action: str,  # "confirm", "retry", "cancel"
    ) -> OrderAttempt | None:
        """Operator-driven resolution for an UNKNOWN order attempt."""
        attempt = self.attempt_store.get(attempt_id)
        if not attempt:
            return None

        if action == "confirm":
            self._confirm_from_reconciliation(attempt, "operator confirmed")
        elif action == "retry":
            attempt.attempt_number = 1
            attempt.state = OrderState.FAILED_TRANSIENT.value
            attempt.error_category = ErrorCategory.TRANSIENT.value
            attempt.error_message = "operator requested retry"
            self._schedule_retry(attempt)
        elif action == "cancel":
            attempt.state = OrderState.CANCELLED.value
            attempt.final_outcome = "cancelled by operator"
            self._safe_notify(f"🚫 Order attempt {attempt_id} cancelled by operator")

        self.attempt_store.save(attempt)
        return attempt

    # -----------------------------------------------------------------------
    # Internal execution
    # -----------------------------------------------------------------------

    def _validate_at_placement(self, attempt: OrderAttempt) -> tuple[bool, str, dict[str, Any]]:
        """Final time-of-check validation immediately before a real order is submitted.

        This is the last safety gate before Kraken's AddOrder endpoint. Conditions
        may have changed since the preflight or web-level gate ran, so every
        non-simulated order re-checks the current exchange state and local safety
        conditions. Simulated/demo orders bypass these checks.

        Returns ``(ok, reason, details)``. ``reason`` is empty when ``ok`` is True.
        """
        if attempt.simulated:
            return True, "", {}

        # Local environment and mode checks.
        if os.environ.get("APP_ENV", "").lower() != "production":
            return False, "APP_ENV is not production", {}

        if is_demo_mode():
            return False, "demo mode is active", {}

        if self.config is None or not self.config.live_trading_enabled:
            return False, "live trading is not enabled", {}

        # Bot safety state.
        if self.state is not None and self.state.status in ("stopped", "error", "hold"):
            return False, f"bot is in {self.state.status!r} state", {}

        # No unresolved UNKNOWN or unacknowledged HOLD order attempts.
        for a in self.attempt_store.list_all():
            if a.attempt_id == attempt.attempt_id:
                continue
            if a.state == OrderState.UNKNOWN.value:
                return False, "unresolved UNKNOWN order attempts exist", {}
            if a.state == OrderState.HOLD.value and not a.acknowledged:
                return False, "held order attempts exist", {}

        # Idempotency: a confirmed attempt with the same userref already exists.
        existing = self.attempt_store.by_userref(attempt.userref)
        if existing is not None and existing.attempt_id != attempt.attempt_id:
            if existing.state == OrderState.CONFIRMED.value:
                return False, f"confirmed attempt already exists for userref {attempt.userref}", {}

        # Current exchange metadata.
        if self.api is None:
            raise RuntimeError("OrderExecutor has no API client configured")
        try:
            pair_info = self.api.get_asset_pair_info(attempt.pair)
        except Exception as exc:
            return False, f"cannot load pair metadata: {redact_sensitive(str(exc))}", {}

        ordermin_str = pair_info.get("ordermin")
        costmin_str = pair_info.get("costmin")
        lot_decimals = pair_info.get("lot_decimals")

        details: dict[str, Any] = {
            "pair_info": pair_info,
            "ordermin": ordermin_str,
            "costmin": costmin_str,
            "lot_decimals": lot_decimals,
        }

        try:
            price = self.api.get_ticker(attempt.pair)
        except Exception as exc:
            return False, f"cannot fetch price: {redact_sensitive(str(exc))}", details
        details["price"] = price

        if ordermin_str is not None:
            try:
                ordermin_val = float(ordermin_str)
                if attempt.amount < ordermin_val:
                    return False, f"amount {attempt.amount} is below Kraken ordermin {ordermin_val}", details
            except (TypeError, ValueError):
                return False, f"cannot parse Kraken ordermin {ordermin_str!r}", details

        estimated_cost = attempt.amount * price
        if costmin_str is not None:
            try:
                costmin_val = float(costmin_str)
                if estimated_cost < costmin_val:
                    return False, f"estimated cost {estimated_cost:.2f} is below Kraken costmin {costmin_val}", details
            except (TypeError, ValueError):
                return False, f"cannot parse Kraken costmin {costmin_str!r}", details

        # Volume precision.
        if lot_decimals is not None:
            try:
                decimals = int(lot_decimals)
                quantized = round(attempt.amount, decimals)
                if abs(quantized - attempt.amount) > 10 ** (-decimals - 1):
                    return False, f"amount {attempt.amount} exceeds lot_decimals {decimals}", details
            except (TypeError, ValueError):
                return False, f"cannot parse lot_decimals {lot_decimals!r}", details

        # Balance check with fee buffer.
        try:
            balance = self.api.get_balance()
        except Exception as exc:
            return False, f"cannot fetch balance: {redact_sensitive(str(exc))}", details

        quote = _quote_currency(attempt.pair)
        available = balance.get(f"Z{quote}", balance.get(quote, 0.0))
        details["available"] = available

        buffer_percent = float(
            os.environ.get("ORDER_FEE_BUFFER_PERCENT", DEFAULT_FEE_BUFFER_PERCENT)
        )
        total_required = estimated_cost * (1 + KRAKEN_TAKER_FEE_RATE + buffer_percent / 100.0)
        details["total_required"] = total_required
        if available < total_required:
            return (
                False,
                f"available balance {available:.2f} {quote} is less than estimated cost "
                f"{total_required:.2f} {quote} (includes fee + buffer)",
                details,
            )

        return True, "", details

    def _execute(
        self,
        attempt: OrderAttempt,
        on_confirmed: Callable[[OrderAttempt], None] | None = None,
    ) -> OrderAttempt:
        """Attempt to place the order on Kraken (or the demo client)."""
        if self.api is None:
            raise RuntimeError("OrderExecutor has no API client configured")
        if self.store is None:
            raise RuntimeError("OrderExecutor has no transaction store configured")

        # Final placement-time validation. For live orders this re-checks current
        # exchange metadata, balance, fee buffer, and safety state as close as
        # possible to the actual AddOrder call.
        if not attempt.simulated:
            placement_ok, placement_reason, _placement_details = self._validate_at_placement(attempt)
            if not placement_ok:
                self._enter_hold(attempt, f"placement validation failed: {placement_reason}")
                return attempt

        attempt.state = OrderState.SUBMITTING.value
        attempt.request_timestamp = utc_now().isoformat()
        self.attempt_store.save(attempt)

        try:
            result = self.api.place_market_order(
                attempt.pair,
                str(attempt.amount),
                userref=attempt.userref,
            )
            attempt.response_timestamp = utc_now().isoformat()
            txid = ""
            if isinstance(result, dict):
                txid_list = result.get("txid") or []
                if txid_list:
                    txid = str(txid_list[0])
            attempt.kraken_ref = txid or None
            attempt.state = OrderState.SUBMITTED.value
            self.attempt_store.save(attempt)

            # For market orders we treat Kraken acceptance as filled/closed.
            self._confirm(attempt, result)
            if on_confirmed:
                on_confirmed(attempt)
            return attempt

        except LiveOrderBlockedError:
            # Safety guard in non-production environments; do not retry.
            self._enter_hold(attempt, "live order blocked outside production")
            return attempt
        except Exception as exc:  # noqa: BLE001
            attempt.response_timestamp = utc_now().isoformat()
            category, message = self._classify_error(exc)
            attempt.error_category = category.value
            attempt.error_message = redact_sensitive(message)

            if category == ErrorCategory.TRANSIENT:
                attempt.state = OrderState.FAILED_TRANSIENT.value
                self.attempt_store.save(attempt)
                self._safe_notify(
                    f"⚠️ Buy attempt transient failure\n"
                    f"Attempt: {attempt.attempt_id}\n"
                    f"Error: {attempt.error_message}"
                )
            elif category == ErrorCategory.PERMANENT:
                self._enter_hold(attempt, attempt.error_message)
            else:
                # UNKNOWN outcome: do not retry until reconciled.
                attempt.state = OrderState.UNKNOWN.value
                self.attempt_store.save(attempt)
                self._safe_notify(
                    f"❓ Buy attempt outcome unknown\n"
                    f"Attempt: {attempt.attempt_id}\n"
                    f"Will reconcile before retrying."
                )

            return attempt

    def _confirm(self, attempt: OrderAttempt, result: Any) -> None:
        """Record a confirmed order as a transaction."""
        if self.store is None:
            raise RuntimeError("OrderExecutor has no transaction store configured")
        order_id = attempt.kraken_ref or f"SIM-{attempt.attempt_id}"
        self.store.add_transaction(
            trading_pair=attempt.pair,
            amount=attempt.amount,
            price=attempt.price,
            fee=attempt.amount * attempt.price * 0.0026,  # estimated until QueryOrders
            order_id=order_id,
            status="filled",
            strategy=attempt.strategy,
            simulated=attempt.simulated,
            notes=f"Order attempt {attempt.attempt_id}",
            dynamic_tier=attempt.dynamic_tier,
        )
        attempt.state = OrderState.CONFIRMED.value
        attempt.final_outcome = "confirmed"
        self.attempt_store.save(attempt)
        self._safe_notify(
            f"✅ Buy order confirmed\n"
            f"Attempt: {attempt.attempt_id}\n"
            f"Amount: {attempt.amount:.8f} {attempt.pair}\n"
            f"Price: {attempt.price:.2f}\n"
            f"Order ID: {order_id}"
        )

    def _confirm_from_reconciliation(self, attempt: OrderAttempt, reason: str) -> None:
        """Confirm an attempt from reconciliation or operator action."""
        if self.store is None:
            raise RuntimeError("OrderExecutor has no transaction store configured")
        order_id = attempt.kraken_ref or f"RECONCILED-{attempt.attempt_id}"
        self.store.add_transaction(
            trading_pair=attempt.pair,
            amount=attempt.amount,
            price=attempt.price,
            fee=attempt.amount * attempt.price * 0.0026,
            order_id=order_id,
            status="filled",
            strategy=attempt.strategy,
            simulated=attempt.simulated,
            notes=f"Reconciled / {reason} ({attempt.attempt_id})",
            dynamic_tier=attempt.dynamic_tier,
        )
        attempt.state = OrderState.CONFIRMED.value
        attempt.final_outcome = f"confirmed via {reason}"
        self.attempt_store.save(attempt)
        self._safe_notify(
            f"✅ Order attempt confirmed via {reason}\n"
            f"Attempt: {attempt.attempt_id}\n"
            f"Order ID: {order_id}"
        )

    def _schedule_retry(self, attempt: OrderAttempt) -> None:
        """Set next_retry_at for a transient failure."""
        if attempt.attempt_number >= self._policy["max_attempts"]:
            self._enter_hold(attempt, "max retry attempts exhausted")
            return

        attempt.attempt_number += 1
        delay = self._policy["base_seconds"] * (self._policy["multiplier"] ** (attempt.attempt_number - 2))
        jitter = random.randint(0, self._policy["max_jitter_seconds"])
        next_retry = utc_now() + timedelta(seconds=delay + jitter)
        attempt.next_retry_at = next_retry.isoformat()
        attempt.state = OrderState.RETRY_SCHEDULED.value
        attempt.error_category = ErrorCategory.TRANSIENT.value
        self.attempt_store.save(attempt)
        self._safe_notify(
            f"🔄 Buy retry scheduled\n"
            f"Attempt: {attempt.attempt_id} (#{attempt.attempt_number})\n"
            f"Next retry: {next_retry.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )

    def _process_unknown(self, attempt: OrderAttempt, now: datetime) -> None:
        """Reconcile an UNKNOWN attempt, or move it to hold if it stays ambiguous too long."""
        request_ts = datetime.fromisoformat(attempt.request_timestamp) if attempt.request_timestamp else now
        age_seconds = (now - request_ts).total_seconds()

        if age_seconds > self._policy["unknown_hold_seconds"]:
            self._enter_hold(attempt, "unknown outcome exceeded hold window")
            return

        last_reconcile = None
        if attempt.last_reconciled_at:
            last_reconcile = datetime.fromisoformat(attempt.last_reconciled_at)
        if last_reconcile and (now - last_reconcile).total_seconds() < self._policy["reconcile_interval_seconds"]:
            return

        self._reconcile(attempt, now)

    def _reconcile(self, attempt: OrderAttempt, now: datetime) -> None:
        """Query Kraken for the UNKNOWN order and update state accordingly."""
        if self.api is None:
            raise RuntimeError("OrderExecutor has no API client configured")
        attempt.last_reconciled_at = now.isoformat()
        attempt.reconcile_count += 1
        self.attempt_store.save(attempt)

        try:
            orders = self.api.query_orders(userref=attempt.userref)
        except Exception as exc:  # noqa: BLE001
            # Reconciliation itself failed; stay UNKNOWN and try again later.
            attempt.error_message = f"reconciliation query failed: {redact_sensitive(str(exc))}"
            self.attempt_store.save(attempt)
            return

        if orders:
            # Kraken knows the order. Use the first match.
            info = next(iter(orders.values()))
            status = str(info.get("status", "")).lower()
            txid = str(info.get("txid", "")) or None
            if txid:
                attempt.kraken_ref = txid

            if status in ("closed", "filled", "executed"):
                # Try to enrich price from actual fill data.
                avg_price = info.get("price") or info.get("avg_price") or attempt.price
                try:
                    attempt.price = float(avg_price)
                except (TypeError, ValueError):
                    pass
                self._confirm_from_reconciliation(attempt, "Kraken query")
            elif status in ("open", "pending"):
                attempt.state = OrderState.SUBMITTED.value
                attempt.final_outcome = "open on Kraken"
                self.attempt_store.save(attempt)
            else:
                # Canceled/expired on Kraken side.
                attempt.state = OrderState.FAILED_PERMANENT.value
                attempt.final_outcome = f"Kraken status: {status}"
                self.attempt_store.save(attempt)
            return

        # Order not found. If we are past the reconcile window, treat as rejected.
        request_ts = datetime.fromisoformat(attempt.request_timestamp) if attempt.request_timestamp else now
        age_seconds = (now - request_ts).total_seconds()
        if age_seconds > self._policy["reconcile_window_seconds"]:
            attempt.state = OrderState.FAILED_PERMANENT.value
            attempt.final_outcome = "not found after reconcile window"
            attempt.error_message = "order not found on Kraken after reconciliation window"
            self.attempt_store.save(attempt)
            self._safe_notify(
                f"⚠️ Unknown order not found\n"
                f"Attempt: {attempt.attempt_id}\n"
                f"Treated as failed after {self._policy['reconcile_window_seconds']}s."
            )
        # Else remain UNKNOWN and reconcile again later.

    def _enter_hold(self, attempt: OrderAttempt, reason: str) -> None:
        """Move an attempt to HOLD and alert the operator."""
        if attempt.state == OrderState.HOLD.value:
            return
        attempt.state = OrderState.HOLD.value
        attempt.final_outcome = reason
        self.attempt_store.save(attempt)
        sanitized = redact_sensitive(reason)
        create_alert(
            f"Order attempt {attempt.attempt_id} on HOLD: {sanitized}",
            severity="error",
            source="order_executor",
        )
        self._safe_notify(
            f"🛑 Order attempt on HOLD\n"
            f"Attempt: {attempt.attempt_id}\n"
            f"Reason: {sanitized}\n"
            f"Action required in dashboard."
        )

    def _safe_notify(self, message: str) -> None:
        """Send a Telegram notification without letting failure hide state."""
        try:
            self.notifier.send(message)
        except Exception as exc:  # noqa: BLE001
            safe = redact_sensitive(str(exc))
            create_alert(f"Notifier failed: {safe}", severity="warning", source="notifier")

    def _classify_error(self, exc: Exception) -> tuple[ErrorCategory, str]:
        """Classify an exception as TRANSIENT, PERMANENT, or UNKNOWN outcome."""
        msg = redact_sensitive(str(exc)).lower()

        # Connection never reached Kraken.
        if any(x in msg for x in ("dns", "name resolution", "connection refused", "network is unreachable")):
            return ErrorCategory.TRANSIENT, str(exc)

        # HTTP status codes.
        http_match = re.search(r"http error (\d+)", msg)
        if http_match:
            code = int(http_match.group(1))
            if code == 429 or code in (502, 503, 504):
                return ErrorCategory.TRANSIENT, str(exc)
            if code in (400, 401, 403, 404):
                return ErrorCategory.PERMANENT, str(exc)

        # Kraken-specific permanent errors.
        permanent_phrases = (
            "insufficient funds",
            "invalid volume",
            "invalid price",
            "invalid pair",
            "invalid arguments",
            "permission denied",
            "unauthorized",
            "invalid key",
            "invalid signature",
            "eorder",
            "etrade",
        )
        if any(p in msg for p in permanent_phrases):
            return ErrorCategory.PERMANENT, str(exc)

        # Timeout / lost connection after possible transmission.
        if any(x in msg for x in ("timeout", "timed out", "connection reset", "broken pipe")):
            return ErrorCategory.UNKNOWN, str(exc)

        # Transient Kraken service messages.
        if any(x in msg for x in ("service unavailable", "temporary", "rate limit", "busy")):
            return ErrorCategory.TRANSIENT, str(exc)

        # Default: unknown outcome is safest.
        return ErrorCategory.UNKNOWN, str(exc)
