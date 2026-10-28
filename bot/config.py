"""Configuration loader and validator for the DCA bot."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from bot.secrets_store import SecretsStore

logger = logging.getLogger("dca_bot.config")


# Default tier set for BTC pairs. Amounts sit comfortably above Kraken's
# ordermin (0.00005 XBT for XXBTZEUR) while keeping individual buys in a
# typical DCA range; scale proportionally for other base assets or budgets.
DEFAULT_DYNAMIC_TIERS: List[dict[str, Any]] = [
    {"threshold_percent": 10.0, "amount": 0.0, "enabled": True},
    {"threshold_percent": 5.0, "amount": 0.0005, "enabled": True},
    {"threshold_percent": -2.0, "amount": 0.00075, "enabled": True},
    {"threshold_percent": -5.0, "amount": 0.001, "enabled": True},
    {"threshold_percent": -10.0, "amount": 0.0015, "enabled": True},
    {"threshold_percent": -20.0, "amount": 0.002, "enabled": True},
]

# Content blocks of the deterministic briefing message (AI advisor, story
# A2/A9). Keep in sync with bot.advisor.briefing.DEFAULT_BRIEFING_SECTIONS;
# the narrative block and the news blocks are opt-in so the default briefing
# consumes no tokens at all.
DEFAULT_BRIEFING_SECTIONS: dict[str, bool] = {
    "market": True,
    "portfolio": True,
    "budget": True,
    "bot": True,
    "narrative": False,
    "news_general": False,
    "news_pair": False,
}

# Narrative focus topics. "news" only takes effect when news headlines were
# fetched successfully; the bot fetches them, the AI only narrates.
NARRATIVE_FOCUS_OPTIONS = ("market", "portfolio", "plan", "news")
DEFAULT_NARRATIVE_FOCUS = ("market", "portfolio", "plan")
CUSTOM_PROMPT_MAX_CHARS = 2000
NEWS_MAX_FEEDS = 5
DEFAULT_NEWS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
)


@dataclass
class DynamicTier:
    """One price-change tier for dynamic DCA."""

    threshold_percent: float
    amount: float
    enabled: bool
    # When True, a buy matched by this tier ignores max_monthly_amount (the
    # reserve is spent opportunistically; the buy is still bounded by the
    # live Kraken balance pre-check and does not count toward the monthly
    # budget).
    override_monthly_budget: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_percent": self.threshold_percent,
            "amount": self.amount,
            "enabled": self.enabled,
            "override_monthly_budget": self.override_monthly_budget,
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
        self.update_check_enabled: bool = True
        self.update_check_interval_hours: int = 24
        self.max_price: Optional[float] = None
        self.max_monthly_amount: Optional[float] = None
        self.dca_end_date: Optional[datetime] = None
        self.dynamic_dca_enabled: bool = False
        self.dynamic_dca_reference: str = "last_buy"
        self.dynamic_dca_cooldown_hours: float = 24.0
        self.dynamic_dca_tiers: List[DynamicTier] = []
        self.preflight_disabled_checks: List[str] = []
        # AI advisor (story A1). Everything is off by default; the advisor
        # is advisory-only and never on the buy path. The API key is managed
        # like all dashboard secrets: AI_ADVISOR_API_KEY env var first,
        # secrets store (data/secrets.json) as fallback — never in this file.
        self.advisor_enabled: bool = False
        self.advisor_features: dict[str, bool] = {"briefing": False, "explainer": False}
        self.advisor_provider: str = "openai"
        self.advisor_base_url: str = ""
        self.advisor_model: str = ""
        self.advisor_timeout_seconds: float = 30.0
        self.advisor_max_retries: int = 2
        self.advisor_max_parallel: int = 2
        self.advisor_daily_request_limit: int = 50
        self.advisor_daily_token_limit: int = 100_000
        self.advisor_daily_cost_limit: float = 1.0
        self.advisor_briefing_time: str = "07:00"
        self.advisor_temperature: float = 0.2
        self.advisor_max_tokens: int = 1024
        self.advisor_briefing_frequency: str = "daily"
        self.advisor_briefing_weekday: int = 0
        self.advisor_briefing_month_day: int = 1
        self.advisor_briefing_sections: dict[str, bool] = dict(DEFAULT_BRIEFING_SECTIONS)
        # Narrative customization and news sources (all optional; safe
        # defaults keep the previous behaviour).
        self.advisor_narrative_focus: tuple[str, ...] = DEFAULT_NARRATIVE_FOCUS
        self.advisor_custom_prompt: str = ""
        self.advisor_narrative_max_words: int = 120
        self.advisor_news_feeds: tuple[str, ...] = DEFAULT_NEWS_FEEDS
        self.loaded_at: Optional[datetime] = None
        self.configured: bool = False
        self._load()

    def _load(self):
        """Load and validate configuration"""
        created_template = False
        if not self.config_path.exists():
            self._create_template()
            created_template = True

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
        self.update_check_enabled = bool(config.get("update_check_enabled", True))
        self.update_check_interval_hours = int(
            config.get("update_check_interval_hours", 24)
        )
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
                override_monthly_budget=bool(tier.get("override_monthly_budget", False)),
            )
            for tier in tiers
        ]

        self.preflight_disabled_checks = [
            str(name)
            for name in config.get("preflight_disabled_checks", [])
            if isinstance(name, str) and name
        ]

        advisor = config.get("advisor", {})
        self.advisor_enabled = bool(advisor.get("enabled", False))
        features_raw = advisor.get("features", {})
        if isinstance(features_raw, dict):
            self.advisor_features = {
                name: bool(features_raw.get(name, False))
                for name in ("briefing", "explainer")
            }
        self.advisor_provider = str(advisor.get("provider", "openai")).lower()
        self.advisor_base_url = str(advisor.get("base_url", ""))
        self.advisor_model = str(advisor.get("model", ""))
        self.advisor_timeout_seconds = float(advisor.get("timeout_seconds", 30.0))
        self.advisor_max_retries = int(advisor.get("max_retries", 2))
        self.advisor_max_parallel = int(advisor.get("max_parallel", 2))
        self.advisor_daily_request_limit = int(advisor.get("daily_request_limit", 50))
        self.advisor_daily_token_limit = int(advisor.get("daily_token_limit", 100_000))
        self.advisor_daily_cost_limit = float(advisor.get("daily_cost_limit", 1.0))
        self.advisor_briefing_time = str(advisor.get("briefing_time", "07:00"))
        self.advisor_temperature = float(advisor.get("temperature", 0.2))
        self.advisor_max_tokens = int(advisor.get("max_tokens", 1024))
        self.advisor_briefing_frequency = str(
            advisor.get("briefing_frequency", "daily")
        ).lower()
        self.advisor_briefing_weekday = int(advisor.get("briefing_weekday", 0))
        self.advisor_briefing_month_day = int(advisor.get("briefing_month_day", 1))
        sections_raw = advisor.get("briefing_sections", {})
        self.advisor_briefing_sections = dict(DEFAULT_BRIEFING_SECTIONS)
        if isinstance(sections_raw, dict):
            for key, value in sections_raw.items():
                if key in DEFAULT_BRIEFING_SECTIONS:
                    self.advisor_briefing_sections[key] = bool(value)
        focus_raw = advisor.get("narrative_focus", list(DEFAULT_NARRATIVE_FOCUS))
        if isinstance(focus_raw, list):
            focus = tuple(str(f) for f in focus_raw if str(f) in NARRATIVE_FOCUS_OPTIONS)
            self.advisor_narrative_focus = focus or DEFAULT_NARRATIVE_FOCUS
        self.advisor_custom_prompt = str(advisor.get("custom_prompt", ""))[
            :CUSTOM_PROMPT_MAX_CHARS
        ]
        self.advisor_narrative_max_words = int(advisor.get("narrative_max_words", 120))
        feeds_raw = advisor.get("news_feeds", list(DEFAULT_NEWS_FEEDS))
        if isinstance(feeds_raw, list):
            feeds = [
                str(u).strip()
                for u in feeds_raw
                if str(u).strip().startswith(("http://", "https://"))
            ][:NEWS_MAX_FEEDS]
            if feeds:
                self.advisor_news_feeds = tuple(feeds)

        # The bot is "configured" once exchange credentials are known. All
        # other fields are validated strictly regardless (the first-boot
        # template ships valid defaults), but a missing key/secret must not
        # be fatal: the dashboard stays up so credentials can be entered in
        # the UI, and the trading loop idles until then.
        self.configured = bool(self.api_key and self.api_secret)
        if not self.configured and not created_template:
            logger.warning(
                "Kraken API credentials not configured — bot idles until "
                "credentials are added in the dashboard (Settings -> API Keys)."
            )
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
        if self.configured and (not self.api_key or not self.api_secret):
            raise Exception("api_key and api_secret are required")
        if self.crypto_amount <= 0:
            raise Exception("crypto_amount must be greater than 0")
        if not (0 < self.dip_threshold_percent <= 100):
            raise Exception("dip_threshold_percent must be between 0 and 100")
        if self.poll_interval_seconds < 60:
            raise Exception("poll_interval_seconds must be at least 60")
        if self.update_check_interval_hours < 1:
            raise Exception("update_check_interval_hours must be at least 1")
        if not (0 <= self.buy_hour <= 23):
            raise Exception("buy_hour must be between 0 and 23")
        if self.dip_buy_cooldown_hours < 0:
            raise Exception("dip_buy_cooldown_hours must be 0 or greater")
        if self.dynamic_dca_reference not in ("last_buy", "avg_buy"):
            raise Exception("dynamic_dca.reference must be 'last_buy' or 'avg_buy'")
        if not (40 <= self.advisor_narrative_max_words <= 400):
            raise Exception("advisor.narrative_max_words must be between 40 and 400")
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
        if self.advisor_provider not in ("openai", "ollama"):
            raise Exception("advisor.provider must be 'openai' or 'ollama'")
        if self.advisor_enabled:
            if not self.advisor_model:
                raise Exception("advisor.model is required when the advisor is enabled")
            if self.advisor_timeout_seconds <= 0:
                raise Exception("advisor.timeout_seconds must be greater than 0")
            if self.advisor_max_retries < 0:
                raise Exception("advisor.max_retries must be 0 or greater")
            if self.advisor_max_parallel < 1:
                raise Exception("advisor.max_parallel must be at least 1")
            if self.advisor_daily_request_limit < 1:
                raise Exception("advisor.daily_request_limit must be at least 1")
            if self.advisor_daily_token_limit < 1:
                raise Exception("advisor.daily_token_limit must be at least 1")
            if self.advisor_daily_cost_limit <= 0:
                raise Exception("advisor.daily_cost_limit must be greater than 0")
            try:
                hour_str, minute_str = self.advisor_briefing_time.strip().split(":", 1)
                hour, minute = int(hour_str), int(minute_str)
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except (ValueError, AttributeError):
                raise Exception('advisor.briefing_time must look like "07:00"')
            if not (0.0 <= self.advisor_temperature <= 2.0):
                raise Exception("advisor.temperature must be between 0 and 2")
            if self.advisor_max_tokens < 1:
                raise Exception("advisor.max_tokens must be at least 1")
            if self.advisor_briefing_frequency not in ("daily", "weekly", "monthly"):
                raise Exception(
                    "advisor.briefing_frequency must be 'daily', 'weekly' or 'monthly'"
                )
            if not (0 <= self.advisor_briefing_weekday <= 6):
                raise Exception("advisor.briefing_weekday must be between 0 (Mon) and 6 (Sun)")
            if not (1 <= self.advisor_briefing_month_day <= 28):
                raise Exception("advisor.briefing_month_day must be between 1 and 28")

    def _create_template(self):
        """Create template configuration file"""
        template = {
            "trading_pair": "XXBTZUSD",
            "deposit_day": 1,
            "crypto_amount": 0.0005,
            "dip_threshold_percent": 5.0,
            "poll_interval_seconds": 600,
            "buy_hour": 8,
            "dip_buy_cooldown_hours": 24.0,
            "update_check_enabled": True,
            "update_check_interval_hours": 24,
            "live_trading_enabled": False,
            "dynamic_dca": {
                "enabled": False,
                "reference": "last_buy",
                "cooldown_hours": 24.0,
                "tiers": DEFAULT_DYNAMIC_TIERS,
            },
            "advisor": {
                "enabled": False,
                "features": {"briefing": False, "explainer": False},
                "provider": "openai",
                "base_url": "",
                "model": "",
                "timeout_seconds": 30.0,
                "max_retries": 2,
                "max_parallel": 2,
                "daily_request_limit": 50,
                "daily_token_limit": 100000,
                "daily_cost_limit": 1.0,
                "briefing_time": "07:00",
                "temperature": 0.2,
                "max_tokens": 1024,
                "briefing_frequency": "daily",
                "briefing_weekday": 0,
                "briefing_month_day": 1,
                "briefing_sections": dict(DEFAULT_BRIEFING_SECTIONS),
                "narrative_focus": list(DEFAULT_NARRATIVE_FOCUS),
                "custom_prompt": "",
                "narrative_max_words": 120,
                "news_feeds": list(DEFAULT_NEWS_FEEDS),
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
            "update_check_enabled": self.update_check_enabled,
            "update_check_interval_hours": self.update_check_interval_hours,
            "max_price": self.max_price,
            "max_monthly_amount": self.max_monthly_amount,
            "dca_end_date": self.dca_end_date.isoformat() if self.dca_end_date else None,
            "live_trading_enabled": self.live_trading_enabled,
            "configured": self.configured,
            "api_key_set": bool(self.api_key),
            "api_secret_set": bool(self.api_secret),
            "dynamic_dca": {
                "enabled": self.dynamic_dca_enabled,
                "reference": self.dynamic_dca_reference,
                "cooldown_hours": self.dynamic_dca_cooldown_hours,
                "tiers": [tier.to_dict() for tier in self.dynamic_dca_tiers],
            },
            "preflight_disabled_checks": list(self.preflight_disabled_checks),
            "advisor": {
                "enabled": self.advisor_enabled,
                "features": dict(self.advisor_features),
                "provider": self.advisor_provider,
                "base_url": self.advisor_base_url,
                "model": self.advisor_model,
                "timeout_seconds": self.advisor_timeout_seconds,
                "max_retries": self.advisor_max_retries,
                "max_parallel": self.advisor_max_parallel,
                "daily_request_limit": self.advisor_daily_request_limit,
                "daily_token_limit": self.advisor_daily_token_limit,
                "daily_cost_limit": self.advisor_daily_cost_limit,
                "briefing_time": self.advisor_briefing_time,
                "temperature": self.advisor_temperature,
                "max_tokens": self.advisor_max_tokens,
                "briefing_frequency": self.advisor_briefing_frequency,
                "briefing_weekday": self.advisor_briefing_weekday,
                "briefing_month_day": self.advisor_briefing_month_day,
                "briefing_sections": dict(self.advisor_briefing_sections),
                "narrative_focus": list(self.advisor_narrative_focus),
                "custom_prompt": self.advisor_custom_prompt,
                "narrative_max_words": self.advisor_narrative_max_words,
                "news_feeds": list(self.advisor_news_feeds),
            },
        }

    def reload(self) -> None:
        """Reload configuration from disk."""
        self._load()
