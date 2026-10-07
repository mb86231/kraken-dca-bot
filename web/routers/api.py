"""Main API router for the DCA bot dashboard."""

import csv
import io
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from slowapi.util import get_remote_address

from bot.api_client import KrakenAPI
from bot.config import Config
from bot.demo import DemoKrakenAPI, is_demo_mode
from bot.notifier import Notifier
from bot.order_execution import OrderAttemptStore, OrderExecutor
from bot.preflight import (
    PREFLIGHT_RESULT_FILE,
    ProductionPreflight,
    check_catalog,
    write_preflight_result,
)
from bot.secrets_store import SecretsStore, _as_bool
from bot.state import BotState, RuntimeOverrides
from bot.store import TransactionStore
from bot.utils import (
    APP_VERSION,
    DISPLAY_TZ,
    atomic_write_json,
    format_datetime,
    now_tz,
    redact_sensitive,
    safe_load_json,
    utc_now,
)
from web.auth import auth_manager, csrf_protect, require_auth
from web.brute_force import check_login_allowed
from web.deps import get_attempt_store, get_config, get_overrides, get_state, get_store
from web.oidc import oidc_provider
from web.rate_limit import (
    api_rate_limit,
    login_rate_limit,
    manual_buy_rate_limit,
    settings_rate_limit,
)
from web.schemas import (
    ExchangeCredentialsUpdate,
    LoginPayload,
    OIDCSettingsUpdate,
    SettingsUpdate,
    TelegramUpdate,
    WebAuthUpdate,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Balance cache
# ---------------------------------------------------------------------------

class _BalanceCache:
    """In-memory cache for the dashboard balance endpoint.

    Why: every open dashboard tab was calling the private ``/0/private/Balance``
    endpoint directly, which quickly exhausts Kraken's per-key rate-limit counter
    and makes the balance display drop to zero while the real error is hidden. This
    cache shares one result across all tabs and limits private API calls to at most
    one per ``TTL_SECONDS``.
    """

    TTL_SECONDS = 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: KrakenAPI | DemoKrakenAPI | None = None
        self._timestamp = 0.0
        self._available_fiat: float | None = None
        self._fiat_currency = ""
        self._error: str | None = None

    def get(
        self,
        config: Config,
    ) -> tuple[float | None, str, str | None]:
        """Return (available_fiat, fiat_currency, error)."""
        fiat_currency = _quote_currency(config.trading_pair)
        with self._lock:
            now = time.time()
            if (
                self._fiat_currency == fiat_currency
                and self._error is None
                and now - self._timestamp < self.TTL_SECONDS
            ):
                return self._available_fiat, self._fiat_currency, None

            # Refresh the cache.
            self._fiat_currency = fiat_currency
            try:
                if is_demo_mode():
                    balance: dict[str, float] = {
                        "Z" + fiat_currency: 10000.0,
                        fiat_currency: 10000.0,
                    }
                else:
                    if self._client is None:
                        self._client = KrakenAPI(config.api_key, config.api_secret)
                    balance = self._client.get_balance()
                available = balance.get(f"Z{fiat_currency}", balance.get(fiat_currency, 0.0))
                self._available_fiat = float(available)
                self._error = None
            except Exception as e:
                self._available_fiat = None
                self._error = redact_sensitive(str(e))
            self._timestamp = now
            return self._available_fiat, self._fiat_currency, self._error


_balance_cache = _BalanceCache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(existing: Any, update: Any) -> Any:
    """Recursively merge `update` into `existing`.

    Dictionaries are merged key-by-key; other values are replaced. This lets
    partial updates like `{"dynamic_dca": {"enabled": true}}` preserve the
    existing nested tiers instead of overwriting the whole block.
    """
    if isinstance(existing, dict) and isinstance(update, dict):
        merged = dict(existing)
        for key, value in update.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return update


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/auth/login")
def api_login(
    request: Request,
    payload: LoginPayload,
    response: Response,
    _rate_limit=Depends(login_rate_limit),
):
    from web.auth import login_post

    check_login_allowed(request, payload.username)
    if login_post(request, payload.username, payload.password, response):
        token = auth_manager.set_csrf_cookie(response)
        return {"status": "ok", "csrf_token": token}
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")


@router.post("/auth/logout")
def api_logout(response: Response, _rate_limit=Depends(api_rate_limit)):
    from web.auth import logout

    logout(response)
    return {"status": "ok"}


@router.get("/auth/me")
def api_me(username: str = Depends(require_auth), _rate_limit=Depends(api_rate_limit)):
    return {"username": username, "role": "admin"}


# ---------------------------------------------------------------------------
# Status & dashboard
# ---------------------------------------------------------------------------

def _portfolio_data(store: TransactionStore, state: BotState, config: Config) -> dict[str, Any]:
    pair = config.trading_pair
    total_amount, avg_price, last_price, total_spent = store.get_statistics(pair)
    current_price = state.last_price or last_price or 0.0
    current_value = total_amount * current_price
    pl_fiat = current_value - total_spent
    pl_percent = (pl_fiat / total_spent * 100) if total_spent > 0 else 0.0

    return {
        "trading_pair": pair,
        "quote_currency": _quote_currency(pair),
        "total_invested": round(total_spent, 2),
        "current_value": round(current_value, 2),
        "total_amount": round(total_amount, 8),
        "avg_buy_price": round(avg_price, 2),
        "last_buy_price": round(last_price, 2),
        "current_price": round(current_price, 2),
        "unrealized_pl": round(pl_fiat, 2),
        "unrealized_pl_percent": round(pl_percent, 2),
        "purchase_count": store.get_transaction_count(pair),
        "failed_count": 0,  # tracked once order statuses are enriched
    }


def _quote_currency(pair: str) -> str:
    p = pair[1:] if pair.startswith("X") else pair
    quote = p[-3:]
    return quote[1:] if quote.startswith("Z") else quote


@router.get("/preflight")
def api_preflight(
    config: Config = Depends(get_config),
    state: BotState = Depends(get_state),
    overrides: RuntimeOverrides = Depends(get_overrides),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Return the latest production preflight result.

    The result is recalculated on every request using the currently loaded
    configuration and persisted bot state. No orders are placed.
    """
    preflight = ProductionPreflight(config, state=state, overrides=overrides)
    result = preflight.run()
    return result.to_dict()


@router.get("/preflight/checks")
def api_preflight_checks(
    config: Config = Depends(get_config),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """List all available preflight checks and which ones are disabled."""
    disabled = set(config.preflight_disabled_checks)
    checks = [
        {**check, "enabled": check["name"] not in disabled}
        for check in check_catalog()
    ]
    return {"checks": checks, "disabled": sorted(disabled)}


@router.post("/preflight/run")
@csrf_protect
async def api_preflight_run(
    request: Request,
    config: Config = Depends(get_config),
    state: BotState = Depends(get_state),
    overrides: RuntimeOverrides = Depends(get_overrides),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Re-run the production preflight and persist the result."""
    api: KrakenAPI | DemoKrakenAPI | None = None
    if not is_demo_mode():
        api = KrakenAPI(config.api_key, config.api_secret)
    preflight = ProductionPreflight(config, api=api, state=state, overrides=overrides)
    result = preflight.run()
    write_preflight_result(PREFLIGHT_RESULT_FILE, result)
    return result.to_dict()


@router.get("/status")
def api_status(
    config: Config = Depends(get_config),
    store: TransactionStore = Depends(get_store),
    state: BotState = Depends(get_state),
    overrides: RuntimeOverrides = Depends(get_overrides),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    portfolio = _portfolio_data(store, state, config)
    return {
        "status": "paused" if overrides.paused else state.status,
        "paused": overrides.paused,
        "mode": config.mode,
        "live_trading_enabled": config.live_trading_enabled,
        "live_trading_env_override": os.environ.get("LIVE_TRADING_ENABLED") is not None,
        "simulated": not config.live_trading_enabled or is_demo_mode(),
        "demo_mode": is_demo_mode(),
        "last_cycle_at": format_datetime(state.last_cycle_at),
        "next_cycle_at": format_datetime(state.next_cycle_at),
        "estimated_buys": state.estimated_buys,
        "monthly_budget": {
            "limit": config.max_monthly_amount,
            "spent": (
                store.get_monthly_spent(config.trading_pair, config.deposit_day, config.buy_hour)
                if config.max_monthly_amount is not None
                else 0.0
            ),
            "currency": _quote_currency(config.trading_pair),
            # Estimate of the next base buy, so the UI can ask for a one-time
            # over-budget approval before requesting the manual cycle.
            "next_buy_cost": (
                round(config.crypto_amount * state.last_price, 2)
                if state.last_price
                else None
            ),
        },
        "last_price": state.last_price,
        "last_price_at": format_datetime(state.last_price_at),
        "last_order_at": format_datetime(state.last_order_at),
        "last_error": state.last_error,
        "last_error_at": format_datetime(state.last_error_at),
        "exchange_connected": state.exchange_connected,
        "telegram_connected": state.telegram_connected,
        "recent_warnings": state.recent_warnings[-10:],
        "alert_count": len(_load_alerts(active_only=True)),
        "portfolio": portfolio,
        "runtime_started_at": format_datetime(state.runtime_started_at),
        "version": APP_VERSION,
    }


@router.get("/balance")
def api_balance(
    config: Config = Depends(get_config),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Return the available fiat balance for the configured trading pair.

    Results are cached for 60 seconds to avoid hammering Kraken's private API with
    one request per dashboard tab. Errors are surfaced so the UI can explain why
    funds are currently unavailable instead of silently showing zero.
    """
    available, currency, error = _balance_cache.get(config)
    response: dict[str, Any] = {
        "fiat_currency": currency,
        "trading_pair": config.trading_pair,
    }
    if error is not None:
        response["available_fiat"] = None
        response["error"] = error
    else:
        response["available_fiat"] = round(available or 0.0, 2)
        response["error"] = None
    return response


@router.get("/portfolio")
def api_portfolio(
    config: Config = Depends(get_config),
    store: TransactionStore = Depends(get_store),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    pair = config.trading_pair
    total_amount, avg_price, last_price, total_spent = store.get_statistics(pair)
    current_price = state.last_price or last_price or 0.0
    current_value = total_amount * current_price

    holdings = []
    if total_amount > 0:
        pl_fiat = current_value - total_spent
        pl_percent = (pl_fiat / total_spent * 100) if total_spent > 0 else 0.0
        txns = store.get_transactions(pair)
        dates = [datetime.fromisoformat(t.date) for t in txns]
        holdings.append({
            "asset": _base_asset(pair),
            "trading_pair": pair,
            "total_quantity": round(total_amount, 8),
            "total_invested": round(total_spent, 2),
            "avg_buy_price": round(avg_price, 2),
            "current_price": round(current_price, 2),
            "current_value": round(current_value, 2),
            "unrealized_pl": round(pl_fiat, 2),
            "unrealized_pl_percent": round(pl_percent, 2),
            "purchase_count": len(txns),
            "first_purchase_at": format_datetime(min(dates)),
            "last_purchase_at": format_datetime(max(dates)),
            "allocation_percent": 100.0,
        })

    return {
        "holdings": holdings,
        "total_invested": round(total_spent, 2),
        "current_value": round(current_value, 2),
        "unrealized_pl": round(current_value - total_spent, 2),
        "purchase_count": len(holdings),
    }


def _base_asset(pair: str) -> str:
    # Naive but sufficient for Kraken pairs: strip quote
    quote = _quote_currency(pair)
    return pair.replace(quote, "").replace("Z", "").replace("X", "")


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@router.get("/transactions")
def api_transactions(
    request: Request,
    search: Optional[str] = None,
    sort: str = "date",
    order: str = "desc",
    asset: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    status: Optional[str] = None,
    strategy: Optional[str] = None,
    simulated: Optional[bool] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    store: TransactionStore = Depends(get_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    txns = list(store.transactions)

    if asset:
        txns = [t for t in txns if t.trading_pair.upper() == asset.upper()]
    if status:
        txns = [t for t in txns if t.status == status]
    if strategy:
        txns = [t for t in txns if t.strategy == strategy]
    if simulated is not None:
        txns = [t for t in txns if t.simulated == simulated]
    if from_date:
        try:
            fd = datetime.fromisoformat(from_date)
            txns = [t for t in txns if datetime.fromisoformat(t.date) >= fd]
        except ValueError:
            pass
    if to_date:
        try:
            td = datetime.fromisoformat(to_date)
            txns = [t for t in txns if datetime.fromisoformat(t.date) <= td]
        except ValueError:
            pass
    if search:
        s = search.lower()
        txns = [t for t in txns if s in t.order_id.lower() or s in t.trading_pair.lower() or s in t.strategy.lower()]

    reverse = order.lower() == "desc"
    txns.sort(key=lambda t: getattr(t, sort, t.date), reverse=reverse)

    total = len(txns)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = txns[start:end]

    return {
        "transactions": [t.to_dict() for t in page_items],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.get("/transactions/export")
def api_export_transactions(
    format: str = Query("json", pattern="^(json|csv)$"),
    store: TransactionStore = Depends(get_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    txns = [t.to_dict() for t in store.transactions]
    if format == "json":
        return Response(content=json.dumps(txns, indent=2), media_type="application/json")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=txns[0].keys() if txns else [])
    writer.writeheader()
    writer.writerows(txns)
    return Response(content=output.getvalue(), media_type="text/csv")


@router.delete("/transactions")
@csrf_protect
async def api_delete_all_transactions(
    request: Request,
    store: TransactionStore = Depends(get_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Delete all transactions. Demo mode will re-seed demo data on next restart."""
    count = store.clear()
    _audit_log("transactions_deleted", {"count": count}, request)
    return {"status": "ok", "message": f"Deleted {count} transactions.", "count": count}


# ---------------------------------------------------------------------------
# Performance & charts
# ---------------------------------------------------------------------------

@router.get("/performance")
def api_performance(
    config: Config = Depends(get_config),
    store: TransactionStore = Depends(get_store),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    pair = config.trading_pair
    txns = store.get_transactions(pair)
    quote = _quote_currency(pair)
    asset = _base_asset(pair)

    if not txns:
        return {
            "quote_currency": quote,
            "asset": asset,
            "total_invested": 0,
            "current_value": 0,
            "unrealized_pl": 0,
            "unrealized_pl_percent": 0,
            "total_amount": 0,
            "avg_buy_price": 0,
            "current_price": state.last_price or 0,
            "purchase_count": 0,
            "first_purchase_at": None,
            "last_purchase_at": None,
            "by_asset": [],
        }

    current_price = state.last_price or txns[-1].price
    total_amount = sum(t.amount for t in txns)
    total_spent = sum(t.amount * t.price for t in txns)
    current_value = total_amount * current_price
    avg_price = total_spent / total_amount if total_amount else 0
    pl_fiat = current_value - total_spent
    pl_percent = (pl_fiat / total_spent * 100) if total_spent else 0

    return {
        "quote_currency": quote,
        "asset": asset,
        "total_invested": round(total_spent, 2),
        "current_value": round(current_value, 2),
        "unrealized_pl": round(pl_fiat, 2),
        "unrealized_pl_percent": round(pl_percent, 2),
        "total_amount": round(total_amount, 8),
        "avg_buy_price": round(avg_price, 2),
        "current_price": round(current_price, 2),
        "purchase_count": len(txns),
        "first_purchase_at": format_datetime(txns[0].date),
        "last_purchase_at": format_datetime(txns[-1].date),
        "by_asset": [{
            "asset": asset,
            "quantity": round(total_amount, 8),
            "avg_price": round(avg_price, 2),
            "current_price": round(current_price, 2),
            "invested": round(total_spent, 2),
            "value": round(current_value, 2),
            "pl": round(pl_fiat, 2),
            "pl_percent": round(pl_percent, 2),
        }],
    }


@router.get("/charts/price")
def api_chart_price(
    range: str = Query("24h", pattern="^(1h|24h|7d|30d|90d|180d|365d|1y)$"),
    config: Config = Depends(get_config),
    store: TransactionStore = Depends(get_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Return OHLC price data for the configured trading pair plus bot buy markers."""
    pair = config.trading_pair
    now = int(time.time())
    range_settings = {
        "1h": {"minutes": 1, "seconds": 3600},
        "24h": {"minutes": 5, "seconds": 86400},
        "7d": {"minutes": 60, "seconds": 604800},
        "30d": {"minutes": 240, "seconds": 2592000},
        "90d": {"minutes": 240, "seconds": 7776000},
        "180d": {"minutes": 1440, "seconds": 15552000},
        "365d": {"minutes": 1440, "seconds": 31536000},
        "1y": {"minutes": 1440, "seconds": 31536000},
    }
    settings = range_settings[range]
    since = now - settings["seconds"]

    try:
        client: DemoKrakenAPI | KrakenAPI
        client = DemoKrakenAPI(config.api_key, config.api_secret) if is_demo_mode() else KrakenAPI(config.api_key, config.api_secret)
        candles = client.get_ohlc(pair, interval=settings["minutes"], since=since)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch market data: {e}")

    price_data = [
        {"x": datetime.fromtimestamp(c[0], tz=timezone.utc).isoformat(), "y": c[4]}
        for c in candles
    ]

    cutoff = now_tz() - timedelta(seconds=settings["seconds"])
    txns = [
        t
        for t in store.get_transactions(pair)
        if datetime.fromisoformat(t.date) >= cutoff
    ]
    buy_data = [
        {"x": t.date, "y": t.price, "strategy": t.strategy, "amount": t.amount}
        for t in txns
    ]

    return {
        "pair": pair,
        "range": range,
        "interval_minutes": settings["minutes"],
        "price_dataset": price_data,
        "buy_dataset": buy_data,
    }


@router.get("/charts/{name}")
def api_charts(
    name: str,
    range: str = Query("all", pattern="^(24h|7d|30d|90d|1y|all)$"),
    config: Config = Depends(get_config),
    store: TransactionStore = Depends(get_store),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    pair = config.trading_pair
    txns = store.get_transactions(pair)
    now = now_tz()
    range_map = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": None}
    days = range_map.get(range)
    cutoff = now - timedelta(days=days) if days else None
    filtered = txns
    if cutoff:
        filtered = [t for t in txns if datetime.fromisoformat(t.date) >= cutoff]

    if name == "portfolio_value":
        current_price = state.last_price or (txns[-1].price if txns else 0)
        labels = []
        values = []
        cumulative = 0.0
        for t in filtered:
            cumulative += t.amount
            labels.append(format_datetime(t.date))
            values.append(round(cumulative * current_price, 2))
        return {"labels": labels, "datasets": [{"label": "Portfolio value", "data": values}]}

    if name == "invested_vs_value":
        labels = []
        invested = []
        value = []
        cum_amount = 0.0
        cum_spent = 0.0
        for t in filtered:
            cum_amount += t.amount
            cum_spent += t.amount * t.price
            labels.append(format_datetime(t.date))
            invested.append(round(cum_spent, 2))
            value.append(round(cum_amount * t.price, 2))
        return {
            "labels": labels,
            "datasets": [
                {"label": "Total invested", "data": invested},
                {"label": "Current value", "data": value},
            ],
        }

    if name == "pl_over_time":
        labels = []
        data = []
        cum_amount = 0.0
        cum_spent = 0.0
        for t in filtered:
            cum_amount += t.amount
            cum_spent += t.amount * t.price
            labels.append(format_datetime(t.date))
            data.append(round(cum_amount * t.price - cum_spent, 2))
        return {"labels": labels, "datasets": [{"label": "Unrealized P/L", "data": data}]}

    if name == "purchase_history":
        labels = []
        data = []
        for t in filtered:
            labels.append(format_datetime(t.date))
            data.append(round(t.amount * t.price, 2))
        return {"labels": labels, "datasets": [{"label": "Purchase amount", "data": data}]}

    if name == "avg_vs_market":
        labels = []
        avg = []
        market = []
        cum_amount = 0.0
        cum_spent = 0.0
        for t in filtered:
            cum_amount += t.amount
            cum_spent += t.amount * t.price
            labels.append(format_datetime(t.date))
            avg.append(round(cum_spent / cum_amount, 2) if cum_amount else 0)
            market.append(round(t.price, 2))
        return {
            "labels": labels,
            "datasets": [
                {"label": "Average buy price", "data": avg},
                {"label": "Market price", "data": market},
            ],
        }

    if name == "cumulative_invested":
        labels = []
        data = []
        cum = 0.0
        for t in filtered:
            cum += t.amount * t.price
            labels.append(format_datetime(t.date))
            data.append(round(cum, 2))
        return {"labels": labels, "datasets": [{"label": "Cumulative invested", "data": data}]}

    if name == "activity_timeline":
        counts: dict[str, int] = {}
        for t in filtered:
            d = datetime.fromisoformat(t.date).strftime("%Y-%m-%d")
            counts[d] = counts.get(d, 0) + 1
        date_labels = sorted(counts.keys())
        return {"labels": date_labels, "datasets": [{"label": "Buys per day", "data": [counts[date_label] for date_label in date_labels]}]}

    if name == "success_vs_failed":
        success = len([t for t in filtered if t.status == "filled"])
        failed = len([t for t in filtered if t.status == "failed"])
        return {"labels": ["Filled", "Failed"], "datasets": [{"data": [success, failed]}]}

    raise HTTPException(status_code=404, detail="Chart not found")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@router.get("/settings")
def api_settings(
    config: Config = Depends(get_config),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    data = config.to_dict(mask_secrets=True)
    data["requires_restart"] = False  # UI hint; true for critical fields handled separately
    data["market"] = _market_context(config, state)
    return data


def _market_context(config: Config, state: BotState) -> dict[str, Any]:
    """Quote currency, last known price and exchange order minimum.

    Used by the settings page to show the fiat value of configured amounts
    and to flag tiers below Kraken's minimum order size. Best-effort: any
    failure (offline, demo mode, unknown pair) yields None, never an error.
    """
    context: dict[str, Any] = {
        "quote_currency": (
            _quote_currency(config.trading_pair) if config.trading_pair else ""
        ),
        "last_price": state.last_price,
        "order_min": None,
    }
    if config.trading_pair and not is_demo_mode():
        try:
            info = KrakenAPI("", "").get_asset_pair_info(
                config.trading_pair, timeout=5
            )
            context["order_min"] = float(info["ordermin"])
        except Exception:
            context["order_min"] = None
    return context


@router.put("/settings")
@csrf_protect
async def api_update_settings(
    request: Request,
    update: SettingsUpdate,
    config: Config = Depends(get_config),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    # Load existing config, merge updates, validate, write atomically
    config_path = config.config_path
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    update_dict = update.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        if key == "dynamic_dca":
            data[key] = _deep_merge(data.get(key, {}), value)
        else:
            data[key] = value

    # Backup current config
    backup_path = config_path.with_suffix(".json.bak")
    shutil.copy2(config_path, backup_path)

    try:
        atomic_write_json(config_path, data, indent=2)
        # Validate by reloading
        Config(config_path=config_path)
        # Update the in-memory config so the dashboard reflects changes immediately
        config.reload()
    except Exception as e:
        # Restore backup
        shutil.copy2(backup_path, config_path)
        config.reload()
        raise HTTPException(status_code=400, detail=f"Invalid settings: {e}")

    _audit_log("config_updated", update_dict, request)
    return {"status": "ok", "requires_restart": True, "message": "Settings saved. Restart the bot to apply critical changes."}


@router.post("/settings/restore")
@csrf_protect
async def api_restore_settings(
    request: Request,
    config: Config = Depends(get_config),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    config_path = config.config_path
    backup_path = config_path.with_suffix(".json.bak")
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="No backup available")
    shutil.copy2(backup_path, config_path)
    _audit_log("config_restored", {}, request)
    return {"status": "ok", "message": "Previous configuration restored."}


@router.get("/settings/history")
def api_settings_history(
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    return {"history": _load_audit_log()}


@router.get("/settings/runtime")
def api_settings_runtime(
    config: Config = Depends(get_config),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Return the current server-side configuration as seen by the bot process."""
    data = config.to_dict(mask_secrets=True)
    data["source"] = str(config.config_path)
    data["loaded_at"] = format_datetime(getattr(config, "loaded_at", None))
    return data


# ---------------------------------------------------------------------------
# Exchange credentials (Kraken)
# ---------------------------------------------------------------------------

@router.get("/settings/exchange")
def api_exchange_status(
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Masked status of Kraken credentials: stored vs. environment vs. unset."""
    return SecretsStore().public_status()


@router.put("/settings/exchange")
@csrf_protect
async def api_update_exchange(
    request: Request,
    update: ExchangeCredentialsUpdate,
    config: Config = Depends(get_config),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Persist Kraken credentials to the secrets store (data/secrets.json, 0600).

    Environment variables remain authoritative: if KRAKEN_API_KEY /
    KRAKEN_API_SECRET are set in the container, they win over the stored values.
    """
    store = SecretsStore()
    store.save_exchange_credentials(update.api_key.strip(), update.api_secret.strip())
    # Re-read config so the running bot loop notices the new credentials
    # without requiring a restart (it idles until configured).
    try:
        config.reload()
    except Exception:
        pass  # keep the 200; validation errors surface via preflight
    _audit_log("exchange_credentials_updated", {"source": "secrets_store"}, request)
    status = store.public_status()
    configured = getattr(config, "configured", True)
    message = (
        "Credentials saved. The bot picks them up automatically."
        if configured
        else "Credentials saved. Configure the strategy (Settings -> Strategy) to finish setup."
    )
    return {
        "status": "ok",
        "message": message,
        "exchange": status,
    }


@router.post("/settings/exchange/clear")
@csrf_protect
async def api_clear_exchange(
    request: Request,
    config: Config = Depends(get_config),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Remove Kraken credentials from the secrets store. Env vars are untouched."""
    store = SecretsStore()
    removed = store.clear_exchange_credentials()
    try:
        config.reload()
    except Exception:
        pass
    _audit_log("exchange_credentials_cleared", {"removed": removed}, request)
    return {
        "status": "ok",
        "message": "Stored credentials removed." if removed else "No stored credentials to remove.",
        "exchange": store.public_status(),
    }


# ---------------------------------------------------------------------------
# Authentication status (local + OIDC)
# ---------------------------------------------------------------------------

@router.get("/settings/auth")
def api_auth_status(
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Masked status of dashboard authentication: local login and OIDC."""
    store = SecretsStore()
    web_status = store.section_status("web")
    return {
        "local": {
            "username": auth_manager.username,
            "username_source": web_status["fields"]["username"]["source"],
            "password_hash": web_status["fields"]["password_hash"],
            "session_secret": web_status["fields"]["session_secret"],
        },
        "oidc": store.section_status("oidc"),
        "oidc_enabled": oidc_provider.enabled,
    }


@router.put("/settings/web")
@csrf_protect
async def api_update_web_auth(
    request: Request,
    update: WebAuthUpdate,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Set the local dashboard admin (username and/or password).

    The password is bcrypt-hashed before it is stored; the raw password is
    never persisted or returned. Environment variables (WEB_UI_USERNAME /
    WEB_UI_PASSWORD_HASH) remain authoritative when set. Changes apply to new
    logins immediately; existing sessions stay valid.
    """
    values: dict[str, str] = {}
    if update.username is not None and update.username.strip():
        values["username"] = update.username.strip()
    if update.password is not None:
        values["password_hash"] = bcrypt.hashpw(update.password.encode(), bcrypt.gensalt()).decode()
    if not values:
        raise HTTPException(status_code=400, detail="Nothing to update")
    store = SecretsStore()
    store.save_section("web", values)
    _audit_log("web_auth_updated", {"fields": sorted(values.keys())}, request)
    return {
        "status": "ok",
        "message": "Local admin saved. Applies to new logins immediately; existing sessions stay valid. "
                   "If the environment variables are set, they take precedence.",
        "web": store.section_status("web"),
    }


@router.post("/settings/web/clear")
@csrf_protect
async def api_clear_web_auth(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Remove stored local-admin credentials. Env vars and active sessions are untouched."""
    store = SecretsStore()
    removed = store.clear_section("web")
    _audit_log("web_auth_cleared", {"removed": removed}, request)
    return {
        "status": "ok",
        "message": "Stored admin credentials removed." if removed else "No stored admin credentials to remove.",
        "web": store.section_status("web"),
    }


@router.put("/settings/oidc")
@csrf_protect
async def api_update_oidc(
    request: Request,
    update: OIDCSettingsUpdate,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Persist Authentik (OIDC) settings to the secrets store.

    Empty strings are treated as "leave unchanged". Enabling OIDC requires an
    issuer URL and a client ID in the resulting effective configuration.
    Environment variables remain authoritative when set.
    """
    update_dict = update.model_dump(exclude_unset=True)
    store = SecretsStore()

    # Determine the effective "enabled" flag after this update.
    enabled: Optional[bool] = update_dict.get("enabled")
    if enabled is None:
        enabled = _as_bool(store.resolve("oidc").get("enabled"))

    if enabled:
        effective = store.resolve("oidc")
        issuer = (update_dict.get("issuer_url") or effective.get("issuer_url") or "").strip()
        client_id = (update_dict.get("client_id") or effective.get("client_id") or "").strip()
        if not issuer or not client_id:
            raise HTTPException(
                status_code=400,
                detail="Enabling OIDC requires an issuer URL and a client ID (now or already stored).",
            )

    values: dict[str, str] = {}
    for field in ("issuer_url", "client_id", "client_secret", "redirect_uri", "scopes"):
        value = update_dict.get(field)
        if value is not None and str(value).strip():
            values[field] = str(value).strip()
    if "enabled" in update_dict:
        values["enabled"] = "true" if update_dict["enabled"] else "false"
    if values:
        store.save_section("oidc", values)
    _audit_log("oidc_updated", {"fields": sorted(values.keys())}, request)
    return {
        "status": "ok",
        "message": "OIDC settings saved. They take effect immediately unless the environment variables are set, which take precedence.",
        "oidc": store.section_status("oidc"),
        "oidc_enabled": oidc_provider.enabled,
    }


@router.post("/settings/oidc/clear")
@csrf_protect
async def api_clear_oidc(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Remove stored OIDC settings. Env vars are untouched."""
    store = SecretsStore()
    removed = store.clear_section("oidc")
    _audit_log("oidc_cleared", {"removed": removed}, request)
    return {
        "status": "ok",
        "message": "Stored OIDC settings removed." if removed else "No stored OIDC settings to remove.",
        "oidc": store.section_status("oidc"),
        "oidc_enabled": oidc_provider.enabled,
    }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

@router.get("/telegram")
def api_telegram(
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    notifier = Notifier()
    return {
        **notifier.to_dict(mask_secrets=True),
        "storage": SecretsStore().section_status("telegram"),
    }


@router.put("/telegram")
@csrf_protect
async def api_update_telegram(
    request: Request,
    update: TelegramUpdate,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Persist Telegram credentials to the secrets store (0600, excluded from backups).

    Environment variables TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID remain
    authoritative when set; stored values apply after a restart.
    """
    values = {}
    if update.bot_token:
        if len(update.bot_token) < 10:
            raise HTTPException(status_code=400, detail="Invalid bot token")
        values["bot_token"] = update.bot_token
    if update.chat_id:
        values["chat_id"] = update.chat_id
    store = SecretsStore()
    if values:
        store.save_section("telegram", values)
    _audit_log("telegram_updated", {"fields": sorted(values.keys())}, request)
    return {
        "status": "ok",
        "message": "Telegram settings saved. Restart the bot to apply them "
                   "(unless the environment variables are set, which take precedence).",
        "storage": store.section_status("telegram"),
    }


@router.post("/telegram/clear")
@csrf_protect
async def api_clear_telegram(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Remove stored Telegram credentials. Env vars are untouched."""
    store = SecretsStore()
    removed = store.clear_section("telegram")
    _audit_log("telegram_cleared", {"removed": removed}, request)
    return {
        "status": "ok",
        "message": "Stored Telegram credentials removed." if removed else "No stored Telegram credentials to remove.",
        "storage": store.section_status("telegram"),
    }


@router.post("/telegram/test")
@csrf_protect
async def api_telegram_test(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    notifier = Notifier()
    success, message = notifier.test()
    return {"success": success, "message": message}


# ---------------------------------------------------------------------------
# Bot controls
# ---------------------------------------------------------------------------

@router.post("/bot/pause")
@csrf_protect
async def api_bot_pause(
    request: Request,
    overrides: RuntimeOverrides = Depends(get_overrides),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    overrides.set_paused(True, reason="Paused via dashboard")
    state.update(paused=True, status="paused")
    state.wake()
    _audit_log("bot_paused", {}, request)
    return {"status": "ok", "message": "Bot paused. New purchases will not be made."}


@router.post("/bot/resume")
@csrf_protect
async def api_bot_resume(
    request: Request,
    overrides: RuntimeOverrides = Depends(get_overrides),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    overrides.set_paused(False)
    state.update(paused=False, status="waiting")
    state.wake()
    _audit_log("bot_resumed", {}, request)
    return {"status": "ok", "message": "Bot resumed."}


@router.post("/bot/cycle")
@csrf_protect
async def api_bot_cycle(
    request: Request,
    overrides: RuntimeOverrides = Depends(get_overrides),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(manual_buy_rate_limit),
):
    if state.status == "stopped":
        raise HTTPException(status_code=503, detail="Bot is not running. Please restart the bot.")
    if state.paused:
        raise HTTPException(status_code=409, detail="Bot is paused. Resume the bot before buying.")
    over_budget = False
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            over_budget = bool(payload.get("over_budget"))
    except Exception:
        pass  # no body (older frontends) — plain manual buy
    overrides.request_manual_cycle(over_budget=over_budget)
    state.wake()
    _audit_log("bot_manual_cycle", {"over_budget": over_budget}, request)
    message = "One-time buy requested"
    if over_budget:
        message += " (approved over monthly budget)"
    message += ". The bot will place the order shortly."
    return {"status": "ok", "message": message}


@router.post("/bot/stop")
@csrf_protect
async def api_bot_stop(
    request: Request,
    overrides: RuntimeOverrides = Depends(get_overrides),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    overrides.request_stop()
    state.wake()
    _audit_log("bot_stop", {}, request)
    return {"status": "ok", "message": "Stop requested. The bot will shut down gracefully."}


@router.post("/bot/restart")
@csrf_protect
async def api_bot_restart(
    request: Request,
    overrides: RuntimeOverrides = Depends(get_overrides),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Restart the bot process; the container restart policy brings it back.

    The endpoint responds first, then the process exits cleanly after a short
    delay so the response can flush. A process exit (unlike `podman stop`) is
    treated by the container runtime as a crash/exit, so the configured
    restart policy (compose: `restart: always`) starts a fresh process, which
    re-reads config.json and the secrets store.

    In development mode the exit is skipped so the test suite and local runs
    are not killed; production and staging behave as described above.
    """
    _audit_log("bot_restart", {}, request)

    if os.environ.get("APP_ENV", "production").lower() in ("production", "staging"):
        def _exit_after_flush() -> None:
            time.sleep(1.5)
            os._exit(0)

        threading.Thread(target=_exit_after_flush, daemon=True).start()
        message = "Restarting the bot now. The dashboard will be unavailable for a few seconds."
    else:
        message = "Development mode: restart exit skipped. The bot keeps running."
    return {"status": "ok", "message": message}


# ---------------------------------------------------------------------------
# Order attempts
# ---------------------------------------------------------------------------

def _attempt_to_dict(attempt):
    data = attempt.to_dict()
    # Redact any accidental secret leakage just in case.
    data.pop("userref", None)
    return data


@router.get("/orders")
def api_orders(
    state: str | None = Query(None),
    store: OrderAttemptStore = Depends(get_attempt_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """List persisted order attempts."""
    attempts = store.list_all()
    if state:
        attempts = [a for a in attempts if a.state == state]
    return {"orders": [_attempt_to_dict(a) for a in attempts]}


@router.get("/orders/{attempt_id}")
def api_order_detail(
    attempt_id: str,
    store: OrderAttemptStore = Depends(get_attempt_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    attempt = store.get(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Order attempt not found")
    return {"order": _attempt_to_dict(attempt)}


@router.post("/orders/{attempt_id}/cancel")
@csrf_protect
async def api_cancel_order(
    request: Request,
    attempt_id: str,
    store: OrderAttemptStore = Depends(get_attempt_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Cancel a non-terminal order attempt."""
    executor = OrderExecutor(api=None, store=None, attempt_store=store, notifier=Notifier())
    attempt = executor.cancel_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Order attempt not found")
    _audit_log("order_cancelled", {"attempt_id": attempt_id}, request)
    return {"status": "ok", "order": _attempt_to_dict(attempt)}


@router.post("/orders/{attempt_id}/acknowledge")
@csrf_protect
async def api_acknowledge_order(
    request: Request,
    attempt_id: str,
    store: OrderAttemptStore = Depends(get_attempt_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Acknowledge a held order attempt."""
    executor = OrderExecutor(api=None, store=None, attempt_store=store, notifier=Notifier())
    attempt = executor.acknowledge_hold(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Order attempt not found")
    _audit_log("order_acknowledged", {"attempt_id": attempt_id}, request)
    return {"status": "ok", "order": _attempt_to_dict(attempt)}


@router.post("/orders/{attempt_id}/resolve")
@csrf_protect
async def api_resolve_order(
    request: Request,
    attempt_id: str,
    store: OrderAttemptStore = Depends(get_attempt_store),
    tx_store: TransactionStore = Depends(get_store),
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Operator resolution for an unknown/failed order attempt.

    Body JSON must contain ``action`` in ("confirm", "retry", "cancel").
    """
    body = await request.json()
    action = str(body.get("action", "")).lower()
    if action not in ("confirm", "retry", "cancel"):
        raise HTTPException(status_code=400, detail="Invalid action")

    executor = OrderExecutor(api=None, store=tx_store, attempt_store=store, notifier=Notifier())
    attempt = executor.resolve_unknown(attempt_id, action)
    if not attempt:
        raise HTTPException(status_code=404, detail="Order attempt not found")
    _audit_log("order_resolved", {"attempt_id": attempt_id, "action": action}, request)
    return {"status": "ok", "order": _attempt_to_dict(attempt)}


# ---------------------------------------------------------------------------
# Logs & health
# ---------------------------------------------------------------------------

def _load_json_lines(log_path: Path) -> list[dict[str, Any]]:
    """Read a JSON-lines log file, skipping corrupt lines."""
    entries: list[dict[str, Any]] = []
    if not log_path.exists():
        return entries
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


@router.get("/logs")
def api_logs(
    level: Optional[str] = None,
    search: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=1000),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    log_path = Path("logs/app.json")
    entries = _load_json_lines(log_path)

    if level:
        entries = [e for e in entries if e.get("level", "").lower() == level.lower()]
    if search:
        s = search.lower()
        entries = [e for e in entries if s in str(e.get("message", "")).lower()]
    if from_date:
        entries = [e for e in entries if e.get("timestamp", "") >= from_date]
    if to_date:
        entries = [e for e in entries if e.get("timestamp", "") <= to_date]

    total = len(entries)
    start = (page - 1) * per_page
    return {
        "logs": entries[start:start + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/logs/download")
def api_logs_download(
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    log_path = Path("logs/app.json")
    entries = _load_json_lines(log_path)
    return Response(content=json.dumps(entries, indent=2), media_type="application/json")


@router.post("/logs/archive")
@csrf_protect
async def api_archive_logs(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Copy the current log file to an archive and truncate the live log."""
    log_path = Path("logs/app.json")
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="No log file to archive")

    archive_dir = log_path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_tz().strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"app-{timestamp}.json"

    try:
        shutil.copy2(log_path, archive_path)
        with open(log_path, "w", encoding="utf-8"):
            pass
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not archive logs: {e}")

    _audit_log("logs_archived", {"archive": str(archive_path)}, request)
    return {"status": "ok", "archive": str(archive_path), "message": f"Logs archived to {archive_path.name}"}


@router.get("/health")
def api_health(
    config: Config = Depends(get_config),
    state: BotState = Depends(get_state),
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    # Check exchange API using a public endpoint so the health check does not
    # consume the private API rate-limit counter.
    exchange_ok = False
    try:
        if is_demo_mode():
            exchange_ok = True
        else:
            client = KrakenAPI(config.api_key, config.api_secret)
            client.get_system_status()
            exchange_ok = True
    except Exception:
        exchange_ok = False

    # Check Telegram
    notifier = Notifier()
    telegram_ok = notifier.enabled

    # Check storage
    storage_ok = config.config_path.exists() and Path("data/transactions.json").exists()

    runtime = ""
    if state.runtime_started_at:
        runtime = str(now_tz() - state.runtime_started_at)

    return {
        "bot_process": state.status in ("running", "waiting", "paused"),
        "backend": True,
        "exchange_api": exchange_ok,
        "telegram_api": telegram_ok,
        "storage": storage_ok,
        "last_price_update": format_datetime(state.last_price_at),
        "last_order": format_datetime(state.last_order_at),
        "version": APP_VERSION,
        "runtime": runtime,
        "server_time": format_datetime(now_tz()),
        "timezone": str(DISPLAY_TZ),
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@router.get("/alerts")
def api_alerts(
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    alerts = _load_alerts()
    return {"alerts": alerts}


@router.post("/alerts/{alert_id}/acknowledge")
@csrf_protect
async def api_ack_alert(
    request: Request,
    alert_id: str,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    alerts = _load_alerts()
    for alert in alerts:
        if alert.get("id") == alert_id:
            alert["acknowledged"] = True
            _save_alerts(alerts)
            return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Alert not found")


@router.post("/alerts/acknowledge-all")
@csrf_protect
async def api_ack_all_alerts(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    """Acknowledge every active alert at once."""
    alerts = _load_alerts()
    acknowledged = 0
    for alert in alerts:
        if not alert.get("acknowledged"):
            alert["acknowledged"] = True
            acknowledged += 1
    if acknowledged:
        _save_alerts(alerts)
    _audit_log("alerts_acknowledged_all", {"count": acknowledged}, request)
    return {"status": "ok", "acknowledged": acknowledged}


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

@router.post("/backups/create")
@csrf_protect
async def api_create_backup(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(settings_rate_limit),
):
    backup_dir = Path("backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = backup_dir / f"crypto-agent-data-{timestamp}.tar.gz"

    import tarfile
    with tarfile.open(backup_file, "w:gz") as tar:
        for path in ["config.json", "data/transactions.json"]:
            p = Path(path)
            if p.exists():
                tar.add(p, arcname=p.name)

    backup_file.chmod(0o600)
    _audit_log("backup_created", {"file": str(backup_file)}, request)
    return {"status": "ok", "file": str(backup_file.name)}


@router.get("/backups")
def api_list_backups(
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    backup_dir = Path("backups")
    if not backup_dir.exists():
        return {"backups": []}
    files = sorted(backup_dir.glob("crypto-agent-data-*.tar.gz"), reverse=True)
    return {"backups": [{"name": f.name, "size": f.stat().st_size, "created": format_datetime(datetime.fromtimestamp(f.stat().st_mtime))} for f in files]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUDIT_LOG_PATH = Path("data/audit_log.json")
ALERTS_PATH = Path("data/alerts.json")


def _audit_log(action: str, details: dict, request: Request) -> None:
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": utc_now().isoformat(),
        "action": action,
        "details": details,
        "source_ip": get_remote_address(request),
        "user": auth_manager.get_session_username(request) or "unknown",
    }
    entries = safe_load_json(AUDIT_LOG_PATH)
    if not isinstance(entries, list):
        entries = []
    entries.append(entry)
    atomic_write_json(AUDIT_LOG_PATH, entries[-500:], indent=2)


def _load_audit_log() -> list:
    entries = safe_load_json(AUDIT_LOG_PATH)
    return entries if isinstance(entries, list) else []


def _load_alerts(active_only: bool = True) -> list:
    alerts = safe_load_json(ALERTS_PATH)
    if not isinstance(alerts, list):
        return []
    if active_only:
        return [a for a in alerts if not a.get("acknowledged")]
    return alerts


def _save_alerts(alerts: list) -> None:
    atomic_write_json(ALERTS_PATH, alerts, indent=2)
