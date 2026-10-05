#!/usr/bin/env python3
"""Reset demo data to a clean state (empty transactions)."""

from pathlib import Path


def main() -> int:
    Path("data/transactions.json").write_text("[]", encoding="utf-8")
    print("Reset transactions.json to empty demo state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
