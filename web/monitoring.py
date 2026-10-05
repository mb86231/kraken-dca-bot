"""Operational status and Prometheus-compatible metrics helpers.

This module aggregates non-sensitive runtime state from the bot's persistent
files so external monitoring tools (Uptime Kuma, Prometheus, custom scripts) can
detect failures independently of the bot's Telegram notifier.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status

from bot.order_execution import OrderState
from bot.utils import APP_VERSION, safe_load_json, utc_now


def require_monitoring_token(request: Request) -> None:
    """Validate a dedicated monitoring token passed as a Bearer token.

    The token is configured via the ``MONITORING_TOKEN`` environment variable.
    If the variable is unset, the endpoint is closed. This keeps monitoring
    clients separate from dashboard sessions and avoids exposing operational
    data to unauthenticated callers.
    """
    expected = os.environ.get("MONITORING_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Monitoring token not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    else:
        token = ""

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Monitoring token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid monitoring token",
        )



def _heartbeat_file() -> Path:
    return Path(os.environ.get("HEARTBEAT_FILE", "data/heartbeat.json"))


def _backup_status_file() -> Path:
    return Path(os.environ.get("BACKUP_DIR", "backups")) / "backup_status.json"


def _order_attempts_file() -> Path:
    return Path(os.environ.get("ORDER_ATTEMPTS_FILE", "data/order_attempts.json"))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _age_seconds(value: str | None) -> int | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return int((utc_now() - parsed).total_seconds())


def _load_heartbeat() -> dict[str, Any]:
    data = safe_load_json(_heartbeat_file())
    return data if isinstance(data, dict) else {}


def _load_backup_status() -> dict[str, Any]:
    data = safe_load_json(_backup_status_file())
    return data if isinstance(data, dict) else {}


def _load_order_attempts() -> list[dict[str, Any]]:
    data = safe_load_json(_order_attempts_file())
    return data if isinstance(data, list) else []


def _order_metrics(attempts: list[dict[str, Any]]) -> dict[str, int]:
    """Derive non-sensitive order counters from persisted attempts."""
    success = 0
    failure = 0
    retry = 0
    hold = 0
    rate_limited = 0
    unknown = 0

    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        state = attempt.get("state", "")
        attempt_number = attempt.get("attempt_number", 1) or 1
        error = (attempt.get("error_message") or "").lower()

        if state == OrderState.CONFIRMED.value:
            success += 1
        elif state in (OrderState.FAILED_PERMANENT.value, OrderState.HOLD.value):
            failure += 1
        elif state == OrderState.UNKNOWN.value:
            unknown += 1

        if state == OrderState.HOLD.value:
            hold += 1

        retry += max(0, attempt_number - 1)

        if "rate limit" in error or "429" in error:
            rate_limited += 1

    return {
        "success": success,
        "failure": failure,
        "retry": retry,
        "hold": hold,
        "rate_limited": rate_limited,
        "unknown": unknown,
    }


def _runtime_started_at(heartbeat: dict[str, Any]) -> datetime | None:
    return _parse_iso(heartbeat.get("runtime_started_at"))


def _uptime_seconds(heartbeat: dict[str, Any]) -> int | None:
    started = _runtime_started_at(heartbeat)
    if started is None:
        return None
    return int((utc_now() - started).total_seconds())


def get_operational_status() -> dict[str, Any]:
    """Return a non-sensitive operational status summary."""
    heartbeat = _load_heartbeat()
    backup_status = _load_backup_status()
    attempts = _load_order_attempts()
    order_metrics = _order_metrics(attempts)

    heartbeat_age = _age_seconds(heartbeat.get("timestamp"))
    backup_age = _age_seconds(backup_status.get("last_successful_at"))
    uptime = _uptime_seconds(heartbeat)

    notifier_enabled = bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )

    return {
        "version": APP_VERSION,
        "environment": os.environ.get("APP_ENV", "production"),
        "demo_mode": os.environ.get("DEMO_MODE", "").lower() in ("true", "1", "yes", "on"),
        "live_trading_enabled": os.environ.get("LIVE_TRADING_ENABLED", "").lower()
        in ("true", "1", "yes", "on"),
        "uptime_seconds": uptime,
        "heartbeat": {
            "timestamp": heartbeat.get("timestamp"),
            "age_seconds": heartbeat_age,
            "status": heartbeat.get("status"),
            "paused": heartbeat.get("paused"),
        },
        "schedule": {
            "last_cycle_at": heartbeat.get("last_cycle_at"),
            "next_cycle_at": heartbeat.get("next_cycle_at"),
        },
        "orders": {
            "last_successful_order_at": heartbeat.get("last_order_at"),
            "hold_active": order_metrics["hold"] > 0,
            "hold_count": order_metrics["hold"],
            "success_count": order_metrics["success"],
            "failure_count": order_metrics["failure"],
            "retry_count": order_metrics["retry"],
            "unknown_count": order_metrics["unknown"],
        },
        "backup": {
            "last_successful_at": backup_status.get("last_successful_at"),
            "age_seconds": backup_age,
            "validation_status": backup_status.get("validation_status", "unknown"),
        },
        "notifier": {
            "enabled": notifier_enabled,
        },
    }


def _prom_metric_line(name: str, value: int | float | None, help_text: str, metric_type: str) -> str:
    lines: list[str] = [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
    if value is None:
        lines.append(f"{name} NaN")
    else:
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


def get_prometheus_metrics() -> str:
    """Return Prometheus-compatible metrics text."""
    status = get_operational_status()
    heartbeat = status["heartbeat"]
    orders = status["orders"]
    backup = status["backup"]

    lines: list[str] = []
    lines.append(_prom_metric_line(
        "dca_bot_uptime_seconds",
        status.get("uptime_seconds"),
        "Time since the bot started",
        "gauge",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_heartbeat_age_seconds",
        heartbeat.get("age_seconds"),
        "Age of the latest trading-loop heartbeat",
        "gauge",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_backup_age_seconds",
        backup.get("age_seconds"),
        "Age of the last successful backup",
        "gauge",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_order_success_total",
        orders["success_count"],
        "Total confirmed order attempts",
        "counter",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_order_failure_total",
        orders["failure_count"],
        "Total failed order attempts",
        "counter",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_order_retry_total",
        orders["retry_count"],
        "Total order retry attempts",
        "counter",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_order_hold_count",
        orders["hold_count"],
        "Current number of held order attempts",
        "gauge",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_order_unknown_count",
        orders["unknown_count"],
        "Current number of order attempts with unknown outcome",
        "gauge",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_rate_limit_rejection_total",
        orders.get("rate_limited", 0),
        "Total order attempts rejected or delayed by rate limiting",
        "counter",
    ))
    lines.append(_prom_metric_line(
        "dca_bot_notifier_enabled",
        int(status["notifier"]["enabled"]),
        "Whether Telegram notifier is configured",
        "gauge",
    ))

    return "".join(lines)
