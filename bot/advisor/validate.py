"""Local validation of advisor responses, independent of the provider.

The validator is the single choke point between raw provider output and
any consumer. It handles invalid JSON, schema violations, model refusals,
incomplete responses, and stale data — every outcome is a non-raising
:class:`Advice` with an explicit status.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError


class AdviceStatus(StrEnum):
    OK = "ok"
    DISABLED = "disabled"
    REFUSED = "refused"
    INVALID_JSON = "invalid_json"
    SCHEMA_INVALID = "schema_invalid"
    STALE = "stale"


# Markers that indicate the model declined instead of answering. Kept
# deliberately short; a refusal is advisory-fatal, never retried. The
# patterns require an explicit inability/decline — benign preambles such
# as "As an AI language model, I have written the briefing…" must NOT
# match, otherwise compliant narratives are misclassified as refusals.
_REFUSAL_RE = re.compile(
    r"i(?:'m| am) sorry,? but i (?:cannot|can't|am unable to|won't)|"
    r"i (?:cannot|can't|am unable to) (?:assist|help|comply|provide|fulfill|continue)|"
    r"as an ai (?:language )?model,? i (?:cannot|can't|am unable to|won't|do not|don't)",
    re.IGNORECASE,
)


@dataclass
class Advice:
    """Validated (or rejected) advisor output. ``data`` is only set when
    ``status`` is :attr:`AdviceStatus.OK`."""

    status: AdviceStatus
    data: dict[str, Any] | None = None
    error: str | None = None
    raw_excerpt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == AdviceStatus.OK


SchemaT = TypeVar("SchemaT", bound=BaseModel)


def is_refusal(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return bool(_REFUSAL_RE.search(stripped))


def parse_response(
    text: str,
    *,
    schema: type[SchemaT] | None = None,
    prose: bool = False,
    max_age_seconds: float | None = None,
    data_timestamp: datetime | None = None,
    now: datetime | None = None,
) -> Advice:
    """Validate raw provider output.

    ``schema`` is a pydantic model describing the expected structured
    output; ``None`` accepts any parseable JSON object. ``prose=True``
    accepts free text instead (validated for refusal/staleness only) and
    returns ``data={"text": ...}`` — used by narrative consumers such as
    the morning briefing. ``max_age_seconds``/``data_timestamp`` enforce
    freshness of the underlying data.
    """
    if is_refusal(text):
        return Advice(status=AdviceStatus.REFUSED, raw_excerpt=_excerpt(text))

    if max_age_seconds is not None and data_timestamp is not None:
        check_at = now or datetime.now(data_timestamp.tzinfo)
        age = (check_at - data_timestamp).total_seconds()
        if age > max_age_seconds:
            return Advice(
                status=AdviceStatus.STALE,
                error=f"data is {age:.0f}s old (max {max_age_seconds:.0f}s)",
            )

    if prose:
        return Advice(status=AdviceStatus.OK, data={"text": text.strip()})

    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return Advice(
            status=AdviceStatus.INVALID_JSON,
            error=str(exc),
            raw_excerpt=_excerpt(text),
        )
    if not isinstance(parsed, dict):
        return Advice(
            status=AdviceStatus.SCHEMA_INVALID,
            error="expected a JSON object at the top level",
            raw_excerpt=_excerpt(text),
        )

    if schema is not None:
        try:
            model = schema.model_validate(parsed)
        except ValidationError as exc:
            return Advice(
                status=AdviceStatus.SCHEMA_INVALID,
                error=str(exc.errors(include_url=False)),
                raw_excerpt=_excerpt(text),
            )
        return Advice(status=AdviceStatus.OK, data=model.model_dump())

    return Advice(status=AdviceStatus.OK, data=parsed)


def _excerpt(text: str, limit: int = 160) -> str:
    squashed = " ".join(text.split())
    return squashed[:limit]
