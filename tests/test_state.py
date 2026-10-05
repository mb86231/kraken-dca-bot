"""Tests for BotState and RuntimeOverrides persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.state import BotState, RuntimeOverrides


def test_bot_state_loads_datetime_fields(tmp_path: Path):
    state_path = tmp_path / "state.json"
    ts = datetime.now(timezone.utc)
    state_path.write_text(
        json.dumps(
            {
                "status": "waiting",
                "last_cycle_at": ts.isoformat(),
                "next_cycle_at": ts.isoformat(),
                "last_price": 50000.0,
                "last_order_at": ts.isoformat(),
                "last_error": "none",
                "last_error_at": ts.isoformat(),
                "runtime_started_at": ts.isoformat(),
                "recent_warnings": [{"timestamp": ts.isoformat(), "message": "test"}],
            }
        )
    )
    state = BotState(filepath=state_path, persist=True)
    assert state.status == "waiting"
    assert isinstance(state.last_cycle_at, datetime)
    assert state.last_price == 50000.0


def test_bot_state_update_persists(tmp_path: Path):
    state = BotState(filepath=tmp_path / "state.json", persist=True)
    state.update(status="running", estimated_buys=3)
    assert state.status == "running"
    assert state.estimated_buys == 3

    # Verify file was written.
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["status"] == "running"
    assert data["estimated_buys"] == 3


def test_bot_state_warning_and_clear(tmp_path: Path):
    state = BotState(filepath=tmp_path / "state.json", persist=True)
    state.warning("first")
    state.warning("second")
    assert len(state.recent_warnings) == 2
    state.clear_warnings()
    assert len(state.recent_warnings) == 0


def test_bot_state_to_dict(tmp_path: Path):
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    state.update(status="paused")
    data = state.to_dict()
    assert data["status"] == "paused"
    assert "last_cycle_at" in data


def test_runtime_overrides_roundtrip(tmp_path: Path):
    overrides = RuntimeOverrides(filepath=tmp_path / "overrides.json")
    overrides.set_paused(True, reason="maintenance")
    assert overrides.paused is True
    assert overrides.pause_reason == "maintenance"

    # Re-load from disk.
    overrides2 = RuntimeOverrides(filepath=tmp_path / "overrides.json")
    assert overrides2.paused is True
    assert overrides2.pause_reason == "maintenance"


def test_runtime_overrides_manual_cycle(tmp_path: Path):
    overrides = RuntimeOverrides(filepath=tmp_path / "overrides.json")
    overrides.request_manual_cycle()
    assert overrides.manual_cycle_requested is True
    overrides.clear_manual_cycle()
    assert overrides.manual_cycle_requested is False


def test_runtime_overrides_stop(tmp_path: Path):
    overrides = RuntimeOverrides(filepath=tmp_path / "overrides.json")
    overrides.request_stop()
    assert overrides.stop_requested is True
    overrides.clear_stop()
    assert overrides.stop_requested is False


def test_runtime_overrides_temporary_max_price(tmp_path: Path):
    overrides = RuntimeOverrides(filepath=tmp_path / "overrides.json")
    overrides.set_temporary_max_price(60000.0)
    assert overrides.temporary_max_price == 60000.0
    overrides.set_temporary_max_price(None)
    assert overrides.temporary_max_price is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
