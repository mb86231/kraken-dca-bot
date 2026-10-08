"""Kraken API client with minimal dependencies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional


# How long to back off when Kraken reports a rate-limit error. Kraken's private
# API uses a counter that decays over several seconds; waiting ~20s gives the
# counter room to recover without extending the retry loop forever.
RATE_LIMIT_BACKOFF_SECONDS = 20

# Default user-agent so Kraken can identify the request and so the request matches
# the guidance in the API docs.
_USER_AGENT = (
    f"kraken-dca-bot/1.1.0 (Python/{platform.python_version()}; {platform.system()})"
)


class LiveOrderBlockedError(RuntimeError):
    """Raised when code attempts a live order without production authorization."""


class KrakenAPI:
    """Secure Kraken API client with minimal dependencies"""

    API_URL = "https://api.kraken.com"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret

    def _get_kraken_signature(self, urlpath: str, data: Dict, nonce: str) -> str:
        """Generate Kraken API signature"""
        postdata = urllib.parse.urlencode(data)
        encoded = (nonce + postdata).encode("utf-8")
        message = urlpath.encode("utf-8") + hashlib.sha256(encoded).digest()

        signature = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(signature.digest()).decode()

    def _api_request(
        self,
        endpoint: str,
        data: Optional[Dict] = None,
        private: bool = False,
        max_retries: int = 3,
        timeout: float = 30,
        retry_filter: Optional[Callable[[Exception], bool]] = None,
    ) -> Dict:
        """Make API request to Kraken with automatic retry on transient failures.

        Rate-limit errors from Kraken (``EAPI:Rate limit exceeded`` or HTTP 429)
        are recognised and retried with a longer, fixed backoff so the per-key
        counter has time to decay.

        ``retry_filter`` decides whether a failed attempt may be retried. The
        default retries everything (safe for idempotent read endpoints).
        ``place_market_order`` passes a filter that retries only rate-limit
        rejections: those are definitive "not accepted" answers, so resubmitting
        is safe. Any other failure of a non-idempotent order submission is
        raised immediately and left to the executor's UNKNOWN/reconciliation
        machinery — blind retries risk a duplicate purchase.
        """

        def _may_retry(exc: Exception) -> bool:
            if retry_filter is not None:
                return retry_filter(exc)
            return True

        def _is_rate_limit_error(exc: Exception) -> bool:
            """Return True if the exception looks like a Kraken rate-limit response."""
            msg = str(exc).lower()
            return (
                "rate limit exceeded" in msg
                or "eapi:rate limit" in msg
                or "too many requests" in msg
                or (isinstance(exc, urllib.error.HTTPError) and exc.code == 429)
            )

        for attempt in range(max_retries):
            url = f"{self.API_URL}{endpoint}"
            req_data = dict(data) if data else {}

            if private:
                nonce = str(int(time.time() * 1000))
                req_data["nonce"] = nonce

                headers = {
                    "API-Key": self.api_key,
                    "API-Sign": self._get_kraken_signature(endpoint, req_data, nonce),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": _USER_AGENT,
                }
                postdata = urllib.parse.urlencode(req_data).encode("utf-8")
                req = urllib.request.Request(url, data=postdata, headers=headers)
            else:
                headers = {"User-Agent": _USER_AGENT}
                if req_data:
                    postdata = urllib.parse.urlencode(req_data).encode("utf-8")
                    req = urllib.request.Request(url, data=postdata, headers=headers)
                else:
                    req = urllib.request.Request(url, headers=headers)

            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))

                    if result.get("error") and len(result["error"]) > 0:
                        raise Exception(f"Kraken API Error: {', '.join(result['error'])}")

                    return result.get("result", {})
            except Exception as e:
                is_last_attempt = attempt == max_retries - 1
                if is_last_attempt or not _may_retry(e):
                    if isinstance(e, urllib.error.HTTPError):
                        raise Exception(f"HTTP Error {e.code}: {e.reason}")
                    elif isinstance(e, urllib.error.URLError):
                        raise Exception(f"Connection Error: {e.reason}")
                    raise
                if _is_rate_limit_error(e):
                    wait = RATE_LIMIT_BACKOFF_SECONDS
                else:
                    wait = 5 * (2**attempt)
                print(f"API request failed (attempt {attempt + 1}/{max_retries}), retrying in {wait}s: {e}")
                time.sleep(wait)

        # The loop always returns or raises; this is unreachable but makes the
        # control flow explicit for type checkers.
        raise Exception("Unexpected end of retry loop")

    def test_connection(self) -> bool:
        """Test API connection and credentials"""
        try:
            self._api_request("/0/private/Balance", private=True)
            return True
        except Exception as e:
            raise Exception(f"API Connection Failed: {str(e)}")

    def get_system_status(self) -> bool:
        """Check whether Kraken's public API is reachable.

        Uses the public SystemStatus endpoint so it does not consume the private
        API rate-limit counter. Returns True when online, raises otherwise.
        """
        try:
            result = self._api_request("/0/public/SystemStatus")
            status = result.get("status", "")
            if status not in ("online", "ok"):
                raise Exception(f"Kraken API status: {status}")
            return True
        except Exception as e:
            raise Exception(f"Kraken API unreachable: {str(e)}")

    def get_ticker(self, pair: str) -> float:
        """Get current price for trading pair"""
        result = self._api_request("/0/public/Ticker", {"pair": pair})
        if pair not in result:
            # Try alternative pair format
            for key in result.keys():
                if key.replace("X", "").replace("Z", "") == pair.replace("X", "").replace("Z", ""):
                    pair = key
                    break

        if pair not in result:
            raise Exception(f"Trading pair {pair} not found")

        return float(result[pair]["c"][0])  # Current price

    def get_balance(self) -> Dict[str, float]:
        """Get account balance"""
        result = self._api_request("/0/private/Balance", private=True)
        return {k: float(v) for k, v in result.items()}

    def get_ohlc(self, pair: str, interval: int = 60, since: Optional[int] = None) -> list[list[float]]:
        """Get OHLC data for a trading pair.

        Returns a list of candles:
        [time, open, high, low, close, vwap, volume, count]
        """
        params: Dict[str, Any] = {"pair": pair, "interval": interval}
        if since is not None:
            params["since"] = since
        result = self._api_request("/0/public/OHLC", params)
        # Kraken returns the pair under its canonical name; fall back to the requested pair.
        data = result.get(pair)
        if data is None:
            for key in result.keys():
                if key.replace("X", "").replace("Z", "") == pair.replace("X", "").replace("Z", ""):
                    data = result[key]
                    break
        return data if data is not None else []

    def get_asset_pair_info(
        self, pair: str, timeout: float = 30
    ) -> Dict[str, Any]:
        """Return Kraken metadata for a trading pair.

        Uses the public ``AssetPairs`` endpoint so it does not consume the private
        API rate-limit counter. Raises if the pair is unknown. ``timeout`` allows
        dashboard call sites to fail fast when Kraken is unreachable.
        """
        result = self._api_request(
            "/0/public/AssetPairs", {"pair": pair}, timeout=timeout
        )
        if pair in result:
            return result[pair]
        # Kraken may return the pair under its canonical WS name (e.g. XBTUSD).
        for key in result.keys():
            if key.replace("X", "").replace("Z", "") == pair.replace("X", "").replace("Z", ""):
                return result[key]
        raise Exception(f"Trading pair {pair} not found")

    def place_market_order(
        self,
        pair: str,
        volume: str,
        order_type: str = "buy",
        userref: int | None = None,
    ) -> Dict:
        """Place market order. Refuses live orders in non-production environments.

        This is the final choke point before Kraken's AddOrder endpoint: even if
        a future caller bypasses the demo-mode switch, a real order can only be
        submitted when APP_ENV is explicitly production (or unset, preserving the
        legacy production default).

        ``userref`` is a stable client reference used for idempotency and
        reconciliation of uncertain orders.

        Order submission is effectively single-shot: only a rate-limit rejection
        (HTTP 429 / ``EAPI:Rate limit``) is retried, because that answer means
        Kraken definitively did not accept the order. Timeouts, connection loss,
        and 5xx responses may mean the order *was* accepted with a lost reply —
        those raise immediately so the executor reconciles via ``query_orders``
        before any resubmission instead of risking a duplicate purchase.
        """
        app_env = os.environ.get("APP_ENV", "production").lower()
        if app_env not in ("production", ""):
            raise LiveOrderBlockedError(
                f"Live order refused: KrakenAPI only places orders when APP_ENV=production (got {app_env!r})."
            )
        data = {
            "pair": pair,
            "type": order_type,
            "ordertype": "market",
            "volume": volume,
        }
        if userref is not None:
            data["userref"] = str(userref)

        def _retry_only_rate_limits(exc: Exception) -> bool:
            msg = str(exc).lower()
            return (
                "rate limit exceeded" in msg
                or "eapi:rate limit" in msg
                or "too many requests" in msg
                or (isinstance(exc, urllib.error.HTTPError) and exc.code == 429)
            )

        return self._api_request(
            "/0/private/AddOrder",
            data,
            private=True,
            retry_filter=_retry_only_rate_limits,
        )

    def query_orders(self, userref: int) -> Dict[str, Dict]:
        """Query private orders by ``userref`` for reconciliation.

        Returns a dict keyed by Kraken order ID. An empty result means Kraken
        currently has no matching order, *not* that the order was definitively
        rejected.
        """
        result = self._api_request("/0/private/QueryOrders", {"userref": str(userref)}, private=True)
        return result if isinstance(result, dict) else {}


