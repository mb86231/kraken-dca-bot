"""Uvicorn-based web server launcher."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.config import Config
    from bot.order_execution import OrderAttemptStore
    from bot.state import BotState, RuntimeOverrides
    from bot.store import TransactionStore


def start_web_server(
    daemon: bool = True,
    config: "Config | None" = None,
    store: "TransactionStore | None" = None,
    state: "BotState | None" = None,
    overrides: "RuntimeOverrides | None" = None,
    attempt_store: "OrderAttemptStore | None" = None,
) -> threading.Thread:
    """Start the FastAPI dashboard server in a background thread.

    When the trading bot is available, pass its shared instances so the dashboard
    reflects live state. Otherwise the dashboard creates its own instances on
    startup.
    """
    import os

    import uvicorn

    from web.app import app

    # Share bot instances with the web app if provided. The lifespan handler
    # in web/app.py will only create defaults for attributes that are None.
    app.state.config = config
    app.state.store = store
    app.state.bot_state = state
    app.state.overrides = overrides
    app.state.attempt_store = attempt_store

    # Default to 0.0.0.0 inside a container so port mappings work; use 127.0.0.1 only when explicitly set.
    host = os.environ.get("WEB_UI_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_UI_PORT", "8000"))

    def run() -> None:
        uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)

    thread = threading.Thread(target=run, name="dca-web-server", daemon=daemon)
    thread.start()
    return thread
