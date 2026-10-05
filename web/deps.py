"""FastAPI dependencies for the web dashboard."""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from bot.config import Config
from bot.order_execution import OrderAttemptStore
from bot.state import BotState, RuntimeOverrides
from bot.store import TransactionStore


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
