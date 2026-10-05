#!/usr/bin/env python3
"""Run the web dashboard in demo mode without starting the trading bot."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    os.environ.setdefault("DEMO_MODE", "true")
    os.environ.setdefault("WEB_UI_ENABLED", "true")
    os.environ.setdefault("WEB_UI_HOST", "127.0.0.1")
    os.environ.setdefault("WEB_UI_PORT", "8000")
    os.environ.setdefault("LIVE_TRADING_ENABLED", "false")

    if not os.environ.get("WEB_UI_PASSWORD_HASH"):
        print(
            "ERROR: WEB_UI_PASSWORD_HASH is not set.\n"
            "Generate one with: python scripts/generate_password_hash.py",
            file=sys.stderr,
        )
        return 1

    if not os.environ.get("SESSION_SECRET"):
        print("ERROR: SESSION_SECRET is not set.", file=sys.stderr)
        return 1

    from web.app import app
    import uvicorn

    host = os.environ.get("WEB_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_UI_PORT", "8000"))
    print(f"Starting demo dashboard at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
