"""Minimised, read-only data snapshots for the AI advisor.

Snapshots are built from an explicit field allowlist — never from raw
database or secrets access. They are the only data the advisor may send
to a provider, and every snapshot carries a ``generated_at`` timestamp so
consumers can enforce freshness.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bot.utils import utc_now

if TYPE_CHECKING:
    from bot.config import Config
    from bot.state import BotState
    from bot.store import TransactionStore


def build_state_snapshot(
    config: "Config",
    state: "BotState",
    store: "TransactionStore",
) -> dict[str, Any]:
    """Assemble the standard bot-state snapshot for advisory features.

    Fields are deliberately few and non-sensitive: market position,
    cycle/budget state, and totals. No credentials, no secrets, no raw
    transaction lists (callers that need history add an explicit,
    bounded excerpt themselves in later stories).
    """
    pair = config.trading_pair
    total_amount, avg_price, last_price, total_spent = store.get_statistics(pair)
    snapshot: dict[str, Any] = {
        "schema_version": "state-snapshot/v1",
        "generated_at": utc_now().isoformat(),
        "trading_pair": pair,
        "mode": config.mode,
        "live_trading_enabled": config.live_trading_enabled,
        "bot_status": state.status,
        "last_price": state.last_price,
        "last_price_at": state.last_price_at.isoformat() if state.last_price_at else None,
        "next_cycle_at": state.next_cycle_at.isoformat() if state.next_cycle_at else None,
        "estimated_buys": state.estimated_buys,
        "portfolio": {
            "total_amount": total_amount,
            "avg_buy_price": avg_price,
            "last_buy_price": last_price,
            "total_spent": total_spent,
            "purchase_count": store.get_transaction_count(pair),
        },
    }
    if config.max_monthly_amount is not None:
        snapshot["monthly_budget"] = {
            "limit": config.max_monthly_amount,
            "spent": store.get_monthly_spent(
                pair, config.deposit_day, config.buy_hour
            ),
        }
    return snapshot
