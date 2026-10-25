"""Audit log for AI advisor interactions.

One JSON record per interaction, written to a dedicated rotating file
(``logs/advisor.json``) through the existing redacting JSON log
infrastructure. Records never contain secrets or full prompts — only a
SHA-256 of the rendered prompt plus the versioned template reference.
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bot.logger import JsonFormatter, RedactingFilter
from bot.utils import utc_now

AUDIT_LOGGER_NAME = "dca_bot.advisor.audit"
AUDIT_FILE_NAME = "advisor.json"


@dataclass
class InteractionRecord:
    """One advisor interaction, suitable for JSON serialisation."""

    interaction_id: str
    feature: str
    provider: str
    model: str
    prompt_version: str
    schema_version: str | None
    data_timestamp: str | None
    status: str  # ok | refused | invalid_json | schema_invalid | stale | error | disabled
    latency_ms: float | None
    tokens_used: int | None
    cost_estimate: float | None
    error_category: str | None
    prompt_sha256: str
    created_at: str = field(default_factory=lambda: utc_now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def hash_prompt(*parts: str) -> str:
    """Stable prompt identifier for the audit trail (never the prompt itself)."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class AdvisorAudit:
    """Writes interaction records; never raises into bot code.

    The underlying logger is namespaced per resolved log directory so
    instances pointing at different directories (e.g. in tests) do not
    share handlers."""

    def __init__(self, log_dir: str | Path = "logs"):
        resolved = Path(log_dir)
        suffix = hashlib.sha256(str(resolved.absolute()).encode()).hexdigest()[:8]
        self._logger = logging.getLogger(f"{AUDIT_LOGGER_NAME}.{suffix}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            console = logging.StreamHandler(sys.stderr)
            console.setFormatter(JsonFormatter())
            console.addFilter(RedactingFilter())
            self._logger.addHandler(console)
            try:
                resolved.mkdir(parents=True, exist_ok=True)
                file_handler = logging.handlers.RotatingFileHandler(
                    resolved / AUDIT_FILE_NAME,
                    maxBytes=5 * 1024 * 1024,
                    backupCount=3,
                )
                file_handler.setFormatter(JsonFormatter())
                file_handler.addFilter(RedactingFilter())
                self._logger.addHandler(file_handler)
            except OSError:
                # Audit is advisory; keep the console handler at minimum.
                pass

    def record(self, record: InteractionRecord) -> None:
        try:
            self._logger.info(
                "advisor_interaction %s",
                _flatten(record.to_dict()),
            )
        except Exception:
            # Auditing must never break the bot.
            pass


def _flatten(obj: dict[str, Any]) -> str:
    """Serialise a record for the JSON formatter's message field."""
    return json.dumps(obj, default=str)
