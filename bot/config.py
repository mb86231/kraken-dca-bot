"""Configuration loader and validator for the DCA bot."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from bot.secrets_store import SecretsStore


DEFAULT_DYNAMIC_TIERS: List[dict[str, Any]] = [
    {"threshold_percent": 10.0, "amount": 0.0, "enabled": True},
    {"threshold_percent": 5.0, "amount": 0.00005, "enabled": True},
    {"threshold_percent": -2.0, "amount": 0.0001, "enabled": True},
    {"threshold_percent": -5.0, "amount": 0.00015, "enabled": True},
    {"threshold_percent": -10.0, "amount": 0.0002, "enabled": True},
    {"threshold_percent": -20.0, "amount": 0.0003, "enabled": True},
]


@dataclass
class DynamicTier:
    """One price-change tier for dynamic DCA."""

    threshold_percent: float
    amount: float
    enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_percent": self.threshold_percent,
            "amount": self.amount,
            "enabled": self.enabled,
        }


class Config:
    """Configuration loader and validator"""

    def __init__(self, config_path: str | Path | None = None):
        if config_path is None:
            config_path = os.environ.get("CONFIG_PATH", "config.json")
        self.config_path = Path(config_path)
        self.mode: str = "recurring"
        self.trading_pair: str = ""
        self.deposit_day: int = 1
        self.api_key: str = ""
        self.api_secret: str = ""
        self.live_trading_enabled: bool = False
        self.crypto_amount: float = 0.0
        self.dip_threshold_percent: float = 5.0
        self.poll_interval_seconds: int = 600
        self.buy_hour: int = 8
        self.dip_buy_cooldown_hours: float = 24.0
        self.max_price: Optional[float] = None
        self.max_monthly_amount: Optional[float] = None
        self.dca_end_date: Optional[datetime] = None
        self.dynamic_dca_enabled: bool = False
        self.dynamic_dca_reference: str = "last_buy"
        self.dynamic_dca_cooldown_hours: float = 24.0
        self.dynamic_dca_tiers: List[DynamicTier] = []
        self.preflight_disabled_checks: List[str] = []
        self.loaded_at: Optional[datetime] = None
        self._load()

    def _load(self):
        """Load and validate configuration"""
        if not self.config_path.exists():
            self._create_template()
            raise Exception(f"Config file created at {self.config_path}. Please fill in your details.")

        with open(self.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # Load and validate
        self.mode = config.get("mode", "recurring").lower()
        self.trading_pair = config.get("trading_pair", "").upper()
        self.deposit_day = int(config.get("deposit_day", 1))
        # API credentials MUST NOT come from config.json. Sources, in priority
        # order: KRAKEN_API_KEY / KRAKEN_API_SECRET environment variables, then
        # the dashboard-managed secrets store (data/secrets.json, mode 0600).
        self.api_key = os.environ.get("KRAKEN_API_KEY", "")
        self.api_secret = os.environ.get("KRAKEN_API_SECRET", "")
        if not self.api_key or not self.api_secret:
            stored_key, stored_secret = SecretsStore().get_exchange_credentials()
            self.api_key = self.api_key or (stored_key or "")
            self.api_secret = self.api_secret or (stored_secret or "")
        # Live trading: the LIVE_TRADING_ENABLED environment variable overrides
        # the config.json "live_trading_enabled" flag when set. Absent the env
        # var, the config flag decides; it defaults to False (dry-run).
        live_trading_raw = os.environ.get("LIVE_TRADING_ENABLED")
        if live_trading_raw is None:
            self.live_trading_enabled = bool(config.get("live_trading_enabled", False))
        else:
            self.live_trading_enabled = str(live_trading_raw).lower() in ("true", "1", "yes", "on")
        self.crypto_amount = float(config.get("crypto_amount", 0.0))
        self.dip_threshold_percent = float(config.get("dip_threshold_percent", 5.0))
        self.poll_interval_seconds = int(config.get("poll_interval_seconds", 600))
        self.buy_hour = int(config.get("buy_hour", 8))
        self.dip_buy_cooldown_hours = float(config.get("dip_buy_cooldown_hours", 24.0))
        max_price_raw = config.get("max_price")
        self.max_price = float(max_price_raw) if max_price_raw is not None else None
        max_monthly_raw = config.get("max_monthly_amount")
        self.max_monthly_amount = float(max_monthly_raw) if max_monthly_raw is not None else None
        dca_end_raw = config.get("dca_end_date")
        self.dca_end_date = datetime.fromisoformat(dca_end_raw).astimezone() if dca_end_raw else None

        dynamic_dca = config.get("dynamic_dca", {})
        self.dynamic_dca_enabled = bool(dynamic_dca.get("enabled", False))
        self.dynamic_dca_reference = str(dynamic_dca.get("reference", "last_buy")).lower()
        self.dynamic_dca_cooldown_hours = float(
            dynamic_dca.get("cooldown_hours", 24.0)
        )
        tiers = dynamic_dca.get("tiers", DEFAULT_DYNAMIC_TIERS)
        self.dynamic_dca_tiers = [
            DynamicTier(
                threshold_percent=float(tier.get("threshold_percent", 0.0)),
                amount=float(tier.get("amount", 0.0)),
                enabled=bool(tier.get("enabled", True)),
            )
            for tier in tiers
        ]

        self.preflight_disabled_checks = [
            str(name)
            for name in config.get("preflight_disabled_checks", [])
            if isinstance(name, str) and name
        ]

        self._validate()
        self.loaded_at = datetime.now().astimezone()

    def _validate(self) -> None:
        """Validate loaded configuration values."""
        if self.mode not in ("recurring", "lump_sum"):
            raise Exception("mode must be 'recurring' or 'lump_sum'")
        if not self.trading_pair:
            raise Exception("trading_pair is required in config")
        if self.mode == "recurring" and not (1 <= self.deposit_day <= 28):
            raise Exception("deposit_day must be between 1 and 28 (limited to 28 to ensure validity in February)")
        if self.mode == "lump_sum":
            if self.dca_end_date is None:
                raise Exception("dca_end_date is required when mode is 'lump_sum' (e.g. \"2027-06-01\")")
            if self.dca_end_date <= datetime.now().astimezone():
                raise Exception("dca_end_date must be in the future")
        if not self.api_key or not self.api_secret:
            raise Exception("api_key and api_secret are required")
        if self.crypto_amount <= 0:
            raise Exception("crypto_amount must be greater than 0")
        if not (0 < self.dip_threshold_percent <= 100):
            raise Exception("dip_threshold_percent must be between 0 and 100")
        if self.poll_interval_seconds < 60:
            raise Exception("poll_interval_seconds must be at least 60")
        if not (0 <= self.buy_hour <= 23):
            raise Exception("buy_hour must be between 0 and 23")
        if self.dip_buy_cooldown_hours < 0:
            raise Exception("dip_buy_cooldown_hours must be 0 or greater")
        if self.dynamic_dca_reference not in ("last_buy", "avg_buy"):
            raise Exception("dynamic_dca.reference must be 'last_buy' or 'avg_buy'")
        if self.dynamic_dca_cooldown_hours < 0:
            raise Exception("dynamic_dca.cooldown_hours must be 0 or greater")
        if self.dynamic_dca_enabled:
            if not self.dynamic_dca_tiers:
                raise Exception("dynamic_dca.tiers must contain at least one tier when enabled")
            if any(tier.amount < 0 for tier in self.dynamic_dca_tiers):
                raise Exception("dynamic_dca tier amounts must be 0 or greater")
            enabled_non_skip = [
                tier for tier in self.dynamic_dca_tiers if tier.enabled and tier.amount > 0
            ]
            if not enabled_non_skip:
                raise Exception(
                    "dynamic_dca must have at least one enabled tier with amount > 0"
                )

    def _create_template(self):
        """Create template configuration file"""
        template = {
            "trading_pair": "XXBTZUSD",
            "deposit_day": 1,
            "crypto_amount": 0.0001,
            "dip_threshold_percent": 5.0,
            "poll_interval_seconds": 600,
            "buy_hour": 8,
            "dip_buy_cooldown_hours": 24.0,
            "live_trading_enabled": False,
            "dynamic_dca": {
                "enabled": False,
                "reference": "last_buy",
                "cooldown_hours": 24.0,
                "tiers": DEFAULT_DYNAMIC_TIERS,
            },
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2)

    def to_dict(self, mask_secrets: bool = True) -> dict[str, Any]:
        """Return configuration as a dictionary for the dashboard."""
        return {
            "mode": self.mode,
            "trading_pair": self.trading_pair,
            "deposit_day": self.deposit_day,
            "buy_hour": self.buy_hour,
            "crypto_amount": self.crypto_amount,
            "dip_threshold_percent": self.dip_threshold_percent,
            "dip_buy_cooldown_hours": self.dip_buy_cooldown_hours,
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_price": self.max_price,
            "max_monthly_amount": self.max_monthly_amount,
            "dca_end_date": self.dca_end_date.isoformat() if self.dca_end_date else None,
            "live_trading_enabled": self.live_trading_enabled,
            "api_key_set": bool(self.api_key),
            "api_secret_set": bool(self.api_secret),
            "dynamic_dca": {
                "enabled": self.dynamic_dca_enabled,
                "reference": self.dynamic_dca_reference,
                "cooldown_hours": self.dynamic_dca_cooldown_hours,
                "tiers": [tier.to_dict() for tier in self.dynamic_dca_tiers],
            },
            "preflight_disabled_checks": list(self.preflight_disabled_checks),
        }

    def reload(self) -> None:
        """Reload configuration from disk."""
        self._load()
