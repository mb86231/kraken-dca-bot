"""Tests for backup creation, validation, retention, and restore."""

from __future__ import annotations

import json
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.backup import (
    apply_retention,
    create_backup,
)
from bot.restore import restore_backup


def _write_good_data(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(
        '{"trading_pair": "XBTCHF", "crypto_amount": 0.0001}', encoding="utf-8"
    )
    (data_dir / "transactions.json").write_text("[]", encoding="utf-8")
    (data_dir / "runtime_overrides.json").write_text(
        json.dumps({"paused": False, "manual_cycle_requested": False}), encoding="utf-8"
    )


def test_successful_backup(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    _write_good_data(data_dir)

    status = create_backup(data_dir, backup_dir)

    assert status["validation_status"] == "ok"
    assert status["archive_name"] is not None
    assert status["checksum"] is not None
    assert status["failure_reason"] is None
    archive_path = backup_dir / status["archive_name"]
    assert archive_path.exists()
    assert (backup_dir / f"{status['archive_name']}.sha256").exists()
    assert (backup_dir / "backup_status.json").exists()

    # Archive contains expected files.
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
    assert "config.json" in names
    assert "transactions.json" in names
    assert "runtime_overrides.json" in names


def test_backup_fails_when_required_file_missing(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text("{}")
    # transactions.json is intentionally missing.

    status = create_backup(data_dir, backup_dir)

    assert status["validation_status"] == "failed"
    assert "transactions.json" in status["failure_reason"]


def test_backup_fails_on_corrupt_json(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text("{}")
    (data_dir / "transactions.json").write_text("not json")

    status = create_backup(data_dir, backup_dir)

    assert status["validation_status"] == "failed"
    assert "Invalid JSON" in status["failure_reason"]


def test_restore_detects_corrupt_archive(tmp_path: Path):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(b"not a tar archive")
    data_dir = tmp_path / "data"

    result = restore_backup(archive, data_dir)

    assert result["status"] == "failed"


def test_restore_detects_checksum_mismatch(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    _write_good_data(data_dir)

    status = create_backup(data_dir, backup_dir)
    archive_path = backup_dir / status["archive_name"]

    # Tamper with the archive after creation.
    original = archive_path.read_bytes()
    archive_path.write_bytes(original + b"extra")

    result = restore_backup(archive_path, tmp_path / "restore_target")

    assert result["status"] == "failed"
    assert "Checksum mismatch" in result["error"]


def test_restore_refuses_path_traversal(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    restore_dir = tmp_path / "restore"
    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "config.json").write_text("{}")
    (data_dir / "transactions.json").write_text("[]")

    archive_path = backup_dir / "evil.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(data_dir / "config.json", arcname="../escaped_config.json")

    result = restore_backup(archive_path, restore_dir)

    assert result["status"] == "failed"
    assert "unsafe members" in result["error"]


def test_restore_dry_run_does_not_modify_data(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    _write_good_data(data_dir)

    status = create_backup(data_dir, backup_dir)
    archive_path = backup_dir / status["archive_name"]

    # Change the live data after the backup was taken.
    (data_dir / "config.json").write_text(
        '{"trading_pair": "XXBTZUSD", "crypto_amount": 0.001}', encoding="utf-8"
    )
    original_config = (data_dir / "config.json").read_text(encoding="utf-8")

    result = restore_backup(archive_path, data_dir, dry_run=True)

    assert result["status"] == "dry_run"
    assert (data_dir / "config.json").read_text(encoding="utf-8") == original_config


def test_successful_restore_into_temp_directory(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    restore_dir = tmp_path / "restore"
    _write_good_data(data_dir)

    status = create_backup(data_dir, backup_dir)
    archive_path = backup_dir / status["archive_name"]

    # Alter original after backup.
    (data_dir / "config.json").write_text("{}", encoding="utf-8")

    result = restore_backup(archive_path, restore_dir, force=True)

    assert result["status"] == "ok"
    assert "config.json" in result["files_restored"]
    assert (restore_dir / "config.json").read_text(encoding="utf-8") == '{"trading_pair": "XBTCHF", "crypto_amount": 0.0001}'
    assert (restore_dir / "transactions.json").exists()


def test_restore_refuses_when_bot_appears_running(tmp_path: Path):
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    _write_good_data(data_dir)

    status = create_backup(data_dir, backup_dir)
    archive_path = backup_dir / status["archive_name"]

    # Write a very recent heartbeat.
    recent = datetime.now(timezone.utc) - timedelta(seconds=30)
    (data_dir / "heartbeat.json").write_text(
        json.dumps({"timestamp": recent.isoformat()}), encoding="utf-8"
    )

    result = restore_backup(archive_path, data_dir)

    assert result["status"] == "failed"
    assert "Bot appears to be running" in result["error"]


def test_apply_retention_keeps_daily_and_weekly_backups(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    # Create 60 daily backups so there are enough distinct ISO weeks.
    for i in range(60):
        ts = now - timedelta(days=i)
        name = f"crypto-agent-data-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        path = backup_dir / name
        path.write_text("dummy")
        # Set mtime to the synthetic date.
        epoch = (ts - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
        os.utime(path, (epoch, epoch))

    retained = apply_retention(backup_dir, retention_days=60, daily_count=5, weekly_count=4)

    # 5 dailies + 4 weekly snapshots beyond the daily window.
    assert retained == 9
    remaining = list(backup_dir.glob("crypto-agent-data-*.tar.gz"))
    assert len(remaining) == 9


def test_apply_retention_respects_hard_age_ceiling(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    # One very old backup.
    old = now - timedelta(days=40)
    path = backup_dir / f"crypto-agent-data-{old.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    path.write_text("dummy")
    epoch = (old - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
    os.utime(path, (epoch, epoch))

    # One recent backup.
    recent_path = backup_dir / f"crypto-agent-data-{now.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    recent_path.write_text("dummy")

    apply_retention(backup_dir, retention_days=30, daily_count=10, weekly_count=10)

    assert not path.exists()
    assert recent_path.exists()
