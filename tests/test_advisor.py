"""Tests for the AI advisor foundation (story A1).

Covers the provider clients (mocked HTTP), local validation, limits and
circuit breaker, audit records, config integration, the kill switch, and
the guarantee that a disabled advisor is unreachable and never affects
the buy path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from bot.advisor import (
    AdviceStatus,
    Advisor,
    AdvisorSettings,
    build_state_snapshot,
    kill_switch_active,
    parse_response,
)
from bot.advisor.audit import AdvisorAudit, InteractionRecord, hash_prompt
from bot.advisor.limits import BoundedExecutor, CircuitBreaker, DailyBudget
from bot.advisor.provider import (
    OllamaClient,
    OpenAICompatibleClient,
    ProviderError,
    create_provider,
)
from bot.config import Config
from bot.state import BotState
from bot.store import TransactionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_payload(content: str, tokens: int = 12) -> dict[str, Any]:
    return {
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": tokens},
    }


def _client_with(handler: Any, **kwargs: Any) -> OpenAICompatibleClient:
    kwargs.setdefault("max_retries", 0)
    client = OpenAICompatibleClient(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        timeout=5.0,
        **kwargs,
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0)
    )
    return client


class _OkSchema(BaseModel, extra="forbid"):
    score: float
    label: str


# ---------------------------------------------------------------------------
# Provider clients
# ---------------------------------------------------------------------------


def test_openai_client_success_requests_json_mode():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, json=_openai_payload('{"ok": true}'))

    client = _client_with(handler)
    result = client.complete("system", "user")

    assert result.text == '{"ok": true}'
    assert result.tokens_used == 12
    assert result.model == "test-model"
    assert result.latency_ms >= 0
    body = json.loads(requests_seen[0].read())
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 512


def test_openai_client_sends_configured_temperature():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, json=_openai_payload('{"ok": true}'))

    client = _client_with(handler, temperature=1.0)
    client.complete("system", "user")

    body = json.loads(requests_seen[0].read())
    assert body["temperature"] == 1.0


def test_openai_client_default_temperature_is_0_2():
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, json=_openai_payload('{"ok": true}'))

    client = _client_with(handler)
    client.complete("system", "user")

    body = json.loads(requests_seen[0].read())
    assert body["temperature"] == 0.2


def test_openai_client_malformed_payload_is_invalid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = _client_with(handler)
    with pytest.raises(ProviderError) as excinfo:
        client.complete("system", "user")
    assert excinfo.value.category == "invalid_response"


def test_openai_client_empty_content_is_invalid_response_with_hint():
    """Reasoning models can exhaust max_tokens on internal reasoning and
    return empty content; that must surface as a clear error, not look
    like a refusal."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"total_tokens": 300},
            },
        )

    client = _client_with(handler)
    with pytest.raises(ProviderError) as excinfo:
        client.complete("system", "user")
    assert excinfo.value.category == "invalid_response"
    assert "empty content" in str(excinfo.value)
    assert "length" in str(excinfo.value)


def test_openai_client_429_is_rate_limited_without_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "slow down"})

    client = _client_with(handler, max_retries=3)
    with pytest.raises(ProviderError) as excinfo:
        client.complete("system", "user")
    assert excinfo.value.category == "rate_limited"
    assert calls == 1  # fail fast, no retry


def test_openai_client_retries_transient_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_openai_payload('{"ok": true}'))

    monkeypatch.setattr("bot.advisor.provider.time.sleep", lambda _s: None)
    client = _client_with(handler, max_retries=2)
    result = client.complete("system", "user")
    assert result.text == '{"ok": true}'
    assert calls == 2


def test_openai_client_timeout_category(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom", request=request)

    client = _client_with(handler)
    with pytest.raises(ProviderError) as excinfo:
        client.complete("system", "user")
    assert excinfo.value.category == "timeout"


def test_ollama_client_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        return httpx.Response(200, json={"response": '{"ok": true}', "eval_count": 42})

    client = OllamaClient(base_url="http://localhost:11434", model="llama3.2")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    result = client.complete("system", "user")
    assert result.text == '{"ok": true}'
    assert result.tokens_used == 42


def test_ollama_client_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    client = OllamaClient(base_url="http://localhost:11434", model="llama3.2")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as excinfo:
        client.complete("system", "user")
    assert excinfo.value.category == "http"


def test_create_provider_requires_key_for_openai():
    with pytest.raises(ValueError):
        create_provider(
            "openai", base_url="", api_key="", model="m", timeout=5, max_retries=0
        )


def test_create_provider_rejects_unknown():
    with pytest.raises(ValueError):
        create_provider(
            "magic", base_url="x", api_key="k", model="m", timeout=5, max_retries=0
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_parse_response_plain_json_without_schema():
    advice = parse_response('{"a": 1}')
    assert advice.ok
    assert advice.data == {"a": 1}


def test_parse_response_with_schema_ok():
    advice = parse_response('{"score": 0.5, "label": "calm"}', schema=_OkSchema)
    assert advice.ok
    assert advice.data == {"score": 0.5, "label": "calm"}


def test_parse_response_schema_violations():
    missing = parse_response('{"score": 0.5}', schema=_OkSchema)
    assert missing.status == AdviceStatus.SCHEMA_INVALID

    unknown = parse_response(
        '{"score": 0.5, "label": "calm", "extra": true}', schema=_OkSchema
    )
    assert unknown.status == AdviceStatus.SCHEMA_INVALID


def test_parse_response_invalid_json():
    advice = parse_response("not json at all")
    assert advice.status == AdviceStatus.INVALID_JSON


@pytest.mark.parametrize("text", ["", "   ", "I'm sorry, but I cannot assist with that."])
def test_parse_response_refusals(text: str):
    advice = parse_response(text)
    assert advice.status == AdviceStatus.REFUSED


@pytest.mark.parametrize(
    "text",
    [
        # Benign AI preambles that must NOT be read as a decline.
        "As an AI language model, I have written the morning briefing from "
        "the provided snapshot. Price moved within the expected range.",
        "As an AI model, I rely solely on the snapshot values provided. "
        "Budget spent so far is within plan.",
        "I'm sorry, but the price dipped sharply today; the tier logic "
        "handled it as designed.",
    ],
)
def test_parse_response_preamble_is_not_refusal(text: str):
    advice = parse_response(text, prose=True)
    assert advice.ok
    assert advice.data is not None and advice.data["text"]


def test_parse_response_stale_data():
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    advice = parse_response(
        '{"a": 1}', max_age_seconds=3600, data_timestamp=old
    )
    assert advice.status == AdviceStatus.STALE


def test_parse_response_fresh_data_passes():
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    advice = parse_response(
        '{"a": 1}', max_age_seconds=3600, data_timestamp=recent
    )
    assert advice.ok


# ---------------------------------------------------------------------------
# Limits and circuit breaker
# ---------------------------------------------------------------------------


def test_daily_budget_blocks_after_request_limit():
    budget = DailyBudget(request_limit=3, token_limit=10**9, cost_limit=10**9)
    assert budget.allow()
    assert budget.record() is True
    assert budget.record() is True
    assert budget.record() is False  # third call exhausts the limit
    assert budget.allow() is False
    assert "request limit" in (budget.exhausted_reason() or "")


def test_daily_budget_token_limit():
    budget = DailyBudget(request_limit=100, token_limit=100, cost_limit=10**9)
    assert budget.record(tokens=150) is False
    assert "token limit" in (budget.exhausted_reason() or "")


def test_daily_budget_rolls_over_at_utc_midnight(monkeypatch: pytest.MonkeyPatch):
    budget = DailyBudget(request_limit=2, token_limit=10**9, cost_limit=10**9)
    assert budget.record() is True
    assert budget.record() is False  # second call exhausts the limit
    assert budget.allow() is False
    monkeypatch.setattr(DailyBudget, "_today", staticmethod(lambda: "2999-01-01"))
    assert budget.allow() is True


def test_circuit_breaker_opens_and_recovers():
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60)
    assert breaker.allow()
    breaker.on_failure()
    assert breaker.allow()
    breaker.on_failure()
    assert breaker.is_open
    assert not breaker.allow()
    breaker.on_success()  # half-open probe succeeded
    assert not breaker.is_open
    assert breaker.allow()


def test_bounded_executor_runs_task():
    executor = BoundedExecutor(max_workers=1)
    future = executor.submit(lambda: 21 * 2)
    assert future.result(timeout=5) == 42
    executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_record_has_required_fields_and_no_prompt(tmp_path: Path):
    audit = AdvisorAudit(log_dir=tmp_path)
    record = InteractionRecord(
        interaction_id="abc",
        feature="briefing",
        provider="openai",
        model="test-model",
        prompt_version="briefing/v1",
        schema_version="briefing-schema/v1",
        data_timestamp="2026-10-10T06:00:00+00:00",
        status="ok",
        latency_ms=123.0,
        tokens_used=12,
        cost_estimate=None,
        error_category=None,
        prompt_sha256=hash_prompt("system", "user"),
    )
    audit.record(record)

    log_file = tmp_path / "advisor.json"
    assert log_file.exists()
    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    entry = json.loads(line)
    # The message field carries the flattened record JSON.
    payload = json.loads(entry["message"].split(" ", 1)[1])
    for key in (
        "interaction_id", "provider", "model", "prompt_version",
        "data_timestamp", "status", "latency_ms", "tokens_used",
        "error_category", "prompt_sha256",
    ):
        assert key in payload
    assert payload["prompt_sha256"] != "system"
    assert "system" not in line
    assert "user" not in line


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, advisor: dict[str, Any] | None) -> Path:
    data: dict[str, Any] = {
        "trading_pair": "XBTCHF",
        "deposit_day": 24,
        "crypto_amount": 0.0001,
        "dip_threshold_percent": 5.0,
        "poll_interval_seconds": 300,
        "buy_hour": 8,
        "dip_buy_cooldown_hours": 2.0,
    }
    if advisor is not None:
        data["advisor"] = advisor
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_config_advisor_defaults_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    config = Config(config_path=_write_config(tmp_path, None))
    assert config.advisor_enabled is False
    assert config.advisor_features == {"briefing": False, "explainer": False}
    assert config.advisor_provider == "openai"


def test_config_template_includes_advisor_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    Config(config_path=tmp_path / "fresh.json")
    template = json.loads((tmp_path / "fresh.json").read_text())
    assert template["advisor"]["enabled"] is False
    assert template["advisor"]["features"] == {"briefing": False, "explainer": False}


def test_config_rejects_unknown_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    with pytest.raises(Exception, match="advisor.provider"):
        Config(config_path=_write_config(tmp_path, {"provider": "magic"}))


def test_config_requires_model_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    with pytest.raises(Exception, match="advisor.model"):
        Config(config_path=_write_config(tmp_path, {"enabled": True}))


def test_to_dict_round_trips_advisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    advisor = {
        "enabled": True,
        "features": {"briefing": True, "explainer": False},
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "model": "llama3.2",
        "daily_request_limit": 10,
    }
    config = Config(config_path=_write_config(tmp_path, advisor))
    as_dict = config.to_dict()
    assert as_dict["advisor"]["enabled"] is True
    assert as_dict["advisor"]["features"]["briefing"] is True
    assert as_dict["advisor"]["provider"] == "ollama"
    assert "api_key" not in as_dict["advisor"]


# ---------------------------------------------------------------------------
# Advisor service
# ---------------------------------------------------------------------------


def _enabled_settings(**overrides: Any) -> AdvisorSettings:
    settings = AdvisorSettings(
        enabled=True,
        provider="openai",
        base_url="https://example.test/v1",
        model="test-model",
        api_key="test-key",
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _advisor_with(handler: Any, **settings: Any) -> Advisor:
    provider = _client_with(handler)
    return Advisor(_enabled_settings(**settings), provider=provider)


def test_advisor_disabled_returns_disabled_without_provider(tmp_path: Path):
    advisor = Advisor.from_config(
        Config(config_path=_write_config(tmp_path, None))
    )
    advice = advisor.ask(
        "briefing", system_prompt="s", user_prompt="u"
    )
    assert advice.status == AdviceStatus.DISABLED
    assert advisor.enabled is False


def test_advisor_feature_flag_gates_ask():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("provider must not be called")

    advisor = _advisor_with(handler)  # features all off
    advice = advisor.ask("briefing", system_prompt="s", user_prompt="u")
    assert advice.status == AdviceStatus.DISABLED
    assert "not enabled" in (advice.error or "")


def test_advisor_success_records_budget_and_audit(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_payload('{"score": 0.1, "label": "calm"}'))

    advisor = _advisor_with(
        handler,
        features={"briefing": True, "explainer": False},
    )
    advisor.audit = AdvisorAudit(log_dir=tmp_path)
    advice = advisor.ask(
        "briefing",
        system_prompt="s",
        user_prompt="u",
        schema=_OkSchema,
        prompt_version="p/v1",
        schema_version="s/v1",
    )
    assert advice.ok
    assert advice.data == {"score": 0.1, "label": "calm"}
    snapshot = advisor.budgets.snapshot()
    assert snapshot["requests_used"] == 1
    assert advisor.status()["state"] == "ok"

    log_file = tmp_path / "advisor.json"
    payload = json.loads(json.loads(log_file.read_text().strip().splitlines()[-1])["message"].split(" ", 1)[1])
    assert payload["status"] == "ok"
    assert payload["prompt_version"] == "p/v1"
    assert payload["schema_version"] == "s/v1"
    assert payload["error_category"] is None


def test_advisor_provider_failure_degrades_to_no_advice(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    advisor = _advisor_with(
        handler,
        features={"briefing": True, "explainer": False},
        max_retries=0,
    )
    advice = advisor.ask("briefing", system_prompt="s", user_prompt="u")
    assert advice.status == AdviceStatus.DISABLED
    assert advisor.status()["available"] is False
    assert advisor.last_error is not None


def test_advisor_circuit_breaker_stops_calls(tmp_path: Path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="down")

    advisor = _advisor_with(
        handler,
        features={"briefing": True, "explainer": False},
        max_retries=0,
    )
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=3600)
    advisor.breaker = breaker
    advisor.ask("briefing", system_prompt="s", user_prompt="u")
    second = advisor.ask("briefing", system_prompt="s", user_prompt="u")
    assert second.status == AdviceStatus.DISABLED
    assert "circuit" in (second.error or "")
    assert calls == 1


def test_advisor_daily_budget_stops_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_payload('{"score": 0.1, "label": "calm"}'))

    advisor = _advisor_with(
        handler,
        features={"briefing": True, "explainer": False},
        daily_request_limit=1,
    )
    first = advisor.ask("briefing", system_prompt="s", user_prompt="u", schema=_OkSchema)
    assert first.ok
    second = advisor.ask("briefing", system_prompt="s", user_prompt="u")
    assert second.status == AdviceStatus.DISABLED
    assert "limit" in (second.error or "")


def test_kill_switch_forces_advisor_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("AI_ADVISOR_ENABLED", "false")
    assert kill_switch_active()
    config = Config(
        config_path=_write_config(
            tmp_path, {"enabled": True, "model": "m", "provider": "ollama", "base_url": "http://x"}
        )
    )
    advisor = Advisor.from_config(config)
    assert advisor.enabled is False
    assert "kill switch" in (advisor.disabled_reason or "")


def test_from_config_disabled_stub_on_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.delenv("AI_ADVISOR_API_KEY", raising=False)
    config = Config(config_path=_write_config(tmp_path, {"enabled": True, "model": "m"}))
    advisor = Advisor.from_config(config)
    assert advisor.enabled is False
    assert "API key" in (advisor.disabled_reason or "")


def test_advisor_submit_respects_feature_flag():
    advisor = Advisor(_enabled_settings())
    assert advisor.submit("briefing", lambda: 1) is None
    advisor.executor.shutdown(wait=True)


def test_submit_runs_background_task_when_enabled():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_payload('{"ok": true}'))

    advisor = _advisor_with(
        handler, features={"briefing": True, "explainer": False}
    )
    future = advisor.submit("briefing", lambda: 7)
    assert future is not None
    assert future.result(timeout=5) == 7
    advisor.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_state_snapshot_minimised_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    config = Config(config_path=_write_config(tmp_path, None))
    config.max_monthly_amount = 1000.0
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    state.update(last_price=50000.0)

    snapshot = build_state_snapshot(config, state, store)

    assert snapshot["schema_version"] == "state-snapshot/v1"
    assert snapshot["trading_pair"] == "XBTCHF"
    assert snapshot["last_price"] == 50000.0
    assert snapshot["monthly_budget"] == {"limit": 1000.0, "spent": 0.0}
    assert "generated_at" in snapshot
    serialised = json.dumps(snapshot)
    assert "demo-key" not in serialised
    assert "demo-secret" not in serialised


# ---------------------------------------------------------------------------
# Bot wiring: advisor exists, is off by default, buy path unaffected
# ---------------------------------------------------------------------------


def test_bot_constructs_disabled_advisor_and_buy_path_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from test_core_behaviors import _make_bot

    bot = _make_bot(tmp_path, monkeypatch)
    assert bot.advisor.enabled is False
    # The buy path resolves amounts without any advisor involvement.
    assert bot.resolve_buy_amount(90000.0) == bot.config.crypto_amount
    bot.advisor.executor.shutdown(wait=True)


def test_bot_with_enabled_advisor_and_dead_provider_still_resolves_buys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from test_core_behaviors import _make_bot

    bot = _make_bot(
        tmp_path,
        monkeypatch,
        advisor={
            "enabled": True,
            "features": {"briefing": True, "explainer": False},
            "provider": "openai",
            "base_url": "https://unreachable.example.test/v1",
            "model": "test-model",
            "max_retries": 0,
        },
    )
    monkeypatch.setenv("AI_ADVISOR_API_KEY", "test-key")
    # Rebuild the advisor now that the key is present.
    from bot.advisor import Advisor as _Advisor

    bot.advisor = _Advisor.from_config(bot.config)
    assert bot.advisor.enabled is True

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    bot.advisor.provider = _client_with(failing_handler)  # type: ignore[union-attr]
    advice = bot.advisor.ask("briefing", system_prompt="s", user_prompt="u")
    assert advice.status == AdviceStatus.DISABLED
    # The base strategy is untouched by advisor failures.
    assert bot.resolve_buy_amount(90000.0) == bot.config.crypto_amount
    bot.advisor.executor.shutdown(wait=True)


def test_advisor_provider_error_includes_http_detail(tmp_path: Path):
    """Operator-facing errors must name the HTTP status and a short body
    excerpt — a bare 'provider http' is not actionable (staging incident
    while validating the A9 test button, 2026-10-11)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, text='{"error": {"message": "invalid api key provided"}}'
        )

    advisor = _advisor_with(
        handler,
        features={"briefing": True, "explainer": False},
        max_retries=0,
    )
    advice = advisor.ask("briefing", system_prompt="s", user_prompt="u")
    assert advice.status == AdviceStatus.DISABLED
    assert advice.error is not None
    assert "401" in advice.error
    assert "invalid api key" in advice.error
    # The message must stay single-line for logs and the dashboard.
    assert "\n" not in advice.error
