"""Bot runtime state shared between the trading loop and the web dashboard."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.persistence import JsonFile
from bot.utils import now_tz


# Fields that are persisted as ISO-format datetime strings.
_DATETIME_FIELDS = {
    "last_cycle_at",
    "next_cycle_at",
    "last_price_at",
    "last_order_at",
    "last_error_at",
    "runtime_started_at",
}


@dataclass
class BotState:
    """Mutable runtime state of the DCA bot."""

    status: str = "stopped"  # running, paused, stopped, error, waiting
    paused: bool = False
    mode: str = "recurring"
    last_cycle_at: datetime | None = None
    next_cycle_at: datetime | None = None
    last_price: float | None = None
    last_price_at: datetime | None = None
    last_order_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    simulated: bool = True
    runtime_started_at: datetime | None = None
    estimated_buys: int = 0
    version: str = "1.1.0"
    live_trading_enabled: bool = False
    exchange_connected: bool = False
    telegram_connected: bool = False
    recent_warnings: list[dict[str, str]] = field(default_factory=list)
    manual_cycle_requested: bool = False
    stop_requested: bool = False

    persist: bool = field(default=False, repr=False)
    filepath: Path = field(default_factory=lambda: Path("data/state.json"), repr=False)
    _json: JsonFile = field(default=None, repr=False)  # type: ignore[assignment]
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _wake_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def __post_init__(self) -> None:
        if self._json is None:
            object.__setattr__(self, "_json", JsonFile(self.filepath))
        if self.persist:
            self._load()

    def _load(self) -> None:
        """Load persisted state from disk."""
        data = self._json.load()
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if not hasattr(self, key) or key in ("persist", "filepath", "_json", "_lock", "_wake_event"):
                continue
            if key in _DATETIME_FIELDS and isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    continue
            setattr(self, key, value)

    def wake(self) -> None:
        """Signal the trading loop to wake up and re-check runtime overrides."""
        self._wake_event.set()

    def _snapshot_dict(self) -> dict[str, Any]:
        """Return a dict snapshot suitable for persistence. Must be called with _lock held."""
        return {
            "status": self.status,
            "paused": self.paused,
            "mode": self.mode,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "next_cycle_at": self.next_cycle_at.isoformat() if self.next_cycle_at else None,
            "last_price": self.last_price,
            "last_price_at": self.last_price_at.isoformat() if self.last_price_at else None,
            "last_order_at": self.last_order_at.isoformat() if self.last_order_at else None,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "simulated": self.simulated,
            "live_trading_enabled": self.live_trading_enabled,
            "runtime_started_at": self.runtime_started_at.isoformat() if self.runtime_started_at else None,
            "version": self.version,
            "exchange_connected": self.exchange_connected,
            "telegram_connected": self.telegram_connected,
            "recent_warnings": list(self.recent_warnings),
            "manual_cycle_requested": self.manual_cycle_requested,
            "stop_requested": self.stop_requested,
            "estimated_buys": self.estimated_buys,
        }

    def _persist(self, data: dict[str, Any]) -> None:
        """Write a snapshot to disk under the cross-process lock."""
        self._json.save(data, indent=2)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)
            if self.persist:
                self._persist(self._snapshot_dict())

    def warning(self, message: str) -> None:
        with self._lock:
            self.recent_warnings.append({"timestamp": now_tz().isoformat(), "message": message})
            self.recent_warnings = self.recent_warnings[-50:]
            if self.persist:
                self._persist(self._snapshot_dict())

    def clear_warnings(self) -> None:
        with self._lock:
            self.recent_warnings.clear()
            if self.persist:
                self._persist(self._snapshot_dict())

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_dict()


class RuntimeOverrides:
    """Persistent runtime overrides read by the bot each cycle."""

    def __init__(self, filepath: str | Path = "data/runtime_overrides.json", json_file: JsonFile | None = None):
        self.filepath = Path(filepath)
        self._json = json_file or JsonFile(self.filepath)
        self._load()

    def _load(self) -> None:
        data = self._json.load()
        if not isinstance(data, dict):
            data = {}
        self.paused: bool = data.get("paused", False)
        self.pause_reason: str | None = data.get("pause_reason")
        self.manual_cycle_requested: bool = data.get("manual_cycle_requested", False)
        self.stop_requested: bool = data.get("stop_requested", False)
        self.temporary_max_price: float | None = data.get("temporary_max_price")

    def _save(self) -> None:
        data = {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "manual_cycle_requested": self.manual_cycle_requested,
            "stop_requested": self.stop_requested,
            "temporary_max_price": self.temporary_max_price,
        }
        self._json.save(data, indent=2)

    def set_paused(self, paused: bool, reason: str | None = None) -> None:
        self.paused = paused
        self.pause_reason = reason if paused else None
        self._save()

    def request_manual_cycle(self) -> None:
        self.manual_cycle_requested = True
        self._save()

    def clear_manual_cycle(self) -> None:
        self.manual_cycle_requested = False
        self._save()

    def request_stop(self) -> None:
        self.stop_requested = True
        self._save()

    def clear_stop(self) -> None:
        self.stop_requested = False
        self._save()

    def set_temporary_max_price(self, price: float | None) -> None:
        self.temporary_max_price = price
        self._save()

    def to_dict(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "manual_cycle_requested": self.manual_cycle_requested,
            "stop_requested": self.stop_requested,
            "temporary_max_price": self.temporary_max_price,
        }
