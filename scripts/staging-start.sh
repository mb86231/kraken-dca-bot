#!/bin/sh
set -eu

# Start the isolated staging instance.
# This script must be run on the staging host as a user with podman-compose access.

DEPLOY_DIR="${DEPLOY_DIR:-/opt/crypto-agent-staging}"
COMPOSE_FILE="${DEPLOY_DIR}/compose.staging.yaml"

if [ ! -f "${COMPOSE_FILE}" ]; then
    echo "ERROR: staging compose file not found: ${COMPOSE_FILE}"
    exit 1
fi

if [ ! -f "${DEPLOY_DIR}/config/.env" ]; then
    echo "ERROR: staging env file missing: ${DEPLOY_DIR}/config/.env"
    exit 1
fi

cd "${DEPLOY_DIR}"
echo "Starting crypto-agent-staging..."
sudo podman-compose -p crypto-agent-staging -f "${COMPOSE_FILE}" up -d --force-recreate

echo "Waiting for health..."
for i in $(seq 1 30); do
    status=$(sudo podman inspect --format='{{.State.Health.Status}}' crypto-agent-staging 2>/dev/null || echo starting)
    if [ "${status}" = "healthy" ]; then
        echo "Staging container is healthy."
        exit 0
    fi
    echo "Health status: ${status} (retry ${i}/30)"
    sleep 5
done

echo "ERROR: staging container did not become healthy"
exit 1
