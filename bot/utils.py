"""Shared utilities: atomic file writes, formatting, safe JSON loading."""

from __future__ import annotations

import calendar
import json
import os
import platform
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


APP_VERSION = "1.1.0"

# Dashboard/bot display timezone. Change this if you want dates shown in a
# different zone. UTC is used internally for storage and scheduling.
DISPLAY_TZ = ZoneInfo("Europe/Zurich")


def now_tz() -> datetime:
    """Return the current time in the configured display timezone."""
    return datetime.now(timezone.utc).astimezone(DISPLAY_TZ)


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def add_months(dt: datetime, months: int) -> datetime:
    """Return a datetime ``months`` after ``dt``, rolling days that overflow the
    target month to the last valid day. Preserves time-of-day and timezone.
    """
    new_month = dt.month + months
    new_year = dt.year + (new_month - 1) // 12
    new_month = (new_month - 1) % 12 + 1
    max_day = calendar.monthrange(new_year, new_month)[1]
    new_day = min(dt.day, max_day)
    return dt.replace(year=new_year, month=new_month, day=new_day)


def format_datetime(dt: datetime | str | None) -> str | None:
    """Format a datetime for display in the configured display timezone."""
    if dt is None:
        return None
    parsed: datetime
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    else:
        parsed = dt
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def format_currency(value: float | None, currency: str = "") -> str:
    """Format a fiat/crypto value with optional currency suffix."""
    if value is None:
        return "—"
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def format_crypto(value: float | None) -> str:
    """Format a crypto quantity."""
    if value is None:
        return "—"
    return f"{value:.8f}"


def mask_secret(value: str | None, visible_tail: int = 4) -> str:
    """Return a masked representation of a secret.

    Example: mask_secret("abc123") -> "••••123"
    """
    if not value:
        return ""
    if len(value) <= visible_tail:
        return "•" * len(value)
    return "•" * (len(value) - visible_tail) + value[-visible_tail:]


def safe_load_json(filepath: Path) -> Any:
    """Load JSON from a file, returning an empty list on corruption/missing file."""
    if not filepath.exists():
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def atomic_write_json(filepath: Path, data: Any, indent: int = 2) -> None:
    """Atomically write JSON data to filepath using a temporary file + rename.

    Creates a .bak backup of the previous file before overwriting.
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Backup existing file
    if filepath.exists():
        backup_path = filepath.with_suffix(filepath.suffix + ".bak")
        try:
            shutil.copy2(filepath, backup_path)
        except OSError:
            pass

    # Write to temp file in the same directory, then rename for atomicity.
    # If the target is a bind-mounted file, os.replace may fail with
    # EBUSY/EXDEV. In that case fall back to a non-atomic copy.
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp", prefix=filepath.name + ".", dir=str(filepath.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, default=_json_default)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp_path, filepath)
        except OSError as e:
            if e.errno in (16, 18):  # EBUSY / EXDEV
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


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def get_platform_info() -> dict[str, Any]:
    """Return basic platform/runtime information."""
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "node": platform.node(),
    }


def redact_sensitive(text: str) -> str:
    """Redact common secret patterns from log/error text."""
    if not text:
        return text
    import re

    patterns = [
        (r'(API-Key|[\"\']?api[_-]?key[\"\']?\s*[:=]\s*)["\']?[A-Za-z0-9/+=]+["\']?', r'\1[REDACTED]'),
        (r'(API-Sign|[\"\']?api[_-]?secret[\"\']?\s*[:=]\s*)["\']?[A-Za-z0-9/+=]+["\']?', r'\1[REDACTED]'),
        (r'([\"\']?bot[_-]?token[\"\']?\s*[:=]\s*)["\']?[0-9]+:[A-Za-z0-9_-]+["\']?', r'\1[REDACTED]'),
        (r'(telegram\.org/bot)[A-Za-z0-9:_-]+(/sendMessage)', r'\1[REDACTED]\2'),
    ]
    for pattern, repl in patterns:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text
