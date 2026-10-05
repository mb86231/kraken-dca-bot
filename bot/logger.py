"""Structured JSON logging with automatic redaction of secrets."""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO, cast

from bot.utils import DISPLAY_TZ


class RedactingFilter(logging.Filter):
    """Redact sensitive values from log records."""

    PATTERNS = [
        (r'(API-Key|[\"\']?api[_-]?key[\"\']?\s*[:=]\s*)["\']?[A-Za-z0-9/+=]+["\']?', r'\1[REDACTED]'),
        (r'(API-Sign|[\"\']?api[_-]?secret[\"\']?\s*[:=]\s*)["\']?[A-Za-z0-9/+=]+["\']?', r'\1[REDACTED]'),
        (r'([\"\']?bot[_-]?token[\"\']?\s*[:=]\s*)["\']?[0-9]+:[A-Za-z0-9_-]+["\']?', r'\1[REDACTED]'),
        (r'(telegram\.org/bot)[A-Za-z0-9:_-]+(/sendMessage)', r'\1[REDACTED]\2'),
        (r'(session|password|secret)\s*[:=]\s*["\']?[^\s"\']+["\']?', r'\1=[REDACTED]'),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        for pattern, repl in self.PATTERNS:
            record.msg = re.sub(pattern, repl, str(record.msg), flags=re.IGNORECASE)
            if record.args:
                record.args = tuple(
                    re.sub(pattern, repl, str(arg), flags=re.IGNORECASE) for arg in record.args
                )
        return True


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(value: str) -> str:
    """Remove terminal colour codes from a string."""
    return ANSI_RE.sub("", value)


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "level": record.levelname,
            "component": record.name,
            "message": strip_ansi(record.getMessage()),
        }
        if record.exc_info:
            obj["exception"] = strip_ansi(self.formatException(record.exc_info))
        return json.dumps(obj, default=str)


class StreamToLogger:
    """Redirect a stream (e.g. sys.stdout) to a Python logger."""

    encoding = "utf-8"

    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        self.logger = logger
        self.level = level

    def write(self, message: str) -> int:
        if not message:
            return 0
        for line in message.rstrip().splitlines():
            if line:
                self.logger.log(self.level, strip_ansi(line.rstrip()))
        return len(message)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        pass


def setup_logging(log_dir: str | Path = "logs", level: int | None = None) -> logging.Logger:
    """Configure root logger for the bot and dashboard."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    if level is None:
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger("dca_bot")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    # Console output for container logs (use stderr to avoid recursion with stdout redirection)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console.addFilter(RedactingFilter())
    logger.addHandler(console)

    # Rotating JSON file — fall back to console-only if the log directory is not writable.
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path / "app.json",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=5,
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(RedactingFilter())
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning(f"Could not create file logger at {log_path}: {e}")

    return logger


def redirect_stdout_to_logger(logger: logging.Logger) -> None:
    """Redirect sys.stdout so print() output is captured by the JSON log file."""
    sys.stdout = cast(TextIO, StreamToLogger(logger, logging.INFO))


LOGGER: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global LOGGER
    if LOGGER is None:
        LOGGER = setup_logging()
    return LOGGER
