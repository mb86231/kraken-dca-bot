"""Tests for structured logging and secret redaction."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from bot.logger import JsonFormatter, RedactingFilter, StreamToLogger, setup_logging


def test_redacting_filter_strips_api_key():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="API-Key: abc123secret",
        args=(),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert "abc123secret" not in record.msg
    assert "[REDACTED]" in record.msg


def test_redacting_filter_strips_secret_from_args():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="error: %s",
        args=("api_secret=xyz789",),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert "xyz789" not in str(record.args)


def test_json_formatter_outputs_json():
    record = logging.LogRecord(
        name="dca_bot",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )
    line = JsonFormatter().format(record)
    obj = json.loads(line)
    assert obj["level"] == "INFO"
    assert obj["message"] == "hello"
    assert obj["component"] == "dca_bot"


def test_stream_to_logger_redirects(tmp_path: Path, monkeypatch):
    os.environ["LOG_LEVEL"] = "DEBUG"
    logger = setup_logging(log_dir=tmp_path / "logs")
    stream = StreamToLogger(logger, logging.DEBUG)
    stream.write("line one\nline two\n")
    stream.flush()
    assert stream.writable() is True
    assert stream.isatty() is False


def test_setup_logging_returns_same_logger():
    logger1 = setup_logging()
    logger2 = setup_logging()
    assert logger1 is logger2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
