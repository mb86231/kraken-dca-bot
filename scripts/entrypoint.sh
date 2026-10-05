#!/bin/sh
set -eu

# Safety banner: make it obvious whether real orders will be placed.
# Live trading can be enabled via the LIVE_TRADING_ENABLED environment variable
# or via "live_trading_enabled": true in config.json (managed in the dashboard).
CONFIG_PATH="${CONFIG_PATH:-/app/data/config.json}"
live_from_config=false
if grep -q '"live_trading_enabled"[[:space:]]*:[[:space:]]*true' "$CONFIG_PATH" 2>/dev/null; then
    live_from_config=true
fi
if [ "${LIVE_TRADING_ENABLED:-false}" = "true" ] || [ "$live_from_config" = "true" ]; then
    echo "========================================================================"
    echo "WARNING: LIVE TRADING IS ENABLED. REAL MARKET ORDERS WILL BE PLACED."
    echo "========================================================================"
else
    echo "------------------------------------------------------------------------"
    echo "LIVE TRADING IS DISABLED. Running in dry-run / validation mode."
    echo "No orders will be placed on Kraken."
    echo "------------------------------------------------------------------------"
fi

exec python3 -u /app/main.py
