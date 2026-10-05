#!/usr/bin/env python3
"""Production preflight CLI.

Run this script before enabling live trading to prove the bot is correctly
configured. The script is read-only: it never places a real order.

Usage:
    python scripts/preflight_production.py
    python scripts/preflight_production.py --json
    python scripts/preflight_production.py --telegram-test
    python scripts/preflight_production.py --pair XBTCHF --amount 0.0002 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bot.config import Config
from bot.demo import is_demo_mode
from bot.preflight import (
    DEFAULT_RESULT_EXPIRY_SECONDS,
    PREFLIGHT_RESULT_FILE,
    CheckStatus,
    PreflightResult,
    ProductionPreflight,
    write_preflight_result,
)


def _error_result(message: str) -> PreflightResult:
    from bot.preflight import CheckResult
    from bot.utils import utc_now

    ran_at = utc_now()
    check = CheckResult(name="preflight_initialization", status=CheckStatus.FAIL, message=message)
    return PreflightResult(
        preflight_id="error",
        ran_at=ran_at.isoformat(),
        expires_at=ran_at.isoformat(),
        config_hash="",
        overall=CheckStatus.FAIL,
        can_place_live_orders=False,
        checks=[check],
        failures=[message],
    )


def _print_text(result: PreflightResult) -> None:
    print(f"Production Preflight — {result.overall}")
    print(f"  Ran:     {result.ran_at}")
    print(f"  Expires: {result.expires_at}")
    print(f"  Can place live orders: {result.can_place_live_orders}")
    print("")
    for check in result.checks:
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(check.status, "?")
        print(f"  [{icon}] {check.name}: {check.status} — {check.message}")
    if result.failures:
        print("\nFailures:")
        for failure in result.failures:
            print(f"  - {failure}")
    if result.warnings:
        print("\nWarnings:")
        for warning in result.warnings:
            print(f"  - {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only production preflight.")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable text")
    parser.add_argument("--telegram-test", action="store_true", help="Send a harmless Telegram test message")
    parser.add_argument("--pair", type=str, default=None, help="Override trading pair for this check")
    parser.add_argument("--amount", type=float, default=None, help="Override crypto amount for this check")
    parser.add_argument(
        "--output",
        type=Path,
        default=PREFLIGHT_RESULT_FILE,
        help=f"Path to write the preflight result (default: {PREFLIGHT_RESULT_FILE})",
    )
    parser.add_argument(
        "--expiry-seconds",
        type=int,
        default=DEFAULT_RESULT_EXPIRY_SECONDS,
        help=f"Result expiry in seconds (default: {DEFAULT_RESULT_EXPIRY_SECONDS})",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Data directory path",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("backups"),
        help="Backup directory path",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to config.json (default: config.json)",
    )
    args = parser.parse_args(argv)

    # Treat the script itself as a no-op in demo/staging; it is meant for production.
    if is_demo_mode():
        result = _error_result(
            "Demo or staging mode is active. Preflight is only meaningful when APP_ENV=production and DEMO_MODE is false."
        )
    else:
        try:
            config = Config(config_path=args.config)
        except Exception as exc:
            result = _error_result(f"Failed to load configuration: {exc}")
        else:
            preflight = ProductionPreflight(
                config,
                data_dir=args.data_dir,
                backup_dir=args.backup_dir,
            )
            try:
                result = preflight.run(
                    telegram_test=args.telegram_test,
                    pair=args.pair,
                    amount=args.amount,
                    expiry_seconds=args.expiry_seconds,
                )
            except Exception as exc:
                result = _error_result(f"Preflight failed: {exc}")
            write_preflight_result(args.output, result)

    if args.json:
        print(json.dumps(result.to_dict(redact=True), indent=2))
    else:
        _print_text(result)

    return 1 if result.overall == CheckStatus.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
