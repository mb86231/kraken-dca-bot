#!/usr/bin/env python3
"""
Kraken DCA - Automated Dollar Cost Averaging for Cryptocurrency

Entrypoint. The trading logic lives in the `bot` package; the optional web
dashboard is started in a daemon thread when WEB_UI_ENABLED=true.
"""

from __future__ import annotations

import os
import signal
import sys


def main() -> int:
    from bot.colors import Colors
    from bot.core import KrakenDCA
    from bot.demo import is_demo_mode
    from bot.logger import redirect_stdout_to_logger, setup_logging

    logger = setup_logging()
    redirect_stdout_to_logger(logger)
    logger.info("Crypto Agent starting up")

    # Create the bot first so its shared state, config, store and overrides can
    # be handed to the web dashboard thread.
    app = KrakenDCA()
    logger.info(
        f"Startup safety check: APP_ENV={os.environ.get('APP_ENV', 'production')}, "
        f"DEMO_MODE={is_demo_mode()}, can_place_live_orders={os.environ.get('APP_ENV', 'production').lower() == 'production' and not is_demo_mode()}"
    )

    # Allow the container runtime to shut the bot down gracefully with SIGTERM.
    def _handle_sigterm(signum, frame):  # noqa: ARG001
        app.request_stop()

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError:
        # Signals can only be registered on the main thread. In the unlikely case
        # main() is called from a background thread, graceful shutdown falls back
        # to the default container behaviour.
        pass

    # Optional web dashboard: start in a daemon thread so trading is never blocked.
    web_enabled = os.environ.get("WEB_UI_ENABLED", "").lower() in ("true", "1", "yes", "on")
    if web_enabled:
        try:
            from web.server import start_web_server

            start_web_server(
                daemon=True,
                config=app.config,
                store=app.store,
                state=app.state,
                overrides=app.overrides,
                attempt_store=app.order_executor.attempt_store,
            )
            print(f"{Colors.GREEN}Web dashboard enabled.{Colors.RESET}")
        except Exception as e:
            print(f"{Colors.YELLOW}Warning: Could not start web dashboard: {e}{Colors.RESET}")

    try:
        app.run()
    except Exception as e:
        print(f"{Colors.RED}Fatal Error: {str(e)}{Colors.RESET}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
