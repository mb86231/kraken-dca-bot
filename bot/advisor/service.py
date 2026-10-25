"""Advisor orchestration: the only entry point consumers may use.

Failure semantics (normative for the whole epic):

1. When the advisor is off, failing, or rate-limited, the base strategy
   runs unchanged — no advisor code path is on the buy path.
2. AI outages are visible: tracked in ``status()`` for the dashboard and
   logged at warning level (bounded, not per call).
3. Every failure degrades to "no advice"; there is no default
   recommendation on failure.
"""

from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from bot.advisor.audit import AdvisorAudit, InteractionRecord, hash_prompt
from bot.advisor.limits import BoundedExecutor, CircuitBreaker, DailyBudget
from bot.advisor.provider import (
    ProviderClient,
    ProviderError,
    create_provider,
)
from bot.advisor.validate import Advice, AdviceStatus, parse_response
from bot.secrets_store import SecretsStore
from bot.utils import utc_now

if TYPE_CHECKING:
    from pydantic import BaseModel

    from bot.config import Config

logger = logging.getLogger("dca_bot.advisor")

# Per-feature switches. A1 ships the foundation only; briefing (A2) and
# explainer (A3) flags exist now so configuration is stable across stories.
FEATURES = ("briefing", "explainer")

# Environment variable that forces the advisor off regardless of config —
# the global kill switch. Any value in ("false", "0", "off") disables.
KILL_SWITCH_ENV = "AI_ADVISOR_ENABLED"


def resolve_api_key() -> str:
    """Provider API key: environment variable first, secrets store fallback.

    Same precedence rule as every other dashboard-managed secret; the raw key
    is never logged or exposed via status endpoints (see SecretsStore).
    """
    env_key = os.environ.get("AI_ADVISOR_API_KEY", "").strip()
    if env_key:
        return env_key
    stored = SecretsStore().resolve("advisor").get("api_key")
    return stored.strip() if isinstance(stored, str) and stored.strip() else ""


@dataclass
class AdvisorSettings:
    """Resolved advisor configuration (never contains secrets)."""

    enabled: bool = False
    features: dict[str, bool] = field(default_factory=lambda: {f: False for f in FEATURES})
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    timeout_seconds: float = 30.0
    max_retries: int = 2
    max_parallel: int = 2
    daily_request_limit: int = 50
    daily_token_limit: int = 100_000
    daily_cost_limit: float = 1.0
    briefing_time: str = "07:00"
    temperature: float = 0.2
    max_tokens: int = 1024  # reasoning models burn budget on thinking; raise if empty replies
    api_key: str = ""  # env-sourced only, never persisted


def kill_switch_active() -> bool:
    """True when the environment forces the advisor off."""
    value = os.environ.get(KILL_SWITCH_ENV)
    if value is None:
        return False
    return value.strip().lower() in ("false", "0", "off", "no")


class Advisor:
    """Coordinates provider, limits, validation, and audit.

    ``ask`` never raises for provider/validation problems; it returns an
    :class:`Advice` whose status explains the outcome.
    """

    def __init__(
        self,
        settings: AdvisorSettings,
        provider: ProviderClient | None = None,
        audit: AdvisorAudit | None = None,
        budgets: DailyBudget | None = None,
        breaker: CircuitBreaker | None = None,
        executor: BoundedExecutor | None = None,
        disabled_reason: str | None = None,
        kill_switch: bool = False,
    ):
        self.settings = settings
        self.provider = provider
        self.audit = audit or AdvisorAudit()
        self.budgets = budgets or DailyBudget(
            request_limit=settings.daily_request_limit,
            token_limit=settings.daily_token_limit,
            cost_limit=settings.daily_cost_limit,
        )
        self.breaker = breaker or CircuitBreaker()
        self.executor = executor or BoundedExecutor(max_workers=settings.max_parallel)
        self.disabled_reason = disabled_reason
        self.kill_switch = kill_switch
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.consecutive_failures = 0

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: "Config") -> "Advisor":
        """Build an advisor from bot config. Never raises: any configuration
        or provider problem yields a disabled advisor with a reason."""
        settings = AdvisorSettings(
            enabled=config.advisor_enabled,
            features=dict(config.advisor_features),
            provider=config.advisor_provider,
            base_url=config.advisor_base_url,
            model=config.advisor_model,
            timeout_seconds=config.advisor_timeout_seconds,
            max_retries=config.advisor_max_retries,
            max_parallel=config.advisor_max_parallel,
            daily_request_limit=config.advisor_daily_request_limit,
            daily_token_limit=config.advisor_daily_token_limit,
            daily_cost_limit=config.advisor_daily_cost_limit,
            briefing_time=config.advisor_briefing_time,
            temperature=config.advisor_temperature,
            max_tokens=config.advisor_max_tokens,
            api_key=resolve_api_key(),
        )
        if kill_switch_active():
            settings.enabled = False
            return cls(
                settings,
                disabled_reason=f"{KILL_SWITCH_ENV}=false (kill switch)",
                kill_switch=True,
            )
        if not settings.enabled:
            return cls(
                settings,
                disabled_reason=(
                    "AI Advisor master toggle is off "
                    "(enable it in Settings → AI Advisor)"
                ),
            )
        if settings.provider not in ("openai", "ollama"):
            return cls(settings, disabled_reason=f"unknown provider {settings.provider!r}")
        if not settings.model:
            return cls(settings, disabled_reason="no advisor model configured")
        try:
            provider = create_provider(
                settings.provider,
                base_url=settings.base_url,
                api_key=settings.api_key,
                model=settings.model,
                timeout=settings.timeout_seconds,
                max_retries=settings.max_retries,
                temperature=settings.temperature,
            )
        except ValueError as exc:
            logger.warning("Advisor disabled: %s", exc)
            return cls(settings, disabled_reason=str(exc))
        return cls(settings, provider=provider)

    @classmethod
    def disabled_stub(cls, reason: str = "not initialized") -> "Advisor":
        return cls(AdvisorSettings(enabled=False), disabled_reason=reason)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self.provider is not None

    def feature_enabled(self, feature: str) -> bool:
        return self.enabled and self.settings.features.get(feature, False)

    def feature_configured(self, feature: str) -> bool:
        """Feature switch state, independent of the AI master toggle.

        Used by outputs that also run deterministically without the AI
        layer (the morning briefing). The global kill switch still
        disables everything."""
        return not self.kill_switch and bool(self.settings.features.get(feature, False))

    def _track_error(self, message: str) -> None:
        self.last_error = message
        self.last_error_at = utc_now()
        self.consecutive_failures += 1

    def status(self) -> dict[str, Any]:
        """Dashboard-facing advisor state."""
        if not self.enabled:
            return {
                "enabled": False,
                "available": False,
                "state": "disabled",
                "reason": self.disabled_reason,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            }
        available = (
            self.breaker.allow()
            and self.budgets.exhausted_reason() is None
            and self.consecutive_failures == 0
        )
        state = "ok" if available else ("degraded" if self.breaker.allow() else "unavailable")
        return {
            "enabled": True,
            "available": available,
            "state": state,
            "provider": self.settings.provider,
            "model": self.settings.model,
            "circuit_open": self.breaker.is_open,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "budgets": self.budgets.snapshot(),
        }

    # ------------------------------------------------------------------
    # Core request path
    # ------------------------------------------------------------------

    def ask(
        self,
        feature: str,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: "type[BaseModel] | None" = None,
        prose: bool = False,
        prompt_version: str = "unversioned",
        schema_version: str | None = None,
        data_timestamp: datetime | None = None,
        max_age_seconds: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> Advice:
        """Execute one advisory request with limits, validation, and audit.

        Returns ``Advice``; only ``AdviceStatus.OK`` carries data. Any
        failure (disabled, budget, circuit, provider, validation) returns a
        non-OK advice and is audit-logged. ``max_tokens=None`` uses the
        configured ``advisor.max_tokens``. ``timeout_seconds`` and
        ``max_retries`` override the provider defaults for this call only
        (latency-sensitive paths like dashboard test sends)."""
        max_tokens = self.settings.max_tokens if max_tokens is None else max_tokens
        if not self.enabled:
            return Advice(status=AdviceStatus.DISABLED, error=self.disabled_reason)
        if not self.settings.features.get(feature, False):
            return Advice(
                status=AdviceStatus.DISABLED,
                error=f"feature {feature!r} is not enabled",
            )
        if not self.breaker.allow():
            self._track_error("circuit breaker open")
            return Advice(status=AdviceStatus.DISABLED, error="advisor circuit breaker open")
        reason = self.budgets.exhausted_reason()
        if reason is not None:
            self._track_error(reason)
            return Advice(status=AdviceStatus.DISABLED, error=reason)
        if self.provider is None:  # pragma: no cover - guarded by enabled
            return Advice(status=AdviceStatus.DISABLED, error=self.disabled_reason)

        interaction_id = uuid.uuid4().hex
        try:
            result = self.provider.complete(
                system_prompt,
                user_prompt,
                max_tokens=max_tokens,
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
        except ProviderError as exc:
            self.breaker.on_failure()
            self._track_error(f"{exc.category}: {exc}")
            self.audit.record(InteractionRecord(
                interaction_id=interaction_id,
                feature=feature,
                provider=self.settings.provider,
                model=self.settings.model,
                prompt_version=prompt_version,
                schema_version=schema_version,
                data_timestamp=data_timestamp.isoformat() if data_timestamp else None,
                status="error",
                latency_ms=None,
                tokens_used=None,
                cost_estimate=None,
                error_category=exc.category,
                prompt_sha256=hash_prompt(system_prompt, user_prompt),
            ))
            return Advice(status=AdviceStatus.DISABLED, error=f"provider {exc.category}: {exc}")

        self.breaker.on_success()
        self.consecutive_failures = 0
        self.budgets.record(tokens=result.tokens_used, cost=result.cost_estimate)
        advice = parse_response(
            result.text,
            schema=schema,
            prose=prose,
            max_age_seconds=max_age_seconds,
            data_timestamp=data_timestamp,
        )
        if advice.ok:
            self.last_error = None
        else:
            self._track_error(advice.error or advice.status.value)
        self.audit.record(InteractionRecord(
            interaction_id=interaction_id,
            feature=feature,
            provider=self.settings.provider,
            model=result.model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            data_timestamp=data_timestamp.isoformat() if data_timestamp else None,
            status=advice.status.value,
            latency_ms=round(result.latency_ms, 1),
            tokens_used=result.tokens_used,
            cost_estimate=result.cost_estimate,
            error_category=None if advice.ok else advice.status.value,
            prompt_sha256=hash_prompt(system_prompt, user_prompt),
        ))
        return advice

    def submit(self, feature: str, fn: Callable[[], Any]) -> Future[Any] | None:
        """Run ``fn`` in the background if the feature is enabled.

        Returns a Future, or None when the feature is off (callers can skip
        bookkeeping). Exceptions inside ``fn`` stay in the Future."""
        if not self.feature_enabled(feature):
            return None
        return self.executor.submit(fn)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False)
