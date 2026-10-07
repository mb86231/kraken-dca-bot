"""Atomic, schema-versioned JSON transaction storage."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Tuple

from bot.persistence import JsonFile
from bot.utils import utc_now


@dataclass
class Transaction:
    """A single bot purchase record."""

    date: str
    trading_pair: str
    amount: float
    price: float
    fee: float = 0.0
    total_cost: float | None = None
    order_id: str = ""
    status: str = "filled"
    strategy: str = "scheduled"
    simulated: bool = False
    id: str = ""
    notes: str = ""
    # For dynamic-DCA orders: the tier threshold (%) that matched when the
    # order was placed, so the UI can show e.g. "Dynamic -5%".
    dynamic_tier: float | None = None

    def __post_init__(self):
        if not self.id:
            self.id = f"txn-{uuid.uuid4().hex[:12]}"
        if self.total_cost is None:
            self.total_cost = self.amount * self.price + (self.fee or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        # v1 compatibility: old records only had date, trading_pair, amount, price
        defaults = {
            "fee": 0.0,
            "total_cost": None,
            "order_id": "",
            "status": "filled",
            "strategy": "scheduled",
            "simulated": False,
            "id": "",
            "notes": "",
            "dynamic_tier": None,
        }
        for key, default in defaults.items():
            data.setdefault(key, default)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class TransactionStore:
    """JSON-based transaction storage with atomic writes and schema migration."""

    SCHEMA_VERSION = 2

    def __init__(self, filepath: str | Path = "data/transactions.json", json_file: JsonFile | None = None):
        self.filepath = Path(filepath)
        self._json = json_file or JsonFile(self.filepath)
        self.transactions: List[Transaction] = []
        self._load()

    def _load(self):
        """Load transactions from file, migrating from older schemas."""
        raw = self._json.load()
        if not isinstance(raw, list):
            raw = []

        migrated = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            # v1 -> v2 migration
            txn = Transaction.from_dict(item)
            # Generate order_id if missing using historical index
            if not txn.order_id:
                txn.order_id = f"ORDER-{len(migrated) + 1:06d}"
            migrated.append(txn)

        self.transactions = migrated
        # Persist migrated schema immediately so future reads are fast
        if raw:
            self._save()

    def _save(self):
        """Save transactions atomically."""
        data = [t.to_dict() for t in self.transactions]
        self._json.save(data, indent=2)

    def add_transaction(
        self,
        trading_pair: str,
        amount: float,
        price: float,
        fee: float = 0.0,
        order_id: str = "",
        status: str = "filled",
        strategy: str = "scheduled",
        simulated: bool = False,
        notes: str = "",
        dynamic_tier: float | None = None,
    ) -> Transaction:
        """Add a new transaction and persist.

        If order_id is provided and already exists, the existing transaction is
        returned to avoid double-counting.
        """
        if order_id:
            existing = next((t for t in self.transactions if t.order_id == order_id), None)
            if existing:
                return existing
        else:
            order_id = f"ORDER-{len(self.transactions) + 1:06d}"
        txn = Transaction(
            date=utc_now().isoformat(),
            trading_pair=trading_pair,
            amount=amount,
            price=price,
            fee=fee,
            order_id=order_id,
            status=status,
            strategy=strategy,
            simulated=simulated,
            notes=notes,
            dynamic_tier=dynamic_tier,
        )

        def _append(data):
            if not isinstance(data, list):
                data = []
            data.append(txn.to_dict())
            return data

        updated = self._json.update(_append, indent=2)
        self.transactions = [Transaction.from_dict(item) for item in updated]
        return txn

    def get_transaction_count(self, trading_pair: str | None = None) -> int:
        """Get total number of transactions, optionally filtered by trading pair."""
        if trading_pair:
            return len([tx for tx in self.transactions if tx.trading_pair == trading_pair])
        return len(self.transactions)

    def get_transactions(self, trading_pair: str | None = None) -> List[Transaction]:
        """Return transactions, optionally filtered by trading pair."""
        if trading_pair:
            return [tx for tx in self.transactions if tx.trading_pair == trading_pair]
        return list(self.transactions)

    def get_statistics(self, trading_pair: str) -> Tuple[float, float, float, float]:
        """Calculate statistics for trading pair.

        Returns: (total_amount, avg_price, last_price, total_spent)
        """
        pair_txs = [tx for tx in self.transactions if tx.trading_pair == trading_pair]

        if not pair_txs:
            return 0.0, 0.0, 0.0, 0.0

        total_amount = sum(tx.amount for tx in pair_txs)
        total_spent = sum(tx.amount * tx.price for tx in pair_txs)
        avg_price = total_spent / total_amount if total_amount > 0 else 0.0
        last_price = pair_txs[-1].price

        return total_amount, avg_price, last_price, total_spent

    def get_monthly_spent(self, trading_pair: str, deposit_day: int, buy_hour: int = 0) -> float:
        """Return fiat spent in the current deposit cycle."""
        now = utc_now()
        cycle_started = now.day > deposit_day or (now.day == deposit_day and now.hour >= buy_hour)
        if cycle_started:
            period_start = now.replace(day=deposit_day, hour=buy_hour, minute=0, second=0, microsecond=0)
        else:
            first_of_month = now.replace(day=1)
            prev_month = first_of_month - timedelta(days=1)
            period_start = prev_month.replace(day=deposit_day, hour=buy_hour, minute=0, second=0, microsecond=0)
        pair_txs = [
            tx
            for tx in self.transactions
            if tx.trading_pair == trading_pair
            and datetime.fromisoformat(tx.date) >= period_start
        ]
        return sum(tx.amount * tx.price for tx in pair_txs)

    def clear(self) -> int:
        """Remove all transactions and persist an empty store.

        Returns the number of transactions removed.
        """
        count = len(self.transactions)

        def _clear(_data):
            return []

        self._json.update(_clear, indent=2)
        self.transactions = []
        return count
