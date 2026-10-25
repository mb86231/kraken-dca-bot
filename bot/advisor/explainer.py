"""Alert explainer (story A3).

When an alert is created it is delivered immediately — this module only
*adds* an asynchronous AI-generated explanation, linked to the original
alert by its stable ID. The alert's severity, trigger, and facts are never
modified.

Guarantees:
- One explanation attempt per alert (persisted status prevents retry
  storms and concurrent duplicates).
- No explanation when the alert is already older than the TTL when the
  background task runs — stale context is dropped, not sent.
- Dashboard renders the explanation as plain text; Telegram receives it
  as a plain-text follow-up (the notifier sends without parse_mode, so no
  markup interpretation can occur).
- Any advisor failure leaves the alert untouched (status "failed").
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bot.advisor.service import Advisor
from bot.utils import atomic_write_json, safe_load_json, utc_now

logger = logging.getLogger("dca_bot.advisor.explainer")

PROMPT_VERSION = "explainer/v1"
# An explanation for an alert older than this is no longer useful.
DEFAULT_TTL_SECONDS = 30 * 60
# Hard cap on the explanation length stored/delivered.
MAX_EXPLANATION_LENGTH = 500

SYSTEM_PROMPT = (
    "You explain alerts from a Kraken DCA bot in plain language. You "
    "receive the alert's facts as JSON. Rules: explain what happened and "
    "what the operator should check, use only the provided facts, never "
    "invent numbers, no financial advice, under 80 words, plain text."
)


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def truncate(text: str, limit: int = MAX_EXPLANATION_LENGTH) -> str:
    squashed = " ".join(text.split())
    if len(squashed) <= limit:
        return squashed
    return squashed[: limit - 1].rstrip() + "…"


def build_alert_prompt(alert: dict[str, Any], now: datetime) -> str:
    """Facts payload for the model — the alert record only, nothing else."""
    created_at = parse_utc(alert.get("timestamp"))
    age_seconds = int((now - created_at).total_seconds()) if created_at else None
    return (
        "Explain this alert:\n"
        + repr(
            {
                "alert_id": alert.get("id"),
                "severity": alert.get("severity"),
                "source": alert.get("source"),
                "message": alert.get("message"),
                "created_at": alert.get("timestamp"),
                "age_seconds_at_request": age_seconds,
            }
        )
    )


def build_telegram_followup(alert: dict[str, Any], explanation: str) -> str:
    """Plain-text follow-up; the notifier sends without parse_mode."""
    return (
        f"AI explanation for alert {alert.get('id')} "
        f"[{alert.get('severity')}]: {alert.get('message')}\n\n"
        f"{explanation}\n\n"
        "(AI-generated; original alert unchanged.)"
    )


class AlertExplainer:
    """Generates and stores one explanation per alert."""

    def __init__(
        self,
        advisor: Advisor,
        notifier: Any = None,
        *,
        alerts_path: str | Path = "data/alerts.json",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.advisor = advisor
        self.notifier = notifier
        self.alerts_path = Path(alerts_path)
        self.ttl_seconds = ttl_seconds
        self._now = now_provider or utc_now
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Entry point: called (synchronously) from the alert pipeline; the
    # actual work runs on the advisor's bounded background executor.
    # ------------------------------------------------------------------

    def request_explanation(self, alert: dict[str, Any]) -> None:
        alert_id = alert.get("id")
        if not alert_id:
            return
        future = self.advisor.submit("explainer", lambda: self.process(alert_id))
        if future is not None:
            future.add_done_callback(self._log_unexpected_failure)

    @staticmethod
    def _log_unexpected_failure(future: Any) -> None:
        try:
            future.result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Alert explanation task crashed: %s", exc)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        data = safe_load_json(self.alerts_path)
        return data if isinstance(data, list) else []

    def _save(self, alerts: list[dict[str, Any]]) -> None:
        atomic_write_json(self.alerts_path, alerts[-200:], indent=2)

    def _update_alert(self, alert_id: str, **fields: Any) -> list[dict[str, Any]] | None:
        """Merge fields into the alert record and persist. Returns the
        alerts list on success, None when the alert vanished."""
        alerts = self._load()
        for alert in alerts:
            if alert.get("id") == alert_id:
                alert.update(fields)
                self._save(alerts)
                return alerts
        return None

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def process(self, alert_id: str) -> bool:
        """Generate the explanation for one alert. Never raises."""
        with self._lock:
            if alert_id in self._inflight:
                return False
            self._inflight.add(alert_id)
        try:
            return self._process_inner(alert_id)
        finally:
            with self._lock:
                self._inflight.discard(alert_id)

    def _process_inner(self, alert_id: str) -> bool:
        if not self.advisor.feature_enabled("explainer"):
            return False

        alerts = self._load()
        alert = next((a for a in alerts if a.get("id") == alert_id), None)
        if alert is None:
            return False
        # One attempt per alert, ever. No retry storm.
        if alert.get("ai_status") in ("pending", "done", "failed", "stale"):
            return False

        now = self._now()
        created_at = parse_utc(alert.get("timestamp"))
        if created_at is not None and (now - created_at).total_seconds() > self.ttl_seconds:
            self._update_alert(alert_id, ai_status="stale")
            logger.info("Alert %s too old for explanation; dropped", alert_id)
            return False

        # Claim the alert before the (slow) provider call.
        if self._update_alert(alert_id, ai_status="pending") is None:
            return False

        advice = self.advisor.ask(
            "explainer",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_alert_prompt(alert, now),
            prose=True,
            prompt_version=PROMPT_VERSION,
            data_timestamp=created_at,
            max_age_seconds=self.ttl_seconds,
        )
        if not advice.ok or not advice.data or not advice.data.get("text"):
            self._update_alert(
                alert_id,
                ai_status="failed",
                ai_error=advice.error or advice.status.value,
            )
            logger.info("Alert %s explanation failed: %s", alert_id, advice.error)
            return False

        text = truncate(advice.data["text"])
        if self._update_alert(
            alert_id,
            ai_status="done",
            ai_error=None,
            ai_explanation={
                "text": text,
                "generated_at": utc_now().isoformat(),
                "source": "ai",
                "prompt_version": PROMPT_VERSION,
            },
        ) is None:
            return False

        if self.notifier is not None:
            try:
                self.notifier.send(build_telegram_followup(alert, text))
            except Exception as exc:
                # The dashboard copy is the source of truth; a Telegram
                # failure must not flip the explanation back to failed.
                logger.warning("Explanation follow-up send failed: %s", exc)
        return True
