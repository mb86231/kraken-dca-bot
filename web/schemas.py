"""Pydantic request/response schemas for the web dashboard API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class LoginPayload(BaseModel):
    username: str
    password: str


class DynamicTierUpdate(BaseModel):
    threshold_percent: float
    amount: float = Field(..., ge=0)
    enabled: bool


class DynamicDCAUpdate(BaseModel):
    enabled: Optional[bool] = None
    reference: Optional[str] = None
    cooldown_hours: Optional[float] = Field(None, ge=0)
    tiers: Optional[List[DynamicTierUpdate]] = None

    @field_validator("reference")
    @classmethod
    def lowercase_reference(cls, v):
        return v.lower() if v else v


class SettingsUpdate(BaseModel):
    mode: Optional[str] = None
    trading_pair: Optional[str] = None
    deposit_day: Optional[int] = Field(None, ge=1, le=28)
    crypto_amount: Optional[float] = Field(None, gt=0)
    dip_threshold_percent: Optional[float] = Field(None, gt=0, le=100)
    dip_buy_cooldown_hours: Optional[float] = Field(None, ge=0)
    poll_interval_seconds: Optional[int] = Field(None, ge=60)
    buy_hour: Optional[int] = Field(None, ge=0, le=23)
    max_price: Optional[float] = None
    max_monthly_amount: Optional[float] = None
    dca_end_date: Optional[str] = None
    live_trading_enabled: Optional[bool] = None
    dynamic_dca: Optional[DynamicDCAUpdate] = None
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


class DynamicDCAResponse(BaseModel):
    enabled: bool
    reference: str
    cooldown_hours: float
    tiers: List[DynamicTierResponse]
