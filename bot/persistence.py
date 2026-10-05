"""Cross-process safe JSON persistence with atomic writes and bounded locks.

This module is the single persistence abstraction for all shared JSON files in
Production Hardened v1.0. It keeps JSON as the storage format, adds cross-process
file locking via ``filelock``, and preserves the existing atomic temporary-file
write and rename behaviour from ``bot.utils.atomic_write_json``.

Design notes
------------

* One ``JsonFile`` instance per protected file. The lock file is derived from
  the data file path (``<file>.lock``).
* ``filelock`` uses an exclusive lock. Reads therefore also take the exclusive
  lock. This is slightly more conservative than shared/exclusive locks but keeps
  the implementation simple, portable, and correct for the bot's low-contention
  workload.
* All mutating operations go through ``update()`` so the whole read-modify-write
  transaction is protected by the lock. This prevents lost updates when the bot,
  dashboard, backup process, or operational scripts access the same file.
* Lock acquisition has a bounded timeout. A timeout raises
  ``LockAcquisitionError`` rather than blocking forever.
* Temporary files are created on the same filesystem as the destination so
  ``os.replace`` is atomic.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, TypeVar

from filelock import FileLock, Timeout

from bot.utils import _json_default, safe_load_json

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT = 10.0


class LockAcquisitionError(Exception):
    """Raised when a cross-process lock cannot be acquired within the timeout."""


T = TypeVar("T")


class JsonFile:
    """Cross-process locked JSON file with atomic writes."""

    def __init__(
        self,
        filepath: str | Path,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ):
        self.filepath = Path(filepath)
        self.lockfile = self.filepath.with_suffix(self.filepath.suffix + ".lock")
        self.lock_timeout = lock_timeout
        self._lock = FileLock(str(self.lockfile))

    def load(self) -> Any:
        """Load JSON from disk under a cross-process lock.

        Returns the decoded data, or an empty list if the file is missing or
        corrupt (matching the historical ``safe_load_json`` behaviour).
        """
        try:
            with self._lock.acquire(timeout=self.lock_timeout, poll_interval=0.05):
                return safe_load_json(self.filepath)
        except Timeout:
            raise LockAcquisitionError(
                f"Could not acquire read lock for {self.filepath} within {self.lock_timeout}s"
            ) from None

    def save(self, data: Any, indent: int = 2) -> None:
        """Replace the file atomically under a cross-process lock."""
        try:
            with self._lock.acquire(timeout=self.lock_timeout, poll_interval=0.05):
                _atomic_write_json_locked(self.filepath, data, indent=indent)
        except Timeout:
            raise LockAcquisitionError(
                f"Could not acquire write lock for {self.filepath} within {self.lock_timeout}s"
            ) from None

    def update(self, transform: Callable[[Any], T], indent: int = 2) -> T:
        """Read, transform, and write atomically under a cross-process lock.

        The transform receives the current on-disk value (or an empty list for
        missing/corrupt files) and must return the new value to persist. The
        returned value is also passed back to the caller so in-memory state can
        be reconciled.
        """
        try:
            with self._lock.acquire(timeout=self.lock_timeout, poll_interval=0.05):
                data = safe_load_json(self.filepath)
                new_data = transform(data)
                _atomic_write_json_locked(self.filepath, new_data, indent=indent)
                return new_data
        except Timeout:
            raise LockAcquisitionError(
                f"Could not acquire update lock for {self.filepath} within {self.lock_timeout}s"
            ) from None

    def locked(self) -> FileLock:
        """Return the underlying lock for advanced use (e.g. backup coordination)."""
        return self._lock


def _atomic_write_json_locked(filepath: Path, data: Any, indent: int = 2) -> None:
    """Atomic JSON write. Must already hold the cross-process lock.

    This mirrors ``bot.utils.atomic_write_json`` but is kept here so the
    persistence module can reason about lock scope locally.
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Backup existing file before overwriting.
    if filepath.exists():
        backup_path = filepath.with_suffix(filepath.suffix + ".bak")
        try:
            shutil.copy2(filepath, backup_path)
        except OSError:
            pass

    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        prefix=filepath.name + ".",
        dir=str(filepath.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, default=_json_default)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp_path, filepath)
        except OSError as exc:
            # EBUSY (bind-mounted file) or EXDEV (cross-device) fall back to copy.
            if exc.errno in (16, 18):
                shutil.copy2(tmp_path, filepath)
                os.unlink(tmp_path)
            else:
                raise
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
