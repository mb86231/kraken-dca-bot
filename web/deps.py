"""FastAPI dependencies for the web dashboard."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from bot.config import Config
from bot.order_execution import OrderAttemptStore
from bot.state import BotState, RuntimeOverrides
from bot.store import TransactionStore

if TYPE_CHECKING:
    from bot.advisor import Advisor
    from bot.advisor.briefing import BriefingService


def _strict_env() -> bool:
    """Return True when missing dependencies must be treated as a config error."""
    return os.environ.get("APP_ENV", "production").lower() in ("production", "staging")


def _missing_dependency(name: str):
    """Raise a controlled failure when a required dependency is not initialized."""
    raise HTTPException(
        status_code=503,
        detail=f"Server configuration error: {name} is not initialized",
    )


def get_config(request: Request) -> Config:
    if hasattr(request.app.state, "config") and request.app.state.config is not None:
        return request.app.state.config
    if _strict_env():
        _missing_dependency("config")
    return Config()


def get_store(request: Request) -> TransactionStore:
    if hasattr(request.app.state, "store") and request.app.state.store is not None:
        return request.app.state.store
    if _strict_env():
        _missing_dependency("transaction store")
    return TransactionStore()


def get_state(request: Request) -> BotState:
    if hasattr(request.app.state, "bot_state") and request.app.state.bot_state is not None:
        return request.app.state.bot_state
    if _strict_env():
        _missing_dependency("bot state")
    return BotState()


def get_overrides(request: Request) -> RuntimeOverrides:
    if hasattr(request.app.state, "overrides") and request.app.state.overrides is not None:
        return request.app.state.overrides
    if _strict_env():
        _missing_dependency("runtime overrides")
    return RuntimeOverrides()


def get_attempt_store(request: Request) -> OrderAttemptStore:
    if hasattr(request.app.state, "attempt_store") and request.app.state.attempt_store is not None:
        return request.app.state.attempt_store
    if _strict_env():
        _missing_dependency("order attempt store")
    return OrderAttemptStore()


def get_advisor(request: Request) -> Advisor | None:
    """The live advisor instance, or None when the bot did not provide one.

    Prefers the bot instance so a hot-reconfigured advisor is returned;
    falls back to the value injected at server startup (used by tests and
    when the dashboard runs without a bot).

    Never strict-fails: the status endpoint must keep working so the
    dashboard can show "advisor not configured" instead of erroring."""
    bot = getattr(request.app.state, "bot", None)
    if bot is not None:
        return getattr(bot, "advisor", None)
    advisor = getattr(request.app.state, "advisor", None)
    return advisor


def get_briefing(request: Request) -> BriefingService | None:
    """The live briefing service, or None when the bot did not provide one."""
    bot = getattr(request.app.state, "bot", None)
    if bot is not None:
        return getattr(bot, "briefing", None)
    briefing = getattr(request.app.state, "briefing", None)
    return briefing
