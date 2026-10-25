"""AI advisor package (story A1 — foundation).

Advisory layer for the DCA bot: provider abstraction, local validation,
limits, audit. Off by default; the buy path never depends on it.

Public API:

- :class:`~bot.advisor.service.Advisor` — the orchestrator consumers use.
- :class:`~bot.advisor.service.AdvisorSettings` — resolved configuration.
- :class:`~bot.advisor.validate.Advice` / :class:`AdviceStatus` — outcomes.
- :class:`~bot.advisor.provider.ProviderClient` — provider protocol.
"""

from bot.advisor.provider import (
    OllamaClient,
    OpenAICompatibleClient,
    ProviderClient,
    ProviderError,
    ProviderResult,
)
from bot.advisor.service import (
    KILL_SWITCH_ENV,
    Advisor,
    AdvisorSettings,
    kill_switch_active,
)
from bot.advisor.snapshot import build_state_snapshot
from bot.advisor.validate import Advice, AdviceStatus, parse_response

__all__ = [
    "KILL_SWITCH_ENV",
    "Advice",
    "AdviceStatus",
    "Advisor",
    "AdvisorSettings",
    "OllamaClient",
    "OpenAICompatibleClient",
    "ProviderClient",
    "ProviderError",
    "ProviderResult",
    "build_state_snapshot",
    "kill_switch_active",
    "parse_response",
]
