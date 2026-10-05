# Backup and Restore Runbook

This document describes how the DCA-Bot persistent data is backed up, validated, retained, and restored.

## Backup scope

The following files are included in every backup:

| File | Purpose |
|------|---------|
| `config.json` | Bot configuration (no secrets). |
| `transactions.json` | Trade history. |
| `audit_log.json` | Configuration/control change audit trail. |
| `state.json` | Persisted bot runtime state (if present). |
| `runtime_overrides.json` | Pause/resume/manual-cycle overrides. |
| `strategy_state.json` | Per-strategy cycle state (future framework; backed up if present). |
| `heartbeat.json` | Recent trading-loop heartbeat (operationally useful). |
| `transactions.db` | SQLite migration target, if it exists. |

## Exclusions

The following are **never** backed up:

- `.env` and any other secret files
- Cache files (`price_cache.json`, `account_balances.json`)
- `*.bak` temporary copies
- `*.tmp` partial files
- Container logs and container layers
- `__pycache__` and Python bytecode

## Backup schedule

A systemd timer triggers a backup every day at **02:30** local server time.

Files:

- `systemd/dca-bot-backup.service`
- `systemd/dca-bot-backup.timer`

To install:

```bash
sudo cp systemd/dca-bot-backup.service systemd/dca-bot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dca-bot-backup.timer
```

Verify the timer:

```bash
sudo systemctl list-timers dca-bot-backup.timer
sudo systemctl status dca-bot-backup.service
```

## Retention

Defaults (configurable in the systemd unit or via environment variables):

| Setting | Default | Purpose |
|---------|---------|---------|
| `BACKUP_DAILY_COUNT` | `14` | Number of recent daily backups to keep. |
| `BACKUP_WEEKLY_COUNT` | `8` | Number of weekly snapshots to keep beyond the daily window. |
| `RETENTION_DAYS` | `30` | Hard ceiling; anything older is deleted. |

The retention policy keeps the newest backup from each ISO week for historical coverage, then removes everything else older than `RETENTION_DAYS`.

## Manual backup

Run from the production host as the `deploy` user:

```bash
/opt/crypto-agent/scripts/backup.sh
```

Override directories for a one-off run:

```bash
DATA_DIR=/opt/crypto-agent/data \
BACKUP_DIR=/opt/crypto-agent/backups \
BACKUP_DAILY_COUNT=7 \
BACKUP_WEEKLY_COUNT=4 \
/opt/crypto-agent/scripts/backup.sh
```

On success the script prints:

```json
{"status": "ok", "archive": "crypto-agent-data-YYYYMMDD-HHMMSS.tar.gz", "checksum": "<sha256>"}
```

A failure prints:

```json
{"status": "failed", "error": "<reason>"}
```

## Backup validation

Every backup run performs the following checks before the archive is finalized:

1. Required files (`config.json`, `transactions.json`) exist.
2. Every included `.json` file parses as valid JSON.
3. The archive can be opened and contains the expected members.
4. A SHA-256 checksum is written to `<archive>.tar.gz.sha256`.

A `backup_status.json` file is written to the backup directory:

```json
{
  "last_attempted_at": "2026-07-25T02:30:00+00:00",
  "last_successful_at": "2026-07-25T02:30:01+00:00",
  "archive_name": "crypto-agent-data-20260725-023001.tar.gz",
  "archive_path": "/opt/crypto-agent/backups/crypto-agent-data-20260725-023001.tar.gz",
  "checksum": "<sha256>",
  "validation_status": "ok",
  "failure_reason": null,
  "retained_backups": 22
}
```

This file intentionally contains no secrets.

## Restore prerequisites

Before restoring:

1. Prefer restoring to a staging instance first.
2. Stop the bot container to avoid concurrent writes.
3. Confirm the backup archive and its `.sha256` sidecar are present.
4. Verify the archive checksum manually if desired:

```bash
cd /opt/crypto-agent/backups
sha256sum -c crypto-agent-data-YYYYMMDD-HHMMSS.tar.gz.sha256
```

## Restore procedure

The restore script refuses to run if the bot appears to be running (recent heartbeat), unless `--force` is used.

### Dry-run first

```bash
/opt/crypto-agent/scripts/restore_backup.sh \
  /opt/crypto-agent/backups/crypto-agent-data-YYYYMMDD-HHMMSS.tar.gz \
  --dry-run
```

### Production restore

1. Stop the container:

```bash
sudo systemctl stop crypto-agent.service
```

2. Run the restore script and confirm:

```bash
/opt/crypto-agent/scripts/restore_backup.sh \
  /opt/crypto-agent/backups/crypto-agent-data-YYYYMMDD-HHMMSS.tar.gz
```

The script creates a pre-restore snapshot at `data/.pre-restore/pre-restore-YYYYMMDD-HHMMSS.tar.gz` before overwriting files.

3. Restart the bot:

```bash
sudo systemctl start crypto-agent.service
```

### Staging restore test

To test a backup without touching production:

```bash
mkdir -p /tmp/dca-restore-test/data
DATA_DIR=/tmp/dca-restore-test/data \
/opt/crypto-agent/scripts/restore_backup.sh \
  /opt/crypto-agent/backups/crypto-agent-data-YYYYMMDD-HHMMSS.tar.gz \
  --force
```

Then start a staging container pointing at `/tmp/dca-restore-test/data`.

## Rollback

If a restore causes problems, use the pre-restore snapshot:

```bash
/opt/crypto-agent/scripts/restore_backup.sh \
  /opt/crypto-agent/data/.pre-restore/pre-restore-YYYYMMDD-HHMMSS.tar.gz \
  --force
```

For application-level rollback (container image), use `scripts/rollback.sh` as documented in `docs/operations.md`.

## Monitoring integration

`/health/ready` includes backup age when a `backup_status.json` file is present:

```json
{
  "checks": {
    "backup_status": "ok",
    "backup_age_seconds": 86400
  }
}
```

Set `BACKUP_DIR` if the status file is not in the default `backups/` directory.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Missing required files: [...]` | `config.json` or `transactions.json` absent. | Verify data directory path and files. |
| `Invalid JSON in ...` | Corrupt runtime data. | Inspect the file; fix or restore from a previous backup. |
| `Checksum mismatch` | Archive was altered after creation. | Use a different backup file. |
| `Bot appears to be running` | Recent heartbeat detected. | Stop the container before restoring. |
| `Archive contains unsafe members` | Malicious or malformed tarball. | Do not restore; inspect the archive. |
| Backups not running daily | Timer not enabled or service failed. | Check `systemctl status dca-bot-backup.timer`. |

## Verification checklist

- [ ] `systemctl status dca-bot-backup.timer` shows active and next trigger.
- [ ] A manual backup run succeeds and produces a `.tar.gz` + `.sha256`.
- [ ] `backup_status.json` shows `validation_status: "ok"`.
- [ ] A restore dry-run reports success without changing data.
- [ ] A restore into a temporary directory matches the source files.
- [ ] `/health/ready` reports backup age when the status file exists.
- [ ] Retention removes old backups but keeps the configured daily/weekly counts.
