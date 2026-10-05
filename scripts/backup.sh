#!/bin/sh
set -eu

# Backup wrapper for the DCA-Bot. The actual implementation lives in
# bot/backup.py so it can be unit-tested and run on Windows too.
#
# Run this from the production host (or via systemd) as the user that owns
# /opt/crypto-agent. It creates timestamped, validated, checksum-verified
# tarballs in BACKUP_DIR and writes backup_status.json.
#
# Environment variables:
#   DATA_DIR           source data directory (default /opt/crypto-agent/data)
#   BACKUP_DIR         destination directory (default /opt/crypto-agent/backups)
#   RETENTION_DAYS     hard ceiling for backup age (default 30)
#   BACKUP_DAILY_COUNT number of recent daily backups to keep (default 14)
#   BACKUP_WEEKLY_COUNT number of weekly backups to keep (default 8)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

export DATA_DIR="${DATA_DIR:-/opt/crypto-agent/data}"
export BACKUP_DIR="${BACKUP_DIR:-/opt/crypto-agent/backups}"
export RETENTION_DAYS="${RETENTION_DAYS:-30}"
export BACKUP_DAILY_COUNT="${BACKUP_DAILY_COUNT:-14}"
export BACKUP_WEEKLY_COUNT="${BACKUP_WEEKLY_COUNT:-8}"

cd "$PROJECT_DIR"
exec python3 -m bot.backup
