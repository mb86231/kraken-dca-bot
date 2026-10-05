"""Tests for the demo API with live public-market price fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.demo import DemoKrakenAPI, _kraken_public_pair


def test_kraken_public_pair_mapping():
    assert _kraken_public_pair("XBTCHF") == "XBTCHF"
    assert _kraken_public_pair("XXBTZUSD") == "XBTUSD"
    assert _kraken_public_pair("xbtchf") == "XBTCHF"


def test_demo_api_uses_live_price(monkeypatch):
    api = DemoKrakenAPI()
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"error": [], "result": {"XBTCHF": {"c": ["54000.50", "1.0"]}}}'
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        price = api.get_ticker("XBTCHF")

    assert price == 54000.5


def test_demo_api_falls_back_to_synthetic_price(monkeypatch):
    api = DemoKrakenAPI()

    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        price = api.get_ticker("XBTCHF")

    assert price > 0


def test_demo_api_ohlc_uses_live_candles(monkeypatch):
    api = DemoKrakenAPI()
    fake_response = MagicMock()
    fake_response.read.return_value = (
        b'{"error": [], "result": {"XBTCHF": [[1000, 1.0, 2.0, 0.5, 1.5, 1.2, 10.0, 5]]}}'
    )
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        candles = api.get_ohlc("XBTCHF", interval=60)

    assert len(candles) == 1
    assert candles[0] == [1000, 1.0, 2.0, 0.5, 1.5, 1.2, 10.0, 5]


def test_demo_api_place_order_is_simulated():
    api = DemoKrakenAPI()
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = OSError("network down")
        result = api.place_market_order("XBTCHF", "0.0001")

    assert result["txid"][0].startswith("DEMO-ORDER")
    assert len(api._orders) == 1


def test_demo_api_orders_reduce_balance():
    api = DemoKrakenAPI()
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = OSError("network down")
        api.place_market_order("XBTCHF", "0.0001")

    assert api._balance["ZCHF"] < 10000.0
    assert api._balance["CHF"] < 10000.0
