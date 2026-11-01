"""Regression tests for transient Kraken ``EGeneral:No data`` handling.

Kraken intermittently answers public read endpoints (notably Ticker) with
``EGeneral:No data`` during load or degradation. The bot must retry those
patiently (fixed 30 s backoff) instead of escalating an alert every poll
cycle, while non-idempotent order submission must still refuse to retry
them blindly. Alert creation must dedupe identical, still-unacknowledged
alerts inside a 15-minute window.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

import bot.alerts as alerts
from bot.api_client import EMPTY_RESULT_BACKOFF_SECONDS, KrakenAPI


def _make_api() -> KrakenAPI:
    # KrakenAPI expects api_secret to be base64-encoded for HMAC.
    secret = base64.b64encode(b"test-secret").decode()
    return KrakenAPI("test-key", secret)


def _ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


def _no_data_response() -> MagicMock:
    return _ok_response({"error": ["EGeneral:No data"], "result": {}})


def test_ticker_retries_no_data_with_fixed_backoff():
    """No-data No-data → success: retried, backoff is the fixed empty-result
    pause (not the short exponential ladder)."""
    api = _make_api()
    success = _ok_response({"error": [], "result": {"XXBTZEUR": {"c": ["69925.1", "0"]}}})

    sleeps: list[float] = []
    with patch(
        "urllib.request.urlopen",
        side_effect=[_no_data_response(), _no_data_response(), success],
    ), patch("time.sleep", side_effect=sleeps.append):
        price = api.get_ticker("XBTEUR")

    assert price == 69925.1
    assert len(sleeps) == 2
    assert all(s == EMPTY_RESULT_BACKOFF_SECONDS for s in sleeps)


def test_ticker_no_data_exhausted_raises():
    api = _make_api()
    with patch(
        "urllib.request.urlopen", side_effect=[_no_data_response()] * 3
    ), patch("time.sleep"):
        with pytest.raises(Exception, match="EGeneral:No data"):
            api.get_ticker("XBTEUR")


def test_order_submission_does_not_retry_no_data(monkeypatch):
    """place_market_order only retries rate limits; a No-data rejection must
    surface immediately for the executor's reconciliation machinery."""
    # Other tests may leave APP_ENV pointing at a non-production value, which
    # makes the live-order choke point refuse before the API is ever called.
    monkeypatch.setenv("APP_ENV", "production")
    api = _make_api()
    calls: list[int] = []

    def _counting_urlopen(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(1)
        return _no_data_response()

    with patch("urllib.request.urlopen", side_effect=_counting_urlopen), patch("time.sleep"):
        with pytest.raises(Exception, match="EGeneral:No data"):
            api.place_market_order("XBTEUR", "0.0001")

    assert len(calls) == 1


def _patched_alerts_path(tmp_path, monkeypatch):
    path = tmp_path / "alerts.json"
    monkeypatch.setattr(alerts, "ALERTS_PATH", path)
    return path


def test_alert_dedupe_refreshes_identical_recent_alert(tmp_path, monkeypatch):
    _patched_alerts_path(tmp_path, monkeypatch)
    first = alerts.create_alert("Check cycle error: x", "error")
    second = alerts.create_alert("Check cycle error: x", "error")

    stored = alerts.load_alerts()
    assert len(stored) == 1
    assert second["id"] == first["id"]
    assert second["timestamp"] >= first["timestamp"]


def test_alert_after_acknowledge_creates_new_entry(tmp_path, monkeypatch):
    _patched_alerts_path(tmp_path, monkeypatch)
    alerts.create_alert("Check cycle error: x", "error")
    stored = alerts.load_alerts()
    stored[0]["acknowledged"] = True
    alerts.save_alerts(stored)

    alerts.create_alert("Check cycle error: x", "error")
    assert len(alerts.load_alerts()) == 2


def test_alert_different_message_not_deduped(tmp_path, monkeypatch):
    _patched_alerts_path(tmp_path, monkeypatch)
    alerts.create_alert("Check cycle error: x", "error")
    alerts.create_alert("Check cycle error: y", "error")
    alerts.create_alert("Check cycle error: x", "warning")

    stored = alerts.load_alerts()
    assert len(stored) == 3
