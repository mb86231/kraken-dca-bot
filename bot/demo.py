"""Demo mode: mock exchange, synthetic prices, and fake transactions.

DemoKrakenAPI now fetches live market prices from Kraken's public API while
keeping orders and balances simulated. Production (KrakenAPI) is untouched.
"""

from __future__ import annotations

import json
import os
import random
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bot.utils import utc_now


# Live price data comes from Kraken's public market API, which requires no
# API key. See https://docs.kraken.com/rest/#tag/Market-Data/operation/getTickerInformation
KRAKEN_PUBLIC_API = "https://api.kraken.com/0/public"

# Kraken uses two pair conventions. Map common bot pair formats to the
# public API pair name.
_KRAKEN_PAIR_ALIASES = {
    "XXBTZUSD": "XBTUSD",
    "XXBTZEUR": "XBTEUR",
    "XXBTZCHF": "XBTCHF",
    "XXBTZGBP": "XBTGBP",
    "XETHZUSD": "ETHUSD",
    "XETHZEUR": "ETHEUR",
    "XETHZCHF": "ETHCHF",
}


def _kraken_public_pair(pair: str) -> str:
    """Return the Kraken public API pair name for a bot trading pair."""
    pair = pair.upper()
    return _KRAKEN_PAIR_ALIASES.get(pair, pair)


def _fetch_public(path: str, params: Optional[Dict[str, str]] = None) -> Optional[Dict]:
    """Call a Kraken public endpoint and return the parsed result, or None."""
    query = ""
    if params:
        query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{KRAKEN_PUBLIC_API}{path}{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if payload.get("error"):
                return None
            return payload.get("result")
    except (OSError, json.JSONDecodeError):
        return None


class DemoKrakenAPI:
    """Mock Kraken API that simulates orders and balances, but uses live prices."""

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key or "demo-key"
        self.api_secret = api_secret or "demo-secret"
        self._price: float = 65000.0
        self._orders: list[Dict] = []
        # Seed balances for the common fiat quote currencies so demo mode works
        # regardless of whether the configured pair is XBTCHF, XBTEUR, XXBTZUSD, etc.
        self._balance: Dict[str, float] = {
            "ZCHF": 10000.0,
            "CHF": 10000.0,
            "ZEUR": 10000.0,
            "EUR": 10000.0,
            "ZUSD": 10000.0,
            "USD": 10000.0,
        }

    def test_connection(self) -> bool:
        return True

    def _synthetic_price(self) -> float:
        change = random.uniform(-0.015, 0.016)
        self._price = max(1000.0, self._price * (1 + change))
        return round(self._price, 2)

    def get_ticker(self, pair: str) -> float:
        result = _fetch_public("/Ticker", {"pair": _kraken_public_pair(pair)})
        if not result:
            return self._synthetic_price()
        # Kraken returns a dict keyed by the (possibly aliased) pair name.
        for key, value in result.items():
            if isinstance(value, dict) and "c" in value:
                # "c" is the last closed price array, [price, volume].
                try:
                    return float(value["c"][0])
                except (IndexError, ValueError, TypeError):
                    break
        return self._synthetic_price()

    def get_balance(self) -> Dict[str, float]:
        return dict(self._balance)

    def get_asset_pair_info(self, pair: str) -> Dict[str, Any]:
        """Return synthetic pair metadata consistent with the Kraken public API."""
        return {
            "ordermin": "0.0001",
            "costmin": "10",
            "lot_decimals": 8,
            "pair_decimals": 2,
        }

    def get_ohlc(self, pair: str, interval: int = 60, since: Optional[int] = None) -> List[List[float]]:
        """Return live Kraken OHLC candles when available, otherwise synthetic demo candles."""
        params: Dict[str, str] = {
            "pair": _kraken_public_pair(pair),
            "interval": str(interval),
        }
        if since is not None:
            params["since"] = str(since)
        result = _fetch_public("/OHLC", params)
        if result:
            for key, value in result.items():
                if isinstance(value, list) and len(value) > 0 and isinstance(value[0], list):
                    # Convert string values to float and add the last
                    # traded volume (Kraken OHLC is 8 fields, index 7).
                    candles = []
                    for c in value:
                        if len(c) < 8:
                            continue
                        try:
                            candles.append([
                                int(c[0]),          # time
                                float(c[1]),        # open
                                float(c[2]),        # high
                                float(c[3]),        # low
                                float(c[4]),        # close
                                float(c[5]),        # vwap
                                float(c[6]),        # volume
                                int(c[7]),          # count
                            ])
                        except (ValueError, TypeError):
                            continue
                    if candles:
                        return candles

        # Fallback to synthetic demo candles
        now = int(datetime.now(timezone.utc).timestamp())
        interval_seconds = interval * 60
        start = since or (now - interval_seconds * 100)
        synthetic_candles: List[List[float]] = []
        close = self._price
        t = now
        while t >= start:
            change = random.uniform(-0.005, 0.005)
            open_p = close / (1 + change)
            high_p = max(open_p, close) * (1 + random.uniform(0, 0.003))
            low_p = min(open_p, close) * (1 - random.uniform(0, 0.003))
            synthetic_candles.insert(0, [t, round(open_p, 2), round(high_p, 2), round(low_p, 2), round(close, 2), round((open_p + close) / 2, 2), 0.0, 1])
            close = open_p
            t -= interval_seconds
        return synthetic_candles

    def place_market_order(
        self,
        pair: str,
        volume: str,
        order_type: str = "buy",
        userref: int | None = None,
    ) -> Dict:
        price = self.get_ticker(pair)
        amount = float(volume)
        cost = amount * price
        order_id = f"DEMO-ORDER-{len(self._orders) + 1:04d}"
        order = {
            "order_id": order_id,
            "pair": pair,
            "amount": amount,
            "price": price,
            "cost": cost,
            "userref": userref,
            "status": "closed",
        }
        self._orders.append(order)
        # Reduce fiat balance
        quote = pair[-3:] if not pair.startswith("X") else pair[-3:].lstrip("Z")
        for key in list(self._balance.keys()):
            if key.endswith(quote) or key == f"Z{quote}":
                self._balance[key] = max(0.0, self._balance[key] - cost)
        return {
            "descr": {"order": f"buy {amount} {pair} @ market"},
            "txid": [order_id],
            "userref": userref,
        }

    def query_orders(self, userref: int) -> Dict[str, Dict]:
        """Return demo orders matching ``userref`` for reconciliation tests."""
        matches = {
            o["order_id"]: o
            for o in self._orders
            if o.get("userref") == userref
        }
        return matches


def is_demo_mode() -> bool:
    """True when demo mode applies, checked against every safety signal.

    DEMO_MODE=true forces demo anywhere. Staging/development are always demo
    even if DEMO_MODE is unset, so code paths choosing the mock client fail safe.
    """
    if os.environ.get("DEMO_MODE", "").lower() in ("true", "1", "yes", "on"):
        return True
    if os.environ.get("APP_ENV", "production").lower() in ("staging", "development"):
        return True
    return False


def generate_demo_transactions(
    pair: str = "XBTCHF",
    count: int = 24,
    start_price: float = 65000.0,
    amount: float = 0.0001,
    simulated: bool = True,
) -> list[Dict]:
    """Generate synthetic transaction history."""
    transactions = []
    price = start_price
    now = utc_now()
    for i in range(count):
        # Random walk
        change = random.uniform(-0.03, 0.035)
        price = max(1000.0, price * (1 + change))
        date = now - timedelta(days=count - i)
        cost = amount * price
        transactions.append({
            "id": f"txn-demo-{i + 1:04d}",
            "date": date.isoformat(),
            "trading_pair": pair,
            "amount": amount,
            "price": round(price, 2),
            "fee": round(cost * 0.0026, 4),
            "total_cost": round(cost, 2),
            "order_id": f"DEMO-ORDER-{i + 1:04d}",
            "status": "filled",
            "strategy": "scheduled" if i % 5 != 4 else "dip",
            "simulated": simulated,
            "notes": "Demo transaction",
        })
    return transactions


def seed_demo_transactions(store, pair: str = "XBTCHF", count: int = 24) -> None:
    """Populate an empty transaction store with demo data."""
    if store.get_transaction_count() > 0:
        return
    for txn_data in generate_demo_transactions(pair=pair, count=count):
        store.add_transaction(
            trading_pair=txn_data["trading_pair"],
            amount=txn_data["amount"],
            price=txn_data["price"],
            fee=txn_data["fee"],
            order_id=txn_data["order_id"],
            status=txn_data["status"],
            strategy=txn_data["strategy"],
            simulated=txn_data["simulated"],
            notes=txn_data["notes"],
        )
