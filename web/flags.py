"""Shared parsing of boolean environment flags.

One truth table for the whole application so that validation, cookie setters
and preflight checks can never disagree (e.g. accepting ``1``/``yes`` at
validation time while the cookie setter only recognises literal ``true``).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("dca_bot.web.flags")

TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def parse_bool(raw: str | None) -> bool | None:
    """Parse a boolean string with the shared truth table.

    Returns ``None`` for unset/empty input and raises ``ValueError`` for
    unrecognised values so misconfiguration fails loudly at validation
    boundaries instead of silently disabling a security feature.
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValueError(f"unrecognised boolean value {raw!r}")


def env_flag(name: str, *, default: bool = False) -> bool:
    """Return a boolean environment flag.

    Unset/empty returns ``default``. Unrecognised values log a warning and
    fall back to ``default`` — runtime code must stay available, while
    ``validate_production_security`` rejects the same garbage at startup.
    """
    try:
        parsed = parse_bool(os.environ.get(name))
    except ValueError:
        logger.warning(
            "Unrecognised boolean value for %s: %r — treating as %s. "
            "Use one of true/1/yes/on or false/0/no/off.",
            name,
            os.environ.get(name),
            default,
        )
        return default
    return default if parsed is None else parsed
