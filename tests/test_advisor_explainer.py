"""Tests for the alert explainer (story A3).

Covers: original alert delivered independently, one explanation attempt
per alert, stale alerts dropped, provider/telegram failure behaviour,
length capping, and the create_alert hook contract.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import bot.alerts as alerts_module
from bot.advisor.explainer import (
    MAX_EXPLANATION_LENGTH,
    AlertExplainer,
    build_alert_prompt,
    build_telegram_followup,
    parse_utc,
    truncate,
)
from bot.advisor.provider import OpenAICompatibleClient
from bot.advisor.service import Advisor, AdvisorSettings


class _FakeNotifier:
    def __init__(self, fail: bool = False):
        self.messages: list[str] = []
        self._fail = fail

    def send(self, message: str) -> bool:
        if self._fail:
            raise ConnectionError("telegram down")
        self.messages.append(message)
        return True


class _CountingHandler:
    def __init__(self, content: str):
        self.calls = 0
        self._content = content

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": self._content}}
                ],
                "usage": {"total_tokens": 15},
            },
        )


def _advisor(handler: Any, *, features: dict[str, bool] | None = None) -> Advisor:
    settings = AdvisorSettings(
        enabled=True,
        provider="openai",
        base_url="https://example.test/v1",
        model="test-model",
        api_key="test-key",
        features=features or {"briefing": False, "explainer": True},
    )
    provider = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        timeout=5.0,
        max_retries=0,
    )
    provider._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0)
    )
    return Advisor(settings, provider=provider)


def _write_alert(
    path: Path,
    alert_id: str = "alert-deadbeef",
    *,
    age_seconds: float = 60.0,
) -> dict[str, Any]:
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    alert = {
        "id": alert_id,
        "timestamp": created.isoformat(),
        "message": "Price data is stale",
        "severity": "warning",
        "source": "bot",
        "acknowledged": False,
    }
    path.write_text(__import__("json").dumps([alert]))
    return alert


def _load(path: Path) -> list[dict[str, Any]]:
    import json

    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_parse_utc_handles_naive_and_aware():
    assert parse_utc(None) is None
    assert parse_utc("not-a-date") is None
    naive = parse_utc("2026-10-10T07:00:00")
    assert naive is not None and naive.tzinfo is not None
    aware = parse_utc("2026-10-10T07:00:00+02:00")
    assert aware is not None and aware.utcoffset() == timedelta(hours=2)


def test_truncate_caps_length():
    long_text = "word " * 300
    assert len(truncate(long_text)) <= MAX_EXPLANATION_LENGTH
    assert truncate("short").endswith("short")


def test_build_prompt_contains_facts_only():
    now = datetime.now(timezone.utc)
    alert = {"id": "a1", "severity": "error", "source": "bot", "message": "boom",
             "timestamp": now.isoformat()}
    prompt = build_alert_prompt(alert, now)
    assert "boom" in prompt
    assert "a1" in prompt


def test_telegram_followup_format():
    msg = build_telegram_followup(
        {"id": "a1", "severity": "warning", "message": "stale price"},
        "The bot has not seen fresh prices.",
    )
    assert "a1" in msg and "AI-generated" in msg
    assert "stale price" in msg


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_explanation_generated_and_alert_unchanged(tmp_path: Path):
    path = tmp_path / "alerts.json"
    original = _write_alert(path)
    handler = _CountingHandler("The bot has not received fresh price data. Check the exchange connection.")
    advisor = _advisor(handler)
    notifier = _FakeNotifier()
    explainer = AlertExplainer(advisor, notifier, alerts_path=path)

    assert explainer.process("alert-deadbeef") is True

    stored = _load(path)[0]
    assert stored["ai_status"] == "done"
    assert stored["ai_explanation"]["source"] == "ai"
    assert "fresh price data" in stored["ai_explanation"]["text"]
    # Original alert facts untouched.
    for key in ("id", "timestamp", "message", "severity", "source"):
        assert stored[key] == original[key]
    # Telegram follow-up delivered, referencing the alert.
    assert len(notifier.messages) == 1
    assert "alert-deadbeef" in notifier.messages[0]
    advisor.executor.shutdown(wait=True)


def test_one_attempt_per_alert_no_retry_storm(tmp_path: Path):
    path = tmp_path / "alerts.json"
    _write_alert(path)
    handler = _CountingHandler("first explanation")
    advisor = _advisor(handler)
    explainer = AlertExplainer(advisor, _FakeNotifier(), alerts_path=path)

    assert explainer.process("alert-deadbeef") is True
    assert explainer.process("alert-deadbeef") is False  # already done
    assert handler.calls == 1
    advisor.executor.shutdown(wait=True)


def test_failed_provider_marks_failed_without_retry(tmp_path: Path):
    path = tmp_path / "alerts.json"
    _write_alert(path)

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    advisor = _advisor(failing)
    explainer = AlertExplainer(advisor, _FakeNotifier(), alerts_path=path)

    assert explainer.process("alert-deadbeef") is False

    stored = _load(path)[0]
    assert stored["ai_status"] == "failed"
    assert stored["ai_error"]
    assert "ai_explanation" not in stored
    # A later run must not retry the same alert.
    assert explainer.process("alert-deadbeef") is False
    advisor.executor.shutdown(wait=True)


def test_stale_alert_is_dropped_without_ai_call(tmp_path: Path):
    path = tmp_path / "alerts.json"
    _write_alert(path, age_seconds=3600)  # older than the 30-min TTL
    handler = _CountingHandler("too late")
    advisor = _advisor(handler)
    notifier = _FakeNotifier()
    explainer = AlertExplainer(advisor, notifier, alerts_path=path)

    assert explainer.process("alert-deadbeef") is False

    stored = _load(path)[0]
    assert stored["ai_status"] == "stale"
    assert handler.calls == 0
    assert notifier.messages == []
    advisor.executor.shutdown(wait=True)


def test_missing_alert_is_a_noop(tmp_path: Path):
    path = tmp_path / "alerts.json"
    path.write_text("[]")
    handler = _CountingHandler("nothing")
    advisor = _advisor(handler)
    explainer = AlertExplainer(advisor, _FakeNotifier(), alerts_path=path)

    assert explainer.process("alert-nope") is False
    assert handler.calls == 0
    advisor.executor.shutdown(wait=True)


def test_feature_disabled_leaves_alert_untouched(tmp_path: Path):
    path = tmp_path / "alerts.json"
    _write_alert(path)
    handler = _CountingHandler("nope")
    advisor = _advisor(handler, features={"briefing": False, "explainer": False})
    explainer = AlertExplainer(advisor, _FakeNotifier(), alerts_path=path)

    assert explainer.process("alert-deadbeef") is False
    assert handler.calls == 0
    assert "ai_status" not in _load(path)[0]
    advisor.executor.shutdown(wait=True)


def test_explanation_length_capped(tmp_path: Path):
    path = tmp_path / "alerts.json"
    _write_alert(path)
    long_text = "Very long explanation. " * 80
    handler = _CountingHandler(long_text)
    advisor = _advisor(handler)
    explainer = AlertExplainer(advisor, _FakeNotifier(), alerts_path=path)

    assert explainer.process("alert-deadbeef") is True
    text = _load(path)[0]["ai_explanation"]["text"]
    assert len(text) <= MAX_EXPLANATION_LENGTH
    advisor.executor.shutdown(wait=True)


def test_telegram_failure_keeps_dashboard_explanation(tmp_path: Path):
    path = tmp_path / "alerts.json"
    _write_alert(path)
    handler = _CountingHandler("Useful explanation.")
    advisor = _advisor(handler)
    explainer = AlertExplainer(advisor, _FakeNotifier(fail=True), alerts_path=path)

    assert explainer.process("alert-deadbeef") is True
    stored = _load(path)[0]
    assert stored["ai_status"] == "done"
    assert stored["ai_explanation"]["text"] == "Useful explanation."
    advisor.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# create_alert hook contract
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(alerts_module, "ALERTS_PATH", tmp_path / "alerts.json")
    yield tmp_path / "alerts.json"
    alerts_module.set_explainer_hook(None)


def test_original_alert_delivered_without_explainer(isolated_alerts: Path):
    """With no hook registered the alert is complete and immediate."""
    alert = alerts_module.create_alert("Bot error: something", "error")
    assert alert["message"] == "Bot error: something"
    assert "ai_status" not in alert


def test_hook_exception_never_breaks_alert_delivery(isolated_alerts: Path):
    def bad_hook(alert: dict[str, Any]) -> None:
        raise RuntimeError("hook exploded")

    alerts_module.set_explainer_hook(bad_hook)
    alert = alerts_module.create_alert("Price data is stale", "warning")
    assert alert["severity"] == "warning"
    stored = alerts_module.load_alerts()
    assert stored[-1]["id"] == alert["id"]


def test_hook_triggers_async_explanation(isolated_alerts: Path):
    handler = _CountingHandler("Check the exchange connectivity.")
    advisor = _advisor(handler)
    notifier = _FakeNotifier()
    explainer = AlertExplainer(advisor, notifier, alerts_path=isolated_alerts)
    alerts_module.set_explainer_hook(explainer.request_explanation)

    alert = alerts_module.create_alert("Price data is stale", "warning")
    # The alert exists immediately, without waiting for the AI.
    fresh = [a for a in alerts_module.load_alerts() if a["id"] == alert["id"]][0]
    assert "ai_explanation" not in fresh

    deadline = time.time() + 5
    explained = None
    while time.time() < deadline:
        stored = [a for a in alerts_module.load_alerts() if a["id"] == alert["id"]][0]
        if stored.get("ai_status") == "done":
            explained = stored
            break
        time.sleep(0.05)
    assert explained is not None, "explanation was not generated in time"
    assert "exchange connectivity" in explained["ai_explanation"]["text"]
    assert len(notifier.messages) == 1
    advisor.executor.shutdown(wait=True)
