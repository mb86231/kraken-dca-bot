"""Backup and retention logic for the DCA-Bot persistent data directory.

This module is intentionally written so it can be imported by tests *and* executed
as a CLI via ``python -m bot.backup`` from the project root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Files that must be present for a backup to be considered valid.
REQUIRED_FILES = [
    "config.json",
    "transactions.json",
]

# Files that are backed up if they exist. Ordering is preserved for stable tests.
OPTIONAL_FILES = [
    "audit_log.json",
    "state.json",
    "runtime_overrides.json",
    "strategy_state.json",
    "heartbeat.json",
    "transactions.db",
]

# Caches and other files that must never be included in backups.
EXCLUDED_PATTERNS = (
    ".env",
    ".env.*",
    "secrets.json",
    "secrets.json.bak",
    "*.bak",
    "*.tmp",
    "*.log",
    "price_cache.json",
    "account_balances.json",
    "__pycache__",
)

DEFAULT_DATA_DIR = "/opt/crypto-agent/data"
DEFAULT_BACKUP_DIR = "/opt/crypto-agent/backups"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_DAILY_COUNT = 14
DEFAULT_WEEKLY_COUNT = 8


def _log(level: str, message: str, **extra: Any) -> None:
    """Emit a structured JSON log line to stderr."""
    obj: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": "backup",
        "message": message,
    }
    obj.update(extra)
    print(json.dumps(obj, default=str), file=sys.stderr)


def _json_files(scope: list[str]) -> list[str]:
    return [name for name in scope if name.endswith(".json")]


def _is_excluded(path: Path) -> bool:
    name = path.name
    for pattern in EXCLUDED_PATTERNS:
        if pattern.startswith("*"):
            if name.endswith(pattern[1:]):
                return True
        elif name == pattern or name.startswith(pattern.rstrip("*")):
            return True
    return False


def _resolve_scope(data_dir: Path) -> tuple[list[str], list[str]]:
    """Return (missing_required, files_to_backup) relative to ``data_dir``."""
    missing: list[str] = []
    included: list[str] = []
    for name in REQUIRED_FILES:
        candidate = data_dir / name
        if candidate.exists():
            included.append(name)
        else:
            missing.append(name)
    for name in OPTIONAL_FILES:
        candidate = data_dir / name
        if candidate.exists() and not _is_excluded(candidate):
            included.append(name)
    return missing, included


def _validate_json_file(path: Path) -> None:
    with open(path, "r", encoding="utf-8") as f:
        json.load(f)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_archive(archive_path: Path, expected_members: list[str]) -> None:
    """Open the archive and confirm expected members and valid JSON files."""
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
    missing = [name for name in expected_members if name not in names]
    if missing:
        raise RuntimeError(f"Archive missing expected members: {missing}")

    # Extract to temp dir to validate JSON files.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=tmp_path, filter="data")
        for name in _json_files(expected_members):
            _validate_json_file(tmp_path / name)


def _atomic_rename(src: Path, dst: Path) -> None:
    """Rename ``src`` to ``dst``, falling back to copy+unlink on EXDEV/EBUSY."""
    try:
        src.replace(dst)
    except OSError as exc:
        if exc.errno in (16, 18):  # EBUSY / EXDEV
            shutil.copy2(src, dst)
            src.unlink()
        else:
            raise


def create_backup(
    data_dir: Path,
    backup_dir: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    daily_count: int = DEFAULT_DAILY_COUNT,
    weekly_count: int = DEFAULT_WEEKLY_COUNT,
) -> dict[str, Any]:
    """Create a validated, timestamped backup archive of the bot's data directory.

    Returns a status dictionary describing the outcome. Raises on unrecoverable
    filesystem errors after logging them.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive_name = f"crypto-agent-data-{timestamp}.tar.gz"
    archive_path = backup_dir / archive_name
    tmp_archive = backup_dir / f".{archive_name}.tmp"
    checksum_path = backup_dir / f"{archive_name}.sha256"

    status: dict[str, Any] = {
        "last_attempted_at": datetime.now(timezone.utc).isoformat(),
        "last_successful_at": None,
        "archive_name": None,
        "archive_path": None,
        "checksum": None,
        "validation_status": "failed",
        "failure_reason": None,
        "retained_backups": None,
    }

    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)

    if not data_dir.is_dir():
        status["failure_reason"] = f"Data directory does not exist: {data_dir}"
        _log("ERROR", status["failure_reason"])
        return status

    backup_dir.mkdir(parents=True, exist_ok=True)

    missing_required, files_to_backup = _resolve_scope(data_dir)
    if missing_required:
        status["failure_reason"] = f"Missing required files: {missing_required}"
        _log("ERROR", status["failure_reason"], files=files_to_backup)
        _write_status(backup_dir, status)
        return status

    # Validate JSON files before archiving so we never back up corrupt state.
    for name in _json_files(files_to_backup):
        try:
            _validate_json_file(data_dir / name)
        except (json.JSONDecodeError, OSError) as exc:
            status["failure_reason"] = f"Invalid JSON in {name}: {exc}"
            _log("ERROR", status["failure_reason"])
            _write_status(backup_dir, status)
            return status

    try:
        _log("INFO", "Creating backup archive", archive=str(archive_path), files=files_to_backup)
        with tarfile.open(tmp_archive, "w:gz") as tar:
            for name in files_to_backup:
                tar.add(data_dir / name, arcname=name)

        _validate_archive(tmp_archive, files_to_backup)
        _atomic_rename(tmp_archive, archive_path)
        checksum = _sha256_file(archive_path)
        checksum_path.write_text(f"{checksum}  {archive_name}\n", encoding="utf-8")

        status.update(
            {
                "last_successful_at": datetime.now(timezone.utc).isoformat(),
                "archive_name": archive_name,
                "archive_path": str(archive_path),
                "checksum": checksum,
                "validation_status": "ok",
                "failure_reason": None,
            }
        )
        _log("INFO", "Backup created and validated", archive=archive_name, checksum=checksum)
    except Exception as exc:  # noqa: BLE001
        status["failure_reason"] = f"Backup failed: {exc}"
        _log("ERROR", status["failure_reason"])
        if tmp_archive.exists():
            tmp_archive.unlink(missing_ok=True)
        _write_status(backup_dir, status)
        return status

    retained = apply_retention(backup_dir, retention_days, daily_count, weekly_count)
    status["retained_backups"] = retained
    _write_status(backup_dir, status)
    return status


def apply_retention(
    backup_dir: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    daily_count: int = DEFAULT_DAILY_COUNT,
    weekly_count: int = DEFAULT_WEEKLY_COUNT,
) -> int:
    """Delete old backups while keeping ``daily_count`` recent dailies and
    ``weekly_count`` weekly snapshots. Returns the number of backups retained.
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return 0

    backups = sorted(
        backup_dir.glob("crypto-agent-data-*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return 0

    now = datetime.now(timezone.utc)
    retained: set[Path] = set()

    # Keep the most recent ``daily_count`` backups.
    retained.update(backups[:daily_count])

    # Keep the newest backup from each of the last ``weekly_count`` ISO weeks
    # beyond the daily window.
    weekly: dict[str, Path] = {}
    for path in backups[daily_count:]:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        week_key = mtime.strftime("%G-W%V")
        if week_key not in weekly:
            weekly[week_key] = path

    retained.update(sorted(weekly.values(), key=lambda p: p.stat().st_mtime, reverse=True)[:weekly_count])

    # Remove anything that is not explicitly retained, plus anything older than
    # the retention-days ceiling even if it would otherwise be retained.
    cutoff_ts = now.timestamp() - (retention_days * 86400)
    for path in list(backups):
        if path in retained and path.stat().st_mtime >= cutoff_ts:
            continue
        _log("INFO", "Removing backup outside retention policy", archive=path.name)
        path.unlink(missing_ok=True)
        retained.discard(path)
        checksum_path = path.parent / f"{path.name}.sha256"
        if checksum_path.exists():
            checksum_path.unlink(missing_ok=True)

    return len(retained)


def _write_status(backup_dir: Path, status: dict[str, Any]) -> Path:
    """Write ``backup_status.json`` atomically."""
    status_path = Path(backup_dir) / "backup_status.json"
    tmp_path = status_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    _atomic_rename(tmp_path, status_path)
    return status_path


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup DCA-Bot persistent data.")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument("--backup-dir", default=os.environ.get("BACKUP_DIR", DEFAULT_BACKUP_DIR))
    parser.add_argument("--retention-days", type=int, default=int(os.environ.get("RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
    parser.add_argument("--daily-count", type=int, default=int(os.environ.get("BACKUP_DAILY_COUNT", DEFAULT_DAILY_COUNT)))
    parser.add_argument("--weekly-count", type=int, default=int(os.environ.get("BACKUP_WEEKLY_COUNT", DEFAULT_WEEKLY_COUNT)))
    args = parser.parse_args(argv)

    status = create_backup(
        data_dir=Path(args.data_dir),
        backup_dir=Path(args.backup_dir),
        retention_days=args.retention_days,
        daily_count=args.daily_count,
        weekly_count=args.weekly_count,
    )

    if status["validation_status"] != "ok":
        print(json.dumps({"status": "failed", "error": status["failure_reason"]}))
        return 1

    print(json.dumps({"status": "ok", "archive": status["archive_name"], "checksum": status["checksum"]}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
