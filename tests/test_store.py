"""Tests for transaction store and portfolio calculations."""

from __future__ import annotations

from datetime import datetime

from bot.store import TransactionStore


def test_empty_statistics(temp_dir):
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    total_amount, avg_price, last_price, total_spent = store.get_statistics("XBTCHF")
    assert total_amount == 0.0
    assert avg_price == 0.0
    assert last_price == 0.0
    assert total_spent == 0.0


def test_add_transaction_and_statistics(temp_dir):
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    store.add_transaction("XBTCHF", 0.0001, 50000.0)
    store.add_transaction("XBTCHF", 0.0001, 60000.0)

    total_amount, avg_price, last_price, total_spent = store.get_statistics("XBTCHF")
    assert total_amount == 0.0002
    assert avg_price == 55000.0
    assert last_price == 60000.0
    assert total_spent == 11.0


def test_schema_migration_from_v1(temp_dir):
    store_path = temp_dir / "transactions.json"
    store_path.write_text(
        '[{"date": "2025-01-15T10:00:00", "trading_pair": "XBTCHF", "amount": 0.0001, "price": 50000.0}]'
    )
    store = TransactionStore(filepath=store_path)
    assert store.get_transaction_count() == 1
    txn = store.transactions[0]
    assert txn.order_id.startswith("ORDER-")
    assert txn.id.startswith("txn-")
    assert txn.strategy == "scheduled"


def test_get_monthly_spent(temp_dir):
    store = TransactionStore(filepath=temp_dir / "transactions.json")
    now = datetime.now()
    store.add_transaction("XBTCHF", 0.0001, 50000.0)
    spent = store.get_monthly_spent("XBTCHF", now.day, now.hour)
    assert spent == 5.0
