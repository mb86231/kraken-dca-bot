#!/bin/sh
set -eu

# Restore wrapper for the DCA-Bot. The actual implementation lives in
# bot/restore.py so it can be unit-tested and run on Windows too.
#
# Usage:
#   /opt/crypto-agent/scripts/restore_backup.sh /opt/crypto-agent/backups/crypto-agent-data-YYYYMMDD-HHMMSS.tar.gz
#
# Environment variables:
#   DATA_DIR  target data directory (default /opt/crypto-agent/data)
#
# Options are passed through to bot/restore.py:
#   --dry-run       validate only; do not modify data
#   --force         restore even if the bot appears to be running
#   --yes           skip interactive confirmation

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

export DATA_DIR="${DATA_DIR:-/opt/crypto-agent/data}"

cd "$PROJECT_DIR"
exec python3 -m bot.restore "$@"
