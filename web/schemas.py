"""Pydantic request/response schemas for the web dashboard API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from bot.config import (
    CUSTOM_PROMPT_MAX_CHARS,
    NEWS_MAX_FEEDS,
    NARRATIVE_FOCUS_OPTIONS,
)


class LoginPayload(BaseModel):
    username: str
    password: str


class DynamicTierUpdate(BaseModel):
    threshold_percent: float
    amount: float = Field(..., ge=0)
    enabled: bool
    override_monthly_budget: bool = False


class DynamicDCAUpdate(BaseModel):
    enabled: Optional[bool] = None
    reference: Optional[str] = None
    cooldown_hours: Optional[float] = Field(None, ge=0)
    tiers: Optional[List[DynamicTierUpdate]] = None

    @field_validator("reference")
    @classmethod
    def lowercase_reference(cls, v):
        return v.lower() if v else v


class AdvisorFeaturesUpdate(BaseModel):
    briefing: Optional[bool] = None
    explainer: Optional[bool] = None


class AdvisorUpdate(BaseModel):
    """Non-secret AI advisor config (story A8). Saved into config.json under
    the ``advisor`` block; the provider API key is managed separately via the
    secrets store and never passes through this schema."""

    enabled: Optional[bool] = None
    features: Optional[AdvisorFeaturesUpdate] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    timeout_seconds: Optional[float] = Field(None, gt=0)
    max_retries: Optional[int] = Field(None, ge=0)
    max_parallel: Optional[int] = Field(None, ge=1)
    daily_request_limit: Optional[int] = Field(None, ge=1)
    daily_token_limit: Optional[int] = Field(None, ge=1)
    daily_cost_limit: Optional[float] = Field(None, gt=0)
    briefing_time: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0, le=2)
    max_tokens: Optional[int] = Field(None, ge=1, le=32768)
    briefing_frequency: Optional[str] = None
    briefing_weekday: Optional[int] = Field(None, ge=0, le=6)
    briefing_month_day: Optional[int] = Field(None, ge=1, le=28)
    briefing_sections: Optional[dict[str, bool]] = None
    narrative_focus: Optional[List[str]] = None
    custom_prompt: Optional[str] = Field(None, max_length=CUSTOM_PROMPT_MAX_CHARS)
    narrative_max_words: Optional[int] = Field(None, ge=40, le=400)
    news_feeds: Optional[List[str]] = None

    @field_validator("narrative_focus")
    @classmethod
    def known_focus(cls, v):
        if v is None:
            return v
        unknown = [f for f in v if f not in NARRATIVE_FOCUS_OPTIONS]
        if unknown:
            raise ValueError(
                f"advisor.narrative_focus: unknown topics {unknown}; "
                f"allowed: {list(NARRATIVE_FOCUS_OPTIONS)}"
            )
        return v

    @field_validator("news_feeds")
    @classmethod
    def sane_feeds(cls, v):
        if v is None:
            return v
        if len(v) > NEWS_MAX_FEEDS:
            raise ValueError(f"advisor.news_feeds: at most {NEWS_MAX_FEEDS} feeds")
        for url in v:
            if not str(url).startswith(("http://", "https://")):
                raise ValueError(f"advisor.news_feeds: '{url}' is not an http(s) URL")
        return [str(u).strip() for u in v if str(u).strip()]

    @field_validator("briefing_frequency")
    @classmethod
    def known_frequency(cls, v):
        if v is None:
            return v
        v = v.lower()
        if v not in ("daily", "weekly", "monthly"):
            raise ValueError(
                "advisor.briefing_frequency must be 'daily', 'weekly' or 'monthly'"
            )
        return v

    @field_validator("provider")
    @classmethod
    def known_provider(cls, v):
        if v is None:
            return v
        v = v.lower()
        if v not in ("openai", "ollama"):
            raise ValueError("advisor.provider must be 'openai' or 'ollama'")
        return v


class SettingsUpdate(BaseModel):
    mode: Optional[str] = None
    trading_pair: Optional[str] = None
    deposit_day: Optional[int] = Field(None, ge=1, le=28)
    crypto_amount: Optional[float] = Field(None, gt=0)
    dip_threshold_percent: Optional[float] = Field(None, gt=0, le=100)
    dip_buy_cooldown_hours: Optional[float] = Field(None, ge=0)
    poll_interval_seconds: Optional[int] = Field(None, ge=60)
    update_check_enabled: Optional[bool] = None
    update_check_interval_hours: Optional[int] = Field(None, ge=1)
    buy_hour: Optional[int] = Field(None, ge=0, le=23)
    max_price: Optional[float] = None
    max_monthly_amount: Optional[float] = None
    dca_end_date: Optional[str] = None
    live_trading_enabled: Optional[bool] = None
    dynamic_dca: Optional[DynamicDCAUpdate] = None
    advisor: Optional[AdvisorUpdate] = None
    preflight_disabled_checks: Optional[List[str]] = None

    @field_validator("trading_pair")
    @classmethod
    def uppercase_pair(cls, v):
        return v.upper() if v else v

    @field_validator("mode")
    @classmethod
    def lowercase_mode(cls, v):
        return v.lower() if v else v

    @field_validator("preflight_disabled_checks")
    @classmethod
    def known_preflight_checks(cls, v):
        if v is None:
            return v
        from bot.preflight import CHECK_CATALOG

        unknown = [name for name in v if name not in CHECK_CATALOG]
        if unknown:
            raise ValueError(f"unknown preflight check(s): {', '.join(sorted(unknown))}")
        return v


class TelegramUpdate(BaseModel):
    enabled: Optional[bool] = None
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None


class ExchangeCredentialsUpdate(BaseModel):
    api_key: str = Field(..., min_length=8)
    api_secret: str = Field(..., min_length=8)


class AdvisorKeyUpdate(BaseModel):
    """Provider API key for the AI advisor (secrets store only, never config)."""

    api_key: str = Field(..., min_length=8)


class WebAuthUpdate(BaseModel):
    """Local admin credentials for the dashboard login."""

    username: Optional[str] = Field(None, min_length=1, max_length=64)
    password: Optional[str] = Field(None, min_length=8, max_length=128)


class OIDCSettingsUpdate(BaseModel):
    """Authentik (OIDC) settings persisted to the secrets store.

    Empty strings are treated as "leave unchanged"; use the clear endpoint
    to remove stored values entirely.
    """

    enabled: Optional[bool] = None
    issuer_url: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None
    scopes: Optional[str] = None


class DynamicTierResponse(BaseModel):
    threshold_percent: float
    amount: float
    enabled: bool
    override_monthly_budget: bool = False


class DynamicDCAResponse(BaseModel):
    enabled: bool
    reference: str
    cooldown_hours: float
    tiers: List[DynamicTierResponse]
