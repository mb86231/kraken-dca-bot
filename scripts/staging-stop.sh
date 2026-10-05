#!/bin/sh
set -eu

# Stop the isolated staging instance without affecting production.

DEPLOY_DIR="${DEPLOY_DIR:-/opt/crypto-agent-staging}"
COMPOSE_FILE="${DEPLOY_DIR}/compose.staging.yaml"

cd "${DEPLOY_DIR}"
echo "Stopping crypto-agent-staging..."
sudo podman-compose -p crypto-agent-staging -f "${COMPOSE_FILE}" down

echo "Staging stopped."
