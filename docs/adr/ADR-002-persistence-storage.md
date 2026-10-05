# ADR-002: JSON Persistence with Cross-Process File Locking for v1.0

## Status

Accepted — 2026-07-26

## Context

The DCA-Bot stores runtime and historical state in JSON files that are accessed by
multiple processes:

- `data/transactions.json` — local transaction ledger.
- `data/order_attempts.json` — order-attempt state machine and retry queue.
- `data/state.json` — bot state (paused, running, next cycle, etc.).
- `data/runtime_overrides.json` — operator runtime overrides.
- `data/heartbeat.json` — trading-loop heartbeat.
- `data/preflight.json` — production preflight result.
- `data/alerts.json` — active alerts.
- `backups/backup_status.json` — backup metadata and checksums.

Before this decision, writes were atomic at the file level (temporary file +
`os.replace`) but were not synchronized across processes. The bot, dashboard,
operational scripts, backup process, and any future workers could therefore
read the same version of a file, compute independent updates, and overwrite each
other. For a trading system this creates a risk that the local transaction or
order-attempt state diverges from the exchange state.

A SQLite migration script (`scripts/migrate_to_sqlite.py`) exists as a research
prototype but has not been fully integrated, tested for all stores, or validated
for rollback.

## Decision

For **Production Hardened v1.0**, the project will:

1. Keep JSON as the storage format.
2. Add a centralized, cross-process locking abstraction (`bot.persistence.JsonFile`).
3. Defer the SQLite migration to a future milestone with its own design,
   migration tests, and rollback plan.

The locking implementation uses the mature, maintained `filelock` library
(version 3.32.0, pinned in `requirements.txt`). `JsonFile` provides three
primary operations:

- `load()` — read JSON under an exclusive cross-process lock.
- `save()` — write JSON atomically under an exclusive cross-process lock.
- `update(transform)` — read, transform, and write atomically as one locked
  transaction.

Lock files are derived deterministically from the data file path
(`<file>.lock`). Lock acquisition has a bounded timeout
(`DEFAULT_LOCK_TIMEOUT = 10s`). A timeout raises `LockAcquisitionError` rather
than blocking forever.

## Consequences

### Positive

- Lost updates are prevented when multiple processes touch the same JSON file.
- Atomic temporary-file writes and `os.replace` behavior are preserved.
- The change is backward-compatible: existing data files remain valid.
- No risky storage migration is performed during the go-live phase.
- The abstraction makes a future SQLite migration easier because callers already
  route persistence through a single interface.

### Negative

- All readers and writers serialize on one exclusive lock per file. For the
  bot's low-contention workload this is acceptable, but it could become a
  bottleneck if many concurrent dashboard users or workers are added.
- Lock files must be created in the same directory as the data files, which
  requires write permission to that directory.
- Network filesystems may not implement `filelock` reliably; production
  deployments should use local volumes.

## Locking design

### Lock scope

The lock covers the complete read-modify-write transaction, not just the final
file replacement. For example, `JsonFile.update()` does the following while
holding the lock:

1. Load the current on-disk value.
2. Apply the caller's transform.
3. Serialize the new value to a temporary file on the same filesystem.
4. Flush and `fsync` the temporary file.
5. Atomically replace the destination file.
6. Release the lock.

This eliminates the classic lost-update race:

```text
Process A reads v1
Process B reads v1
Process A writes v2
Process B writes v3 based on v1  <-- A's update is not lost because B's
                                    transform would have been applied to v2
```

### Timeout behavior

- Default timeout is 10 seconds.
- A timeout raises `LockAcquisitionError` with the file path and timeout.
- Callers should log the failure and, where appropriate, expose it in the
  operational status endpoint.
- The dashboard's read-only endpoints may return `503` if they cannot acquire a
  lock, rather than serving stale or partial state.

### Lock ordering

If multiple files must be locked by the same operation, always lock them in a
consistent global order (alphabetical by absolute path). This prevents
lock-order inversion and deadlock.

### Backup interaction

Backups receive a consistent snapshot by either:

- Acquiring the relevant locks before copying files, or
- Copying the lock file together with the data file and restoring both.

The `JsonFile.locked()` accessor returns the underlying `FileLock` so backup and
restore scripts can coordinate with writers.

## Atomic-write behavior

`JsonFile` preserves the historical atomic-write strategy:

1. Create a temporary file in the destination directory.
2. Write JSON to it and `fsync`.
3. Replace the destination file atomically with `os.replace`.
4. On `EBUSY`/`EXDEV`, fall back to `shutil.copy2` and remove the temporary file.
5. On any unexpected error, remove the temporary file safely.

Temporary files are named `<filename>.<random>.tmp` and are never left behind
after a successful write.

## Failure behavior

| Scenario | Behavior |
|----------|----------|
| Lock timeout | `LockAcquisitionError`; caller logs; dashboard returns 503 if applicable |
| Corrupt or missing JSON file | `load()`/`update()` return an empty list (matches historical `safe_load_json`) |
| Atomic replace fails (cross-device, busy) | Falls back to copy/unlink; original file is still replaced if copy succeeds |
| Temporary file cleanup fails | Error is ignored; original exception is re-raised |
| Backup copy of existing file fails | Error is ignored; new file is still written |
| Process dies while holding lock | `filelock` releases the lock when the file descriptor is closed by the OS |

## Supported deployment assumptions

- The bot and dashboard run with access to the same local filesystem.
- Data files live on a filesystem that supports POSIX advisory locks or Windows
  file locking (`filelock` handles both).
- No two containers mount the data directory over a network filesystem that does
  not implement locking.

## SQLite migration status

`scripts/migrate_to_sqlite.py` is a research prototype that copies
`transactions.json` into `data/transactions.db`. It is **not integrated** into
production startup and is **not the active storage layer**.

Before SQLite can be adopted, the following must be completed:

- Schema definitions for all stores (transactions, order attempts, bot state,
  runtime overrides, alerts, preflight, heartbeat, backup status).
- Migrations with version tracking and rollback scripts.
- Numeric precision tests for amounts, prices, and fees.
- Concurrency tests equivalent to the current JSON locking tests.
- Backup/restore tests for the SQLite database.
- Corrupt-database recovery tests.
- A documented migration window and rollback plan.
- Dashboard and bot code updated to use SQLite for all reads and writes, or a
  repository pattern that hides the storage backend.

## Rollback considerations

If the JSON + locking implementation must be rolled back:

1. Data files remain valid JSON; no transformation is required.
2. Lock files (`.lock`) can be deleted if they are stale and no process is running.
3. The previous atomic-write helper (`bot.utils.atomic_write_json`) can still
  write the same files, but cross-process safety would be lost.

## Related documents

- `bot/persistence.py`
- `bot/utils.py`
- `tests/test_persistence_locking.py`
- `docs/operations/BACKUP_RESTORE_RUNBOOK.md`
- `docs/operations/INCIDENT_RUNBOOK.md`
