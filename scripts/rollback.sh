#!/bin/sh
set -eu

# Manual rollback helper for the production host.
# Usage: IMAGE_TAG=<previous-sha> /opt/crypto-agent/scripts/rollback.sh

DEPLOY_DIR="${DEPLOY_DIR:-/opt/crypto-agent}"
IMAGE_NAME="${IMAGE_NAME:?IMAGE_NAME must be set}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG must be set}"

echo "Rolling crypto-agent back to ${IMAGE_NAME}:${IMAGE_TAG}"

export IMAGE_NAME IMAGE_TAG
cd "$DEPLOY_DIR"
sudo podman pull "${IMAGE_NAME}:${IMAGE_TAG}"
sudo podman-compose -p crypto-agent -f compose.yaml up -d --force-recreate

# Wait for the container to become healthy.
for i in $(seq 1 30); do
    status=$(sudo podman inspect --format='{{.State.Health.Status}}' crypto-agent 2>/dev/null || echo "starting")
    if [ "$status" = "healthy" ]; then
        echo "Rollback successful: container is healthy"
        exit 0
    fi
    echo "Waiting for health... ($status)"
    sleep 5
done

echo "ERROR: rollback container did not become healthy within timeout"
exit 1
