#!/bin/sh
set -eu

# Reset staging data to a clean state. Production is never touched.

DEPLOY_DIR="${DEPLOY_DIR:-/opt/crypto-agent-staging}"
DATA_DIR="${DEPLOY_DIR}/data"
BACKUP_DIR="${DEPLOY_DIR}/backups"
LOGS_DIR="${DEPLOY_DIR}/logs"

echo "WARNING: This will delete all staging data under ${DEPLOY_DIR}."
echo "Production under /opt/crypto-agent will NOT be affected."
read -r -p "Type 'staging' to confirm: " confirm
if [ "${confirm}" != "staging" ]; then
    echo "Aborting."
    exit 1
fi

# Stop the staging container first to avoid file conflicts.
if sudo podman ps --filter name=crypto-agent-staging --format '{{.Names}}' | grep -q crypto-agent-staging; then
    echo "Stopping staging container..."
    sudo podman-compose -p crypto-agent-staging -f "${DEPLOY_DIR}/compose.staging.yaml" down
fi

echo "Removing staging data..."
sudo rm -rf "${DATA_DIR}" "${BACKUP_DIR}" "${LOGS_DIR}"

# Recreate empty directories and seed empty transaction log.
sudo mkdir -p "${DATA_DIR}" "${BACKUP_DIR}" "${LOGS_DIR}"
sudo chown -R 1500:1500 "${DATA_DIR}" "${BACKUP_DIR}" "${LOGS_DIR}"
echo '[]' > "${DATA_DIR}/transactions.json"

# Copy the default config if it exists.
if [ -f "${DEPLOY_DIR}/config.json.default" ]; then
    cp "${DEPLOY_DIR}/config.json.default" "${DATA_DIR}/config.json"
fi

echo "Staging data reset. Start with scripts/staging-start.sh"
