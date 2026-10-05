"""Restore logic for the DCA-Bot backup archives.

Can be executed as a CLI via ``python -m bot.restore <archive>`` from the
project root.
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

from bot.backup import _validate_json_file

DEFAULT_DATA_DIR = "/opt/crypto-agent/data"
DEFAULT_BACKUP_DIR = "/opt/crypto-agent/backups"
DEFAULT_RUNNING_THRESHOLD_SECONDS = 300


def _log(level: str, message: str, **extra: Any) -> None:
    obj: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": "restore",
        "message": message,
    }
    obj.update(extra)
    print(json.dumps(obj, default=str), file=sys.stderr)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _checksum_for_archive(archive_path: Path) -> str | None:
    """Read the expected SHA-256 checksum from the sidecar file if it exists."""
    sidecar = archive_path.parent / f"{archive_path.name}.sha256"
    if not sidecar.exists():
        return None
    content = sidecar.read_text(encoding="utf-8").strip().split()
    return content[0] if content else None


def _is_safe_member(member: tarfile.TarInfo) -> bool:
    """Reject absolute paths, parent-directory traversal, and symlinks."""
    name = member.name
    if os.path.isabs(name):
        return False
    parts = Path(name).parts
    if ".." in parts or parts[0].startswith("/"):
        return False
    if member.issym() or member.islnk():
        return False
    return True


def _app_recently_heartbeat(data_dir: Path, threshold_seconds: int = DEFAULT_RUNNING_THRESHOLD_SECONDS) -> bool:
    """Return True if the heartbeat file suggests the bot is currently running."""
    heartbeat = data_dir / "heartbeat.json"
    if not heartbeat.exists():
        return False
    try:
        data = json.loads(heartbeat.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < threshold_seconds
    except (KeyError, ValueError, json.JSONDecodeError, OSError):
        return False


def _snapshot_current_files(
    data_dir: Path,
    snapshot_dir: Path,
    scope: list[str],
) -> Path:
    """Create a pre-restore tarball of the current files in ``scope``."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot_path = snapshot_dir / f"pre-restore-{timestamp}.tar.gz"
    with tarfile.open(snapshot_path, "w:gz") as tar:
        for name in scope:
            path = data_dir / name
            if path.exists():
                tar.add(path, arcname=name)
    return snapshot_path


def restore_backup(
    archive_path: Path,
    data_dir: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    running_threshold_seconds: int = DEFAULT_RUNNING_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """Restore ``archive_path`` into ``data_dir``.

    Validates the checksum, archive contents, and JSON files. A pre-restore
    snapshot is created before any files are overwritten. Returns a status
    dictionary describing the outcome.
    """
    archive_path = Path(archive_path)
    data_dir = Path(data_dir)

    result: dict[str, Any] = {
        "status": "failed",
        "archive": str(archive_path),
        "data_dir": str(data_dir),
        "dry_run": dry_run,
        "files_restored": [],
        "pre_restore_snapshot": None,
        "error": None,
    }

    if not archive_path.is_file():
        result["error"] = f"Archive not found: {archive_path}"
        _log("ERROR", result["error"])
        return result

    expected_checksum = _checksum_for_archive(archive_path)
    actual_checksum = _sha256_file(archive_path)
    if expected_checksum and actual_checksum != expected_checksum:
        result["error"] = (
            f"Checksum mismatch: expected {expected_checksum}, got {actual_checksum}"
        )
        _log("ERROR", result["error"])
        return result

    if not force and _app_recently_heartbeat(data_dir, running_threshold_seconds):
        result["error"] = (
            "Bot appears to be running (recent heartbeat). "
            "Stop the container first or use --force to override."
        )
        _log("ERROR", result["error"])
        return result

    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            unsafe = [m.name for m in tar.getmembers() if not _is_safe_member(m)]
            if unsafe:
                result["error"] = f"Archive contains unsafe members: {unsafe}"
                _log("ERROR", result["error"])
                return result

            scope = [m.name for m in tar.getmembers() if m.isfile()]

        # Validate JSON contents before touching the data directory.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=tmp_path, filter="data")
            for name in scope:
                if name.endswith(".json"):
                    try:
                        _validate_json_file(tmp_path / name)
                    except (json.JSONDecodeError, OSError) as exc:
                        result["error"] = f"Invalid JSON in archive member {name}: {exc}"
                        _log("ERROR", result["error"])
                        return result
    except (tarfile.TarError, OSError) as exc:
        result["error"] = f"Could not read archive: {exc}"
        _log("ERROR", result["error"])
        return result

    result["files_to_restore"] = list(scope)

    if dry_run:
        result["status"] = "dry_run"
        result["error"] = None
        _log("INFO", "Dry-run restore validated", files=scope, checksum=actual_checksum)
        return result

    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = _snapshot_current_files(data_dir, data_dir / ".pre-restore", scope)
    result["pre_restore_snapshot"] = str(snapshot_path)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=tmp_path, filter="data")
            for name in scope:
                src = tmp_path / name
                dst = data_dir / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                result["files_restored"].append(name)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"Restore failed: {exc}"
        _log("ERROR", result["error"])
        return result

    result["status"] = "ok"
    result["error"] = None
    result["checksum"] = actual_checksum
    _log("INFO", "Restore completed", files=scope, snapshot=str(snapshot_path))
    return result


def _confirm_prompt(message: str) -> bool:
    try:
        answer = input(f"{message} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore a DCA-Bot backup archive.")
    parser.add_argument("archive", help="Path to the backup tarball.")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument("--dry-run", action="store_true", help="Validate only; do not modify data.")
    parser.add_argument("--force", action="store_true", help="Restore even if the bot appears running.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation (use with automation only after validation).",
    )
    args = parser.parse_args(argv)

    if not args.dry_run and not args.yes:
        confirmed = _confirm_prompt(
            f"Restore {args.archive} into {args.data_dir}? This will overwrite existing files."
        )
        if not confirmed:
            print("Restore cancelled.")
            return 1

    result = restore_backup(
        archive_path=Path(args.archive),
        data_dir=Path(args.data_dir),
        dry_run=args.dry_run,
        force=args.force,
    )

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in ("ok", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(_main())
