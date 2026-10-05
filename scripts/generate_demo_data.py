#!/usr/bin/env python3
"""Generate synthetic demo transactions and reset the demo store."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.demo import generate_demo_transactions
from bot.utils import atomic_write_json


def main() -> int:
    os.environ["DEMO_MODE"] = "true"
    pair = os.environ.get("DEMO_PAIR", "XBTCHF")
    count = int(os.environ.get("DEMO_COUNT", "24"))

    transactions = generate_demo_transactions(pair=pair, count=count, simulated=True)
    atomic_write_json(Path("data/transactions.json"), transactions, indent=2)
    print(f"Generated {len(transactions)} demo transactions in transactions.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
