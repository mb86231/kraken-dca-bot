#!/usr/bin/env python3
"""Interactive validation of a single live Kraken trade.

This script is intended to be run once by an operator in the production
environment before the first unattended live-trade cycle. It performs safety
checks, asks for explicit confirmation, places one small manual buy, and verifies
that the result is recorded correctly.

Requirements:
  - APP_ENV=production
  - DEMO_MODE=false
  - LIVE_TRADING_ENABLED=true
  - Valid KRAKEN_API_KEY and KRAKEN_API_SECRET with Query + Create/Modify Orders
  - WEB_UI_PASSWORD_HASH or OIDC configured (not used by this script)
  - Optional but recommended: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

Example:
  APP_ENV=production LIVE_TRADING_ENABLED=true python scripts/validate_live_trade.py --amount 10

The script exits without placing an order unless the operator types the exact
confirmation phrase.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from repo root without installing.
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.api_client import KrakenAPI
from bot.config import Config
from bot.core import KrakenDCA
from bot.notifier import Notifier
from bot.order_execution import OrderAttemptStore, OrderState
from bot.state import BotState
from bot.store import TransactionStore
from bot.utils import utc_now


MINIMUM_CONFIRMATION_AMOUNT = 5.0  # fiat units (CHF/EUR/USD)
CONFIRMATION_PHRASE = "place one live order"


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def _check_environment() -> None:
    app_env = os.environ.get("APP_ENV", "production").lower()
    if app_env not in ("production", ""):
        _fail(f"APP_ENV must be production or unset (got {app_env!r})")

    if os.environ.get("DEMO_MODE", "").lower() in ("true", "1", "yes", "on"):
        _fail("DEMO_MODE must not be enabled for live validation")

    live = os.environ.get("LIVE_TRADING_ENABLED", "").lower()
    if live not in ("true", "1", "yes", "on"):
        _fail("LIVE_TRADING_ENABLED must be true")

    if not os.environ.get("KRAKEN_API_KEY"):
        _fail("KRAKEN_API_KEY is required")
    if not os.environ.get("KRAKEN_API_SECRET"):
        _fail("KRAKEN_API_SECRET is required")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate one live Kraken trade.")
    parser.add_argument(
        "--amount",
        type=float,
        default=10.0,
        help="Fiat amount for the validation buy (default: 10).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the typed confirmation (not recommended).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to config.json.",
    )
    args = parser.parse_args(argv)

    if args.amount < MINIMUM_CONFIRMATION_AMOUNT:
        _fail(f"Validation amount must be at least {MINIMUM_CONFIRMATION_AMOUNT} fiat units")

    _check_environment()

    print("Live-trade validation starting...")
    print(f"  Config: {args.config}")
    print(f"  Amount: {args.amount:.2f} fiat")
    print(f"  Time:   {utc_now().isoformat()}")

    config = Config(config_path=args.config)
    pair = config.trading_pair
    print(f"  Pair:   {pair}")

    api = KrakenAPI(config.api_key, config.api_secret)

    print("\n1. Testing Kraken API connectivity...")
    try:
        balance = api.get_balance()
        print("   OK: API credentials accepted")
    except Exception as exc:
        _fail(f"Could not query balance: {exc}")

    print("\n2. Checking fiat balance...")
    fiat = config.trading_pair[-3:]
    available = balance.get(f"Z{fiat}", balance.get(fiat, 0.0))
    print(f"   Available {fiat}: {available:.2f}")
    if available < args.amount:
        _fail(f"Insufficient balance: need {args.amount:.2f}, have {available:.2f}")

    print("\n3. Fetching current price...")
    try:
        price = api.get_ticker(pair)
    except Exception as exc:
        _fail(f"Could not fetch price: {exc}")
    print(f"   Price: {price:.2f} {fiat}")

    volume = args.amount / price
    print(f"   Volume: {volume:.8f}")

    print("\n4. Safety confirmation")
    if args.yes:
        print("   --yes provided; skipping typed confirmation.")
    else:
        print(f"   Type exactly: {CONFIRMATION_PHRASE}")
        typed = input("   > ").strip().lower()
        if typed != CONFIRMATION_PHRASE:
            _fail("Confirmation phrase did not match; no order was placed.")

    print("\n5. Placing one live market buy order...")
    store = TransactionStore()
    state = BotState(persist=True)
    attempt_store = OrderAttemptStore()
    notifier = Notifier()
    bot = KrakenDCA(config=config, store=store, state=state)

    # Route through the order executor so the attempt is persisted and retried.
    attempt = bot.order_executor.submit_buy(
        pair=pair,
        amount=volume,
        price=price,
        strategy="manual",
        simulated=False,
    )

    print(f"   Attempt ID: {attempt.attempt_id}")
    print(f"   State:      {attempt.state}")
    if attempt.kraken_ref:
        print(f"   Kraken ref: {attempt.kraken_ref}")

    if attempt.state == OrderState.CONFIRMED.value:
        print("\n   OK: Order confirmed and recorded.")
    elif attempt.state == OrderState.HOLD.value:
        _fail(f"Order attempt is on HOLD: {attempt.final_outcome}")
    elif attempt.state == OrderState.UNKNOWN.value:
        print("   Order outcome is UNKNOWN; the bot will reconcile automatically.")
        print("   Check the dashboard / data/order_attempts.json for updates.")
    else:
        print(f"   Order attempt state: {attempt.state}; monitor for retry.")

    print("\n6. Verification")
    tx_count = store.get_transaction_count(pair)
    print(f"   Transactions recorded: {tx_count}")
    if tx_count == 0 and attempt.state == OrderState.CONFIRMED.value:
        _fail("Order confirmed but no transaction was recorded")

    pending = [a for a in attempt_store.list_all() if a.state not in (
        OrderState.CONFIRMED.value,
        OrderState.CANCELLED.value,
        OrderState.HOLD.value,
    )]
    print(f"   Pending attempts: {len(pending)}")

    print("\nValidation complete.")
    if notifier.enabled:
        print("   Telegram notifications are enabled.")
    else:
        print("   WARNING: Telegram notifications are not configured.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
