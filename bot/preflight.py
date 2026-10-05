"""Read-only production preflight checks.

The preflight proves that the bot is correctly configured before any real Kraken
order can be sent. It is deliberately read-only: it may call Kraken's public API
and read-only private endpoints (e.g. Balance), but it never places an order.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bot.api_client import KrakenAPI
from bot.config import Config
from bot.demo import DemoKrakenAPI, is_demo_mode
from bot.notifier import Notifier
from bot.state import BotState, RuntimeOverrides
from bot.persistence import JsonFile
from bot.utils import safe_load_json, utc_now

# How long a preflight result remains valid. After this it must be re-run.
DEFAULT_RESULT_EXPIRY_SECONDS = int(os.environ.get("PREFLIGHT_EXPIRY_SECONDS", "3600"))

# Maximum acceptable age of the trading-loop heartbeat.
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = int(
    os.environ.get("PREFLIGHT_HEARTBEAT_MAX_AGE_SECONDS", "900")
)

# Safety margin added on top of the estimated Kraken taker fee.
DEFAULT_FEE_BUFFER_PERCENT = float(
    os.environ.get("PREFLIGHT_FEE_BUFFER_PERCENT", "0.5")
)

# Estimated Kraken taker fee for the lowest API volume tier (market order).
KRAKEN_TAKER_FEE_RATE = 0.0026

PREFLIGHT_RESULT_FILE = Path("data/preflight.json")


class CheckStatus:
    """Preflight check classification."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# Registry of all preflight checks. The dashboard uses this to render the
# selectable check list; ``ProductionPreflight`` uses it to validate which
# checks may be disabled. Keep this in sync with the ``_add`` calls below.
CHECK_CATALOG: dict[str, dict[str, str]] = {
    # environment
    "app_env_production": {"category": "environment", "description": "APP_ENV is 'production' (required for live orders)"},
    "demo_mode_disabled": {"category": "environment", "description": "Demo mode is off"},
    "live_trading_enabled": {"category": "environment", "description": "Live trading flag is on (env overrides the config/top-bar toggle)"},
    "bot_not_paused": {"category": "environment", "description": "Bot is not paused"},
    "trading_loop_state": {"category": "environment", "description": "Trading loop is running or waiting"},
    # credentials
    "kraken_credentials_present": {"category": "credentials", "description": "Kraken API credentials are configured (environment or secrets store)"},
    "kraken_authentication": {"category": "credentials", "description": "Credentials authenticate against Kraken"},
    "kraken_minimum_permissions": {"category": "credentials", "description": "Manual confirmation: key has 'Query Funds' and 'Create & Modify Orders' only"},
    "kraken_withdrawal_permission": {"category": "credentials", "description": "Manual confirmation: 'Withdraw Funds' permission is disabled"},
    # security
    "session_secret_present": {"category": "security", "description": "Session signing secret is set (environment or secrets store)"},
    "password_or_oidc_configured": {"category": "security", "description": "Dashboard login is protected (local password or OIDC)"},
    "secure_cookie": {"category": "security", "description": "Secure cookies are enforced in production"},
    "csrf_active": {"category": "security", "description": "CSRF protection is active"},
    "rate_limiting_active": {"category": "security", "description": "API rate limiting is enabled"},
    # filesystem
    "data_dir_writable": {"category": "filesystem", "description": "Data directory is writable"},
    "logs_dir_writable": {"category": "filesystem", "description": "Log directory is writable"},
    "backups_dir_writable": {"category": "filesystem", "description": "Backup directory is writable"},
    # market
    "pair_exists": {"category": "market", "description": "Trading pair resolves on Kraken"},
    "pair_metadata_loaded": {"category": "market", "description": "Pair metadata (lot/ordermin/costmin) is loaded"},
    "ticker_available": {"category": "market", "description": "Current price is available"},
    # amount
    "amount_positive": {"category": "amount", "description": "Configured buy amount is positive"},
    "amount_precision": {"category": "amount", "description": "Amount matches the pair's lot precision"},
    "volume_above_minimum": {"category": "amount", "description": "Amount is above Kraken's order minimum"},
    "cost_above_minimum": {"category": "amount", "description": "Estimated order cost is above Kraken's cost minimum"},
    # balance
    "balance_readable": {"category": "balance", "description": "Account balance can be read"},
    "balance_sufficient": {"category": "balance", "description": "Available quote currency covers the next buy incl. fees"},
    "fee_buffer_included": {"category": "balance", "description": "Taker fee and safety buffer are included in the estimate"},
    # order_state
    "no_hold_state": {"category": "order_state", "description": "Bot is not in HOLD state"},
    "no_unknown_orders": {"category": "order_state", "description": "No order attempts with unknown outcome"},
    "no_duplicate_attempts": {"category": "order_state", "description": "No duplicate non-terminal order attempts"},
    "next_cycle_sensible": {"category": "order_state", "description": "Next cycle time is plausible"},
    "heartbeat_recent": {"category": "order_state", "description": "Trading-loop heartbeat is fresh"},
    # telegram
    "telegram_configured": {"category": "telegram", "description": "Telegram notifier is configured"},
    "telegram_test": {"category": "telegram", "description": "Telegram test message (only when explicitly requested)"},
    # system
    "system_time_plausible": {"category": "system", "description": "System clock is plausible (year and timezone)"},
}


def check_catalog() -> list[dict[str, str]]:
    """Return the check catalog as a list of {name, category, description} dicts."""
    return [
        {"name": name, "category": info["category"], "description": info["description"]}
        for name, info in CHECK_CATALOG.items()
    ]


@dataclass
class CheckResult:
    """A single preflight check result."""

    name: str
    status: str
    message: str
    category: str = "general"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "message": self.message, "category": self.category}


@dataclass
class PreflightResult:
    """Aggregated preflight result."""

    preflight_id: str
    ran_at: str
    expires_at: str
    config_hash: str
    overall: str
    can_place_live_orders: bool
    checks: list[CheckResult]
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def to_dict(self, redact: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "preflight_id": self.preflight_id,
            "ran_at": self.ran_at,
            "expires_at": self.expires_at,
            "config_hash": self.config_hash,
            "overall": self.overall,
            "can_place_live_orders": self.can_place_live_orders,
            "checks": [c.to_dict() for c in self.checks],
            "warnings": list(self.warnings),
            "failures": list(self.failures),
        }
        if redact:
            data = _redact_data(data)
        return data


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "production").lower() == "production"


def _quote_currency(pair: str) -> str:
    """Extract the quote (fiat) currency from a Kraken pair."""
    p = pair[1:] if pair.startswith("X") else pair
    quote = p[-3:]
    return quote[1:] if quote.startswith("Z") else quote


def _config_hash(config: Config) -> str:
    """Stable hash of settings that affect order safety."""
    data = config.to_dict(mask_secrets=True)
    data["api_key_set"] = bool(config.api_key)
    data["api_secret_set"] = bool(config.api_secret)
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _redact_data(data: Any) -> Any:
    """Recursively redact common secret keys from a JSON-serialisable structure."""
    secret_keys = {
        "api_key",
        "api_secret",
        "api_sign",
        "bot_token",
        "telegram_bot_token",
        "session_secret",
        "password",
        "password_hash",
        "client_secret",
    }
    if isinstance(data, dict):
        return {
            k: "[REDACTED]" if isinstance(k, str) and k.lower() in secret_keys else _redact_data(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact_data(v) for v in data]
    return data


def _dir_writable(path: Path) -> bool:
    """Return True if a directory can be written to by the current process."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path))
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _now() -> datetime:
    return utc_now()


def _preflight_json_file(path: str | Path | None = None) -> JsonFile:
    return JsonFile(path or PREFLIGHT_RESULT_FILE)


def write_preflight_result(path: str | Path | None, result: PreflightResult) -> None:
    """Persist a preflight result atomically under a cross-process lock."""
    _preflight_json_file(path).save(result.to_dict(redact=False), indent=2)


def seed_green_preflight_result(
    path: Path,
    config: Config,
    expiry_seconds: int = DEFAULT_RESULT_EXPIRY_SECONDS,
) -> PreflightResult:
    """Write a synthetic green preflight result for tests or bootstrap.

    This is intentionally separate from ``run()`` so tests and tooling can
    create a known-good result without calling Kraken.
    """
    ran_at = _now()
    expires_at = ran_at + timedelta(seconds=expiry_seconds)
    result = PreflightResult(
        preflight_id=str(uuid.uuid4()),
        ran_at=ran_at.isoformat(),
        expires_at=expires_at.isoformat(),
        config_hash=_config_hash(config),
        overall=CheckStatus.PASS,
        can_place_live_orders=True,
        checks=[
            CheckResult(
                name="seeded",
                status=CheckStatus.PASS,
                message="Green preflight result seeded",
                category="test",
            )
        ],
    )
    write_preflight_result(path, result)
    return result


def load_preflight_result(path: str | Path = PREFLIGHT_RESULT_FILE) -> PreflightResult | None:
    """Load a previously persisted preflight result, if any."""
    data = _preflight_json_file(path).load()
    if not isinstance(data, dict):
        return None
    checks = [
        CheckResult(
            name=c.get("name", "unknown"),
            status=c.get("status", CheckStatus.FAIL),
            message=c.get("message", ""),
            category=c.get("category", "general"),
        )
        for c in data.get("checks", [])
        if isinstance(c, dict)
    ]
    return PreflightResult(
        preflight_id=data.get("preflight_id", ""),
        ran_at=data.get("ran_at", ""),
        expires_at=data.get("expires_at", ""),
        config_hash=data.get("config_hash", ""),
        overall=data.get("overall", CheckStatus.FAIL),
        can_place_live_orders=bool(data.get("can_place_live_orders", False)),
        checks=checks,
        warnings=list(data.get("warnings", [])),
        failures=list(data.get("failures", [])),
    )


def latest_preflight_is_valid(
    config: Config,
    path: str | Path | None = None,
    max_age_seconds: int = DEFAULT_RESULT_EXPIRY_SECONDS,
) -> tuple[bool, str]:
    """Return (ok, reason) for whether a current green preflight result exists."""
    if path is None:
        path = PREFLIGHT_RESULT_FILE
    result = load_preflight_result(path)
    if result is None:
        return False, "no preflight result has been generated"

    expires_at = _parse_iso_timestamp(result.expires_at)
    if expires_at is None or _now() > expires_at:
        return False, "preflight result has expired"

    if result.overall == CheckStatus.FAIL:
        return False, "most recent preflight result reported a failure"

    if result.config_hash and result.config_hash != _config_hash(config):
        return False, "configuration has changed since the preflight was run"

    return True, "ok"


class ProductionPreflight:
    """Run read-only preflight checks before live trading is permitted."""

    def __init__(
        self,
        config: Config,
        api: KrakenAPI | DemoKrakenAPI | None = None,
        state: BotState | None = None,
        overrides: RuntimeOverrides | None = None,
        attempt_store: Any = None,
        notifier: Notifier | None = None,
        data_dir: str | Path = "data",
        backup_dir: str | Path = "backups",
        disabled_checks: list[str] | None = None,
    ) -> None:
        self.config = config
        self.api = api
        self.state = state or BotState(persist=False)
        self.overrides = overrides or RuntimeOverrides()
        self.attempt_store = attempt_store
        self.notifier = notifier or Notifier()
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir)
        self.disabled_checks = set(disabled_checks or [])
        self._results: list[CheckResult] = []
        self._pair_info: dict[str, Any] | None = None
        self._price: float | None = None
        self._balance: dict[str, float] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        telegram_test: bool = False,
        pair: str | None = None,
        amount: float | None = None,
        expiry_seconds: int = DEFAULT_RESULT_EXPIRY_SECONDS,
    ) -> PreflightResult:
        """Run all preflight checks and return an aggregated result."""
        self._results = []
        self._pair_info = None
        self._price = None
        self._balance = None
        # Checks disabled for this run: constructor argument plus the
        # dashboard-managed disabled list from config.json.
        self._disabled = set(self.disabled_checks) | set(
            getattr(self.config, "preflight_disabled_checks", []) or []
        )

        self._check_environment()
        self._check_credentials()
        self._check_security()
        self._check_filesystem()
        self._check_pair_and_market(pair_override=pair)
        self._check_amount(amount_override=amount)
        self._check_balance()
        self._check_order_state()
        self._check_telegram(telegram_test)
        self._check_time()

        overall, can_place, warnings, failures = self._summarize()
        ran_at = _now()
        expires_at = ran_at + timedelta(seconds=expiry_seconds)
        return PreflightResult(
            preflight_id=str(uuid.uuid4()),
            ran_at=ran_at.isoformat(),
            expires_at=expires_at.isoformat(),
            config_hash=_config_hash(self.config),
            overall=overall,
            can_place_live_orders=can_place,
            checks=list(self._results),
            warnings=warnings,
            failures=failures,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add(self, name: str, status: str, message: str, category: str = "general") -> None:
        if name in self._disabled:
            return
        self._results.append(CheckResult(name=name, status=status, message=message, category=category))

    def _summarize(self) -> tuple[str, bool, list[str], list[str]]:
        failures = [f"{r.name}: {r.message}" for r in self._results if r.status == CheckStatus.FAIL]
        warnings = [f"{r.name}: {r.message}" for r in self._results if r.status == CheckStatus.WARN]
        if failures:
            return CheckStatus.FAIL, False, warnings, failures
        if warnings:
            return CheckStatus.WARN, True, warnings, failures
        return CheckStatus.PASS, True, warnings, failures

    def _ensure_api(self) -> KrakenAPI | DemoKrakenAPI | None:
        if self.api is not None:
            return self.api
        client: KrakenAPI | DemoKrakenAPI
        if is_demo_mode():
            client = DemoKrakenAPI(self.config.api_key, self.config.api_secret)
        else:
            client = KrakenAPI(self.config.api_key, self.config.api_secret)
        self.api = client
        return client

    def _api_safe(self, label: str, call: Any, fail_status: str = CheckStatus.FAIL) -> Any:
        """Call a read-only API method and return (value, error_message)."""
        try:
            return call(), None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    # ------------------------------------------------------------------
    # Check groups
    # ------------------------------------------------------------------

    def _check_environment(self) -> None:
        if _is_production():
            self._add("app_env_production", CheckStatus.PASS, "APP_ENV is production", "environment")
        else:
            self._add(
                "app_env_production",
                CheckStatus.FAIL,
                f"APP_ENV is {os.environ.get('APP_ENV', 'not set')!r}, production required for live orders",
                "environment",
            )

        if is_demo_mode():
            self._add("demo_mode_disabled", CheckStatus.FAIL, "Demo mode is enabled", "environment")
        else:
            self._add("demo_mode_disabled", CheckStatus.PASS, "Demo mode is disabled", "environment")

        if self.config.live_trading_enabled:
            self._add(
                "live_trading_enabled",
                CheckStatus.PASS,
                "Live trading is enabled (config.json flag; the LIVE_TRADING_ENABLED environment variable overrides it when set)",
                "environment",
            )
        else:
            self._add(
                "live_trading_enabled",
                CheckStatus.WARN,
                "Live trading is disabled — the bot runs in dry-run mode. Toggle it in the top bar or settings (the LIVE_TRADING_ENABLED environment variable overrides when set)",
                "environment",
            )

        if self.overrides.paused:
            self._add("bot_not_paused", CheckStatus.WARN, "Bot is currently paused", "environment")
        else:
            self._add("bot_not_paused", CheckStatus.PASS, "Bot is not paused", "environment")

        status = self.state.status
        if status in {"stopped", "error", "hold"}:
            self._add("trading_loop_state", CheckStatus.FAIL, f"Bot status is {status!r}", "environment")
        elif status in {"running", "waiting"}:
            self._add("trading_loop_state", CheckStatus.PASS, f"Bot status is {status!r}", "environment")
        else:
            self._add("trading_loop_state", CheckStatus.WARN, f"Bot status is {status!r} (unknown)", "environment")

    def _check_credentials(self) -> None:
        if self.config.api_key and self.config.api_secret:
            self._add(
                "kraken_credentials_present",
                CheckStatus.PASS,
                "Kraken credentials are configured (environment variables or dashboard secrets store)",
                "credentials",
            )
        else:
            self._add(
                "kraken_credentials_present",
                CheckStatus.FAIL,
                "Missing credentials: set KRAKEN_API_KEY / KRAKEN_API_SECRET or enter them in Settings → API Keys",
                "credentials",
            )
            # Cannot authenticate if credentials are missing.
            return

        api = self._ensure_api()
        if api is None:
            self._add("kraken_authentication", CheckStatus.FAIL, "No API client available", "credentials")
            return

        _, error = self._api_safe("balance", api.test_connection)
        if error is None:
            self._add("kraken_authentication", CheckStatus.PASS, "Kraken credentials authenticated", "credentials")
        else:
            self._add(
                "kraken_authentication",
                CheckStatus.FAIL,
                f"Kraken authentication failed: {error}",
                "credentials",
            )

        # Kraken does not expose API-key permissions via the REST API.
        self._add(
            "kraken_minimum_permissions",
            CheckStatus.WARN,
            "Kraken does not expose key permissions via API; confirm manually that the key has 'Query Funds' and 'Create & Modify Orders' only",
            "credentials",
        )
        self._add(
            "kraken_withdrawal_permission",
            CheckStatus.WARN,
            "Kraken does not expose key permissions via API; confirm manually that 'Withdraw Funds' is disabled",
            "credentials",
        )

    def _check_security(self) -> None:
        # The session secret comes from the SESSION_SECRET environment variable
        # or, as a fallback, the dashboard secrets store.
        from bot.secrets_store import SecretsStore

        session_secret = os.environ.get("SESSION_SECRET", "").strip()
        if not session_secret:
            session_secret = (SecretsStore().resolve("web").get("session_secret") or "").strip()
        if not session_secret:
            self._add(
                "session_secret_present",
                CheckStatus.FAIL,
                "Session secret is not set (SESSION_SECRET environment variable or Settings → Authentication)",
                "security",
            )
        elif len(session_secret) < 16:
            self._add(
                "session_secret_present",
                CheckStatus.FAIL,
                f"Session secret is only {len(session_secret)} characters (minimum 16)",
                "security",
            )
        else:
            self._add("session_secret_present", CheckStatus.PASS, "Session secret is set", "security")

        # Dashboard login protection: local password and/or OIDC, each with
        # environment-variable → secrets-store resolution.
        web_section = SecretsStore().resolve("web")
        password_hash_set = bool(
            os.environ.get("WEB_UI_PASSWORD_HASH", "").strip()
            or (web_section.get("password_hash") or "").strip()
        )
        oidc_enabled = SecretsStore().resolve("oidc").get("enabled") == "true"
        if password_hash_set or oidc_enabled:
            self._add(
                "password_or_oidc_configured",
                CheckStatus.PASS,
                "Dashboard authentication is configured" + (" (OIDC)" if oidc_enabled else ""),
                "security",
            )
        else:
            self._add(
                "password_or_oidc_configured",
                CheckStatus.FAIL,
                "Dashboard authentication is required: set a local admin password or enable Authentik (OIDC) in Settings → Authentication",
                "security",
            )

        if _is_production():
            secure_cookie = os.environ.get("WEB_UI_SECURE_COOKIE", "").lower()
            if secure_cookie in ("true", "1", "yes", "on"):
                self._add("secure_cookie", CheckStatus.PASS, "WEB_UI_SECURE_COOKIE is true", "security")
            else:
                self._add(
                    "secure_cookie",
                    CheckStatus.FAIL,
                    "WEB_UI_SECURE_COOKIE must be true in production",
                    "security",
                )
        else:
            self._add("secure_cookie", CheckStatus.PASS, "Secure cookie check only applies in production", "security")

        # CSRF depends on the session middleware and CSRF cookie being active.
        if session_secret:
            self._add("csrf_active", CheckStatus.PASS, "Session signing secret is present (CSRF depends on it)", "security")
        else:
            self._add("csrf_active", CheckStatus.WARN, "CSRF protection requires SESSION_SECRET", "security")

        disable_rate_limit = os.environ.get("DISABLE_RATE_LIMIT", "").lower()
        rate_limit_enabled = os.environ.get("RATE_LIMIT_ENABLED", "").lower()
        if disable_rate_limit in ("true", "1", "yes", "on"):
            self._add("rate_limiting_active", CheckStatus.FAIL, "DISABLE_RATE_LIMIT is true", "security")
        elif rate_limit_enabled in ("false", "0", "no", "off"):
            self._add("rate_limiting_active", CheckStatus.FAIL, "RATE_LIMIT_ENABLED is false", "security")
        else:
            self._add("rate_limiting_active", CheckStatus.PASS, "Rate limiting is enabled", "security")

    def _check_filesystem(self) -> None:
        for name, path in (
            ("data_dir_writable", self.data_dir),
            ("logs_dir_writable", self.data_dir.parent / "logs"),
            ("backups_dir_writable", self.backup_dir),
        ):
            if _dir_writable(path):
                self._add(name, CheckStatus.PASS, f"{path} is writable", "filesystem")
            else:
                self._add(name, CheckStatus.FAIL, f"{path} is not writable", "filesystem")

    def _check_pair_and_market(self, pair_override: str | None) -> None:
        pair = (pair_override or self.config.trading_pair).upper()
        if not pair:
            self._add("pair_exists", CheckStatus.FAIL, "No trading pair configured", "market")
            return

        api = self._ensure_api()
        if api is None:
            self._add("pair_exists", CheckStatus.FAIL, "No API client available", "market")
            return

        info, error = self._api_safe("asset_pairs", lambda: api.get_asset_pair_info(pair))
        if error is not None:
            self._add("pair_exists", CheckStatus.FAIL, f"Cannot resolve pair {pair}: {error}", "market")
            return

        self._pair_info = info if isinstance(info, dict) else {}
        self._add("pair_exists", CheckStatus.PASS, f"Pair {pair} is valid", "market")
        self._add(
            "pair_metadata_loaded",
            CheckStatus.PASS,
            f"Loaded pair metadata (lot_decimals={self._pair_info.get('lot_decimals', 'n/a')}, pair_decimals={self._pair_info.get('pair_decimals', 'n/a')})",
            "market",
        )

        price, error = self._api_safe("ticker", lambda: api.get_ticker(pair))
        if error is None:
            self._price = float(price) if price is not None else None
            self._add("ticker_available", CheckStatus.PASS, f"Current price for {pair} is available", "market")
        else:
            self._add("ticker_available", CheckStatus.FAIL, f"Cannot fetch price for {pair}: {error}", "market")

    def _check_amount(self, amount_override: float | None) -> None:
        amount = amount_override if amount_override is not None else self.config.crypto_amount
        if amount <= 0:
            self._add("amount_positive", CheckStatus.FAIL, f"Amount {amount} is not positive", "amount")
            return
        self._add("amount_positive", CheckStatus.PASS, f"Amount {amount} is positive", "amount")

        if self._pair_info is None:
            self._add("amount_precision", CheckStatus.WARN, "Cannot check precision without pair metadata", "amount")
            self._add("volume_above_minimum", CheckStatus.WARN, "Cannot check minimum without pair metadata", "amount")
            return

        lot_decimals = self._pair_info.get("lot_decimals")
        if lot_decimals is not None:
            rounded = round(amount, int(lot_decimals))
            if abs(rounded - amount) > 10 ** -(int(lot_decimals) + 2):
                self._add(
                    "amount_precision",
                    CheckStatus.WARN,
                    f"Amount {amount} exceeds lot_decimals={lot_decimals} precision (rounded to {rounded})",
                    "amount",
                )
            else:
                self._add(
                    "amount_precision",
                    CheckStatus.PASS,
                    f"Amount precision matches lot_decimals={lot_decimals}",
                    "amount",
                )
        else:
            self._add("amount_precision", CheckStatus.WARN, "lot_decimals not provided by Kraken", "amount")

        ordermin = self._pair_info.get("ordermin")
        if ordermin is not None:
            try:
                ordermin_val = float(ordermin)
            except (TypeError, ValueError):
                ordermin_val = None
            if ordermin_val is not None and amount < ordermin_val:
                self._add(
                    "volume_above_minimum",
                    CheckStatus.FAIL,
                    f"Amount {amount} is below Kraken ordermin {ordermin_val}",
                    "amount",
                )
            else:
                self._add(
                    "volume_above_minimum",
                    CheckStatus.PASS,
                    f"Amount {amount} is above Kraken ordermin {ordermin_val}",
                    "amount",
                )
        else:
            self._add("volume_above_minimum", CheckStatus.WARN, "ordermin not provided by Kraken", "amount")

        costmin = self._pair_info.get("costmin")
        if costmin is not None and self._price is not None:
            try:
                costmin_val = float(costmin)
            except (TypeError, ValueError):
                costmin_val = None
            estimated_cost = amount * self._price
            if costmin_val is not None and estimated_cost < costmin_val:
                self._add(
                    "cost_above_minimum",
                    CheckStatus.FAIL,
                    f"Estimated cost {estimated_cost:.2f} is below Kraken costmin {costmin_val}",
                    "amount",
                )
            else:
                self._add(
                    "cost_above_minimum",
                    CheckStatus.PASS,
                    f"Estimated cost {estimated_cost:.2f} is above Kraken costmin {costmin_val}",
                    "amount",
                )
        else:
            self._add(
                "cost_above_minimum",
                CheckStatus.WARN,
                "Cannot check costmin (missing metadata or price)",
                "amount",
            )

    def _check_balance(self) -> None:
        api = self._ensure_api()
        if api is None:
            self._add("balance_readable", CheckStatus.FAIL, "No API client available", "balance")
            return

        balance, error = self._api_safe("balance", api.get_balance)
        if error is None:
            self._balance = balance if isinstance(balance, dict) else {}
            self._add("balance_readable", CheckStatus.PASS, "Account balance fetched", "balance")
        else:
            self._add("balance_readable", CheckStatus.FAIL, f"Cannot fetch balance: {error}", "balance")
            return

        if self._price is None:
            self._add("balance_sufficient", CheckStatus.WARN, "Cannot check balance without price", "balance")
            return

        quote = _quote_currency(self.config.trading_pair)
        available = self._balance.get(f"Z{quote}", self._balance.get(quote, 0.0))
        amount = self.config.crypto_amount
        estimated_cost = amount * self._price
        total_with_fees = estimated_cost * (1 + KRAKEN_TAKER_FEE_RATE + DEFAULT_FEE_BUFFER_PERCENT / 100.0)

        if available >= total_with_fees:
            self._add(
                "balance_sufficient",
                CheckStatus.PASS,
                f"Available {available:.2f} {quote} covers estimated cost {total_with_fees:.2f} {quote}",
                "balance",
            )
            self._add(
                "fee_buffer_included",
                CheckStatus.PASS,
                f"Fee buffer included ({KRAKEN_TAKER_FEE_RATE * 100:.2f}% fee + {DEFAULT_FEE_BUFFER_PERCENT}% buffer)",
                "balance",
            )
        else:
            self._add(
                "balance_sufficient",
                CheckStatus.FAIL,
                f"Available {available:.2f} {quote} is less than estimated cost {total_with_fees:.2f} {quote}",
                "balance",
            )
            self._add(
                "fee_buffer_included",
                CheckStatus.WARN,
                "Fee buffer included but balance is insufficient",
                "balance",
            )

    def _attempt_records(self) -> list[dict[str, Any]]:
        """Return order attempts as plain dicts without importing order_execution."""
        if self.attempt_store is not None:
            records = self.attempt_store.list_all()
            return [a.to_dict() if hasattr(a, "to_dict") else dict(a) for a in records]
        data = safe_load_json(self.data_dir / "order_attempts.json")
        return data if isinstance(data, list) else []

    def _check_order_state(self) -> None:
        if self.state.status == "hold":
            self._add("no_hold_state", CheckStatus.FAIL, "Bot is in HOLD state", "order_state")
        else:
            self._add("no_hold_state", CheckStatus.PASS, "Bot is not in HOLD state", "order_state")

        attempts = self._attempt_records()
        unknown_attempts = [a for a in attempts if a.get("state") == "UNKNOWN"]
        if unknown_attempts:
            self._add(
                "no_unknown_orders",
                CheckStatus.FAIL,
                f"{len(unknown_attempts)} order attempt(s) have unknown outcome",
                "order_state",
            )
        else:
            self._add("no_unknown_orders", CheckStatus.PASS, "No unresolved unknown orders", "order_state")

        terminal = {"CONFIRMED", "CANCELLED", "HOLD"}
        non_terminal = [a for a in attempts if a.get("state") not in terminal]
        cycle_ids: dict[str, int] = {}
        for attempt in non_terminal:
            cycle_ids[attempt.get("cycle_id", "")] = cycle_ids.get(attempt.get("cycle_id", ""), 0) + 1
        duplicates = {cid: count for cid, count in cycle_ids.items() if count > 1 and cid}
        if duplicates:
            self._add(
                "no_duplicate_attempts",
                CheckStatus.FAIL,
                f"Duplicate non-terminal attempt(s) for cycle(s): {list(duplicates.keys())}",
                "order_state",
            )
        else:
            self._add("no_duplicate_attempts", CheckStatus.PASS, "No duplicate non-terminal order attempts", "order_state")

        next_cycle = self.state.next_cycle_at
        if next_cycle is None:
            self._add("next_cycle_sensible", CheckStatus.WARN, "Next cycle time is not set", "order_state")
        else:
            now = _now()
            hours_to_next = (next_cycle - now).total_seconds() / 3600
            if hours_to_next < -48:
                self._add(
                    "next_cycle_sensible",
                    CheckStatus.WARN,
                    f"Next cycle is {hours_to_next:.1f}h in the past",
                    "order_state",
                )
            elif hours_to_next > 45 * 24:
                self._add(
                    "next_cycle_sensible",
                    CheckStatus.WARN,
                    f"Next cycle is {hours_to_next / 24:.1f} days away",
                    "order_state",
                )
            else:
                self._add("next_cycle_sensible", CheckStatus.PASS, "Next cycle time is sensible", "order_state")

        heartbeat_file = self.data_dir / "heartbeat.json"
        if heartbeat_file.exists():
            data = safe_load_json(heartbeat_file)
            ts = _parse_iso_timestamp(data.get("timestamp") if isinstance(data, dict) else None)
            if ts is not None:
                age = int((_now() - ts).total_seconds())
                if age <= DEFAULT_HEARTBEAT_MAX_AGE_SECONDS:
                    self._add("heartbeat_recent", CheckStatus.PASS, f"Heartbeat is {age}s old", "order_state")
                else:
                    self._add(
                        "heartbeat_recent",
                        CheckStatus.WARN,
                        f"Heartbeat is {age}s old (threshold {DEFAULT_HEARTBEAT_MAX_AGE_SECONDS}s)",
                        "order_state",
                    )
            else:
                self._add("heartbeat_recent", CheckStatus.WARN, "heartbeat.json has no timestamp", "order_state")
        else:
            self._add("heartbeat_recent", CheckStatus.WARN, "heartbeat.json not found", "order_state")

    def _check_telegram(self, telegram_test: bool) -> None:
        if self.notifier.enabled:
            self._add("telegram_configured", CheckStatus.PASS, "Telegram notifier is configured", "telegram")
        else:
            self._add(
                "telegram_configured",
                CheckStatus.WARN,
                "Telegram notifier is not configured",
                "telegram",
            )

        if telegram_test:
            success, message = self.notifier.test()
            if success:
                self._add("telegram_test", CheckStatus.PASS, message, "telegram")
            else:
                self._add("telegram_test", CheckStatus.FAIL, f"Telegram test failed: {message}", "telegram")
        else:
            self._add("telegram_test", CheckStatus.PASS, "Skipped (--telegram-test not set)", "telegram")

    def _check_time(self) -> None:
        now = _now()
        if 2020 <= now.year <= 2100 and now.tzinfo is not None:
            self._add(
                "system_time_plausible",
                CheckStatus.PASS,
                f"System time is {now.isoformat()}",
                "system",
            )
        else:
            self._add(
                "system_time_plausible",
                CheckStatus.WARN,
                f"System time looks suspicious: {now.isoformat()}",
                "system",
            )
