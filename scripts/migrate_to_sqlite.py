#!/usr/bin/env python3
"""
Optional migration script: copy transactions.json into a SQLite database.

This is provided as a documented future path. The bot continues to use JSON by
default. Run this script to create `data/transactions.db` from `transactions.json`.
"""

import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    transactions_path = Path("data/transactions.json")
    db_path = Path("data/transactions.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not transactions_path.exists():
        print("No transactions.json found; nothing to migrate.")
        return 0

    with open(transactions_path, "r", encoding="utf-8") as f:
        transactions = json.load(f)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            trading_pair TEXT NOT NULL,
            amount REAL NOT NULL,
            price REAL NOT NULL,
            fee REAL DEFAULT 0,
            total_cost REAL,
            order_id TEXT,
            status TEXT DEFAULT 'filled',
            strategy TEXT DEFAULT 'scheduled',
            simulated INTEGER DEFAULT 0,
            notes TEXT
        )
        """
    )

    for txn in transactions:
        cur.execute(
            """
            INSERT OR REPLACE INTO transactions
            (id, date, trading_pair, amount, price, fee, total_cost, order_id, status, strategy, simulated, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn.get("id"),
                txn.get("date"),
                txn.get("trading_pair"),
                txn.get("amount"),
                txn.get("price"),
                txn.get("fee", 0),
                txn.get("total_cost"),
                txn.get("order_id"),
                txn.get("status", "filled"),
                txn.get("strategy", "scheduled"),
                1 if txn.get("simulated") else 0,
                txn.get("notes"),
            ),
        )

    conn.commit()
    conn.close()
    print(f"Migrated {len(transactions)} transactions to {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
