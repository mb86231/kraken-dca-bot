"""Tests for the Kraken API client retry, error parsing, and helper paths."""

from __future__ import annotations

import base64
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from bot.api_client import KrakenAPI


def _make_api():
    # KrakenAPI expects api_secret to be base64-encoded for HMAC.
    secret = base64.b64encode(b"test-secret").decode()
    return KrakenAPI("test-key", secret)


def test_get_ticker_alt_pair_mapping():
    api = _make_api()
    fake_response = MagicMock()
    fake_response.read.return_value = (
        b'{"error": [], "result": {"XXBTZUSD": {"c": ["55000.0", "1.0"]}}}'
    )
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        # Requested pair not in result, but an aliased version is.
        price = api.get_ticker("XBTUSD")
    assert price == 55000.0


def test_get_balance_parses_result():
    api = _make_api()
    fake_response = MagicMock()
    fake_response.read.return_value = (
        b'{"error": [], "result": {"ZCHF": "1234.56", "CHF": "1234.56"}}'
    )
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        balance = api.get_balance()

    assert balance["ZCHF"] == 1234.56
    assert balance["CHF"] == 1234.56


def test_get_ohlc_returns_candles():
    api = _make_api()
    fake_response = MagicMock()
    fake_response.read.return_value = (
        b'{"error": [], "result": {"XBTCHF": [[1000, 1.0, 2.0, 0.5, 1.5, 1.2, 10.0, 5]]}}'
    )
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        candles = api.get_ohlc("XBTCHF", interval=60)

    assert candles == [[1000, 1.0, 2.0, 0.5, 1.5, 1.2, 10.0, 5]]


def test_api_request_retries_on_rate_limit():
    api = _make_api()

    class FakeRateLimitError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("url", 429, "Too Many Requests", {}, None)

    with patch("urllib.request.urlopen") as mock_urlopen:
        # Third attempt succeeds.
        success_response = MagicMock()
        success_response.read.return_value = b'{"error": [], "result": {"ZCHF": "100.0"}}'
        success_response.__enter__ = MagicMock(return_value=success_response)
        success_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            FakeRateLimitError(),
            FakeRateLimitError(),
            success_response,
        ]

        with patch("time.sleep"):  # speed up test
            balance = api.get_balance()

    assert balance["ZCHF"] == 100.0
    assert mock_urlopen.call_count == 3


def test_api_request_transient_error_raises_with_status():
    api = _make_api()

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("url", 500, "Internal Server Error", {}, None)

    with patch("urllib.request.urlopen", side_effect=FakeHTTPError()), patch("time.sleep"):
        with pytest.raises(Exception, match="HTTP Error 500"):
            api.get_balance()


def test_api_request_url_error_raises():
    api = _make_api()

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Name or service not known"),
    ), patch("time.sleep"):
        with pytest.raises(Exception, match="Connection Error"):
            api.get_balance()


def test_get_system_status_online():
    api = _make_api()
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"error": [], "result": {"status": "online"}}'
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        assert api.get_system_status() is True


def test_get_system_status_not_online():
    api = _make_api()
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"error": [], "result": {"status": "maintenance"}}'
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        with pytest.raises(Exception, match="Kraken API status"):
            api.get_system_status()


def _order_response(order_id: str = "ORDER-1"):
    resp = MagicMock()
    resp.read.return_value = (
        b'{"error": [], "result": {"txid": ["' + order_id.encode() + b'"], " descr": {}}}'
    )
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_addorder_submitted_exactly_once_on_timeout():
    """F1 regression: a timeout after Kraken accepted the order must NOT trigger
    a transport-level retry of AddOrder. One place_market_order call = exactly
    one HTTP request, even if a second attempt would have succeeded."""
    import os
    old_env = os.environ.get("APP_ENV")
    os.environ["APP_ENV"] = "production"
    try:
        api = _make_api()
        with patch("urllib.request.urlopen") as mock_urlopen:
            # First (and only allowed) request: reply lost after remote acceptance.
            mock_urlopen.side_effect = [
                urllib.error.URLError(TimeoutError("timed out")),
                _order_response(),  # would succeed — must never be reached
            ]
            with patch("time.sleep"):
                with pytest.raises(Exception, match="Connection Error"):
                    api.place_market_order("XBTCHF", "0.0001", userref=42)
        assert mock_urlopen.call_count == 1
    finally:
        if old_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = old_env


def test_addorder_retries_rate_limit_then_succeeds():
    """A 429 is a definitive rejection — retrying AddOrder is safe there."""
    import os
    old_env = os.environ.get("APP_ENV")
    os.environ["APP_ENV"] = "production"
    try:
        api = _make_api()

        class FakeRateLimitError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("url", 429, "Too Many Requests", {}, None)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [FakeRateLimitError(), _order_response()]
            with patch("time.sleep"):
                result = api.place_market_order("XBTCHF", "0.0001", userref=42)
        assert mock_urlopen.call_count == 2
        assert result["txid"] == ["ORDER-1"]
    finally:
        if old_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = old_env


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
