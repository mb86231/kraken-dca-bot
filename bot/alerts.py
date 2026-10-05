"""Alert generation and management for the DCA bot."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.state import BotState
from bot.utils import atomic_write_json, safe_load_json


ALERTS_PATH = Path("data/alerts.json")


def load_alerts() -> list[dict[str, Any]]:
    data = safe_load_json(ALERTS_PATH)
    return data if isinstance(data, list) else []


def save_alerts(alerts: list[dict[str, Any]]) -> None:
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(ALERTS_PATH, alerts[-200:], indent=2)


def create_alert(message: str, severity: str = "warning", source: str = "bot") -> dict[str, Any]:
    alerts = load_alerts()
    alert = {
        "id": f"alert-{uuid.uuid4().hex[:8]}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "severity": severity,
        "source": source,
        "acknowledged": False,
    }
    alerts.append(alert)
    save_alerts(alerts)
    return alert


def evaluate_alerts(state: BotState, config, store) -> list[dict[str, Any]]:
    """Check runtime conditions and create alerts as needed."""
    new_alerts = []

    if state.last_error:
        new_alerts.append(create_alert(f"Bot error: {state.last_error}", "error"))

    if state.last_price_at:
        age = (datetime.now(timezone.utc) - state.last_price_at).total_seconds()
        if age > config.poll_interval_seconds * 3:
            new_alerts.append(create_alert("Price data is stale", "warning"))

    if state.telegram_connected is False:
        # Only alert if Telegram was configured but failing
        pass

    # Large order check: each buy uses crypto_amount * current_price
    if state.last_price:
        order_value = config.crypto_amount * state.last_price
        if order_value > 10000:  # Example threshold
            new_alerts.append(create_alert(f"Large order amount detected: {order_value:.2f}", "warning"))

    return new_alerts
