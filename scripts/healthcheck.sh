#!/bin/sh
set -eu

# HTTP-based health check for the DCA-Bot container.
# Requires WEB_UI_ENABLED=true so the dashboard exposes /health/live and
# /health/ready. The readiness endpoint returns HTTP 200 only when the bot
# configuration, storage, and recent trading-loop heartbeat are all healthy.
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"
HEALTH_PORT="${WEB_UI_PORT:-8000}"

if ! curl -fsS "http://${HEALTH_HOST}:${HEALTH_PORT}/health/live" >/dev/null 2>&1; then
    echo "Health check failed: /health/live unreachable"
    exit 1
fi

if ! curl -fsS "http://${HEALTH_HOST}:${HEALTH_PORT}/health/ready" >/dev/null 2>&1; then
    echo "Health check failed: /health/ready did not return HTTP 200"
    exit 1
fi

echo "Health check passed"
exit 0
