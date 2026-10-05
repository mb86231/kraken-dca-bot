"""Tests for shared utility helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.utils import (
    add_months,
    atomic_write_json,
    format_crypto,
    format_currency,
    format_datetime,
    mask_secret,
    redact_sensitive,
    safe_load_json,
)


def test_format_datetime_with_none():
    assert format_datetime(None) is None


def test_format_datetime_with_string():
    dt = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    assert "2026-07-25" in format_datetime(dt.isoformat())


def test_format_datetime_naive_uses_utc():
    dt = datetime(2026, 7, 25, 12, 0, 0)
    assert "2026-07-25" in format_datetime(dt)


def test_format_datetime_invalid_string_returns_input():
    assert format_datetime("not-a-date") == "not-a-date"


def test_format_currency_with_none():
    assert format_currency(None) == "—"


def test_format_currency_with_currency():
    assert format_currency(1234.5, "CHF") == "1,234.50 CHF"


def test_format_crypto_with_none():
    assert format_crypto(None) == "—"


def test_format_crypto_value():
    assert format_crypto(0.00012345) == "0.00012345"


def test_mask_secret_short():
    assert mask_secret("ab") == "••"


def test_mask_secret_long():
    masked = mask_secret("mysecretvalue")
    assert masked.endswith("alue")
    assert masked.startswith("•")


def test_safe_load_json_missing_returns_empty_list(tmp_path: Path):
    assert safe_load_json(tmp_path / "missing.json") == []


def test_safe_load_json_corrupt_returns_empty_list(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    assert safe_load_json(path) == []


def test_atomic_write_json_creates_file(tmp_path: Path):
    path = tmp_path / "data.json"
    atomic_write_json(path, {"key": "value"})
    assert json.loads(path.read_text()) == {"key": "value"}


def test_atomic_write_json_default_value(tmp_path: Path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps(["existing"]))
    atomic_write_json(path, {"new": "data"})
    assert json.loads(path.read_text()) == {"new": "data"}


def test_add_months_rolls_february():
    dt = datetime(2026, 1, 31, 8, 0, tzinfo=timezone.utc)
    result = add_months(dt, 1)
    assert result == datetime(2026, 2, 28, 8, 0, tzinfo=timezone.utc)


def test_add_months_year_rollover():
    dt = datetime(2026, 12, 24, 8, 0, tzinfo=timezone.utc)
    result = add_months(dt, 1)
    assert result == datetime(2027, 1, 24, 8, 0, tzinfo=timezone.utc)


def test_redact_sensitive_api_key():
    text = redact_sensitive("API-Key: abc123XYZ")
    assert "abc123XYZ" not in text
    assert "[REDACTED]" in text


def test_redact_sensitive_telegram_token():
    text = redact_sensitive("https://api.telegram.org/bot123:abc/sendMessage")
    assert "bot123:abc" not in text
    assert "[REDACTED]" in text


def test_redact_sensitive_empty():
    assert redact_sensitive("") == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
