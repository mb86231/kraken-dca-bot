"""Provider abstraction for AI advisor backends.

Two implementations: OpenAI-compatible chat-completions APIs (covers
OpenAI, Kimi Open Platform and similar gateways) and Ollama's local
OpenAI-compatible endpoint. Capabilities such as JSON-mode responses are
requested as a hint but never trusted — every response is validated
locally by ``bot.advisor.validate`` regardless of provider.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class ProviderError(Exception):
    """A provider call failed. ``category`` feeds the audit log."""

    CATEGORIES = ("timeout", "rate_limited", "network", "http", "invalid_response")

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _error_excerpt(body: object, limit: int = 160) -> str:
    """Single-line excerpt of a provider error body, safe to show operators."""
    cleaned = " ".join(str(body).split())
    return cleaned[:limit]


@dataclass
class ProviderResult:
    """Raw, unvalidated provider output plus usage metadata."""

    text: str
    model: str
    latency_ms: float
    tokens_used: int | None = None
    cost_estimate: float | None = None


class ProviderClient(Protocol):
    """Synchronous completion interface. Implementations must never raise
    arbitrary exceptions; failures are raised as :class:`ProviderError`."""

    supports_json_mode: bool

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 512,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ProviderResult: ...


class OpenAICompatibleClient:
    """Client for any OpenAI-compatible ``/chat/completions`` endpoint."""

    supports_json_mode = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        max_retries: int = 2,
        temperature: float = 0.2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.temperature = temperature
        self._client = httpx.Client(timeout=httpx.Timeout(timeout))

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 512,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ProviderResult:
        """Complete a chat request.

        ``timeout`` and ``max_retries`` override the client defaults for this
        call (used to bound latency-sensitive paths like dashboard test sends).
        """
        request_timeout = self.timeout if timeout is None else timeout
        attempts = self.max_retries if max_retries is None else max(0, max_retries)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: ProviderError | None = None
        for attempt in range(attempts + 1):
            started = time.monotonic()
            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=request_timeout,
                )
            except httpx.TimeoutException:
                last_error = ProviderError("timeout", f"request timed out after {request_timeout}s")
            except httpx.TransportError as exc:
                last_error = ProviderError("network", f"transport error: {exc}")
            else:
                latency_ms = (time.monotonic() - started) * 1000
                if response.status_code == 429:
                    raise ProviderError("rate_limited", "provider returned HTTP 429")
                if response.status_code >= 500:
                    last_error = ProviderError(
                        "http",
                        f"provider returned HTTP {response.status_code}: "
                        f"{_error_excerpt(response.text)}",
                    )
                elif response.status_code != 200:
                    raise ProviderError(
                        "http",
                        f"provider returned HTTP {response.status_code}: "
                        f"{_error_excerpt(response.text)}",
                    )
                else:
                    return self._parse(response.text, latency_ms)
            # Retry only transient categories; rate-limit and 4xx fail fast.
            if last_error is not None and last_error.category not in ("timeout", "network", "http"):
                raise last_error
            if attempt < attempts:
                time.sleep(min(2**attempt, 4))
        assert last_error is not None
        raise last_error

    def _parse(self, body: str, latency_ms: float) -> ProviderResult:
        try:
            data = json.loads(body)
            choices = data["choices"]
            message = choices[0]["message"]
            text = message.get("content") or ""
            finish_reason = choices[0].get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("invalid_response", f"malformed completion payload: {exc}")
        if not text.strip():
            # Reasoning models can burn the whole max_tokens budget on
            # internal reasoning and return empty content; report that
            # distinctly instead of letting it look like a refusal.
            raise ProviderError(
                "invalid_response",
                f"provider returned empty content (finish_reason={finish_reason!r}); "
                "the model likely exhausted max_tokens — raise advisor.max_tokens",
            )
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens")
        return ProviderResult(
            text=text,
            model=str(data.get("model") or self.model),
            latency_ms=latency_ms,
            tokens_used=int(tokens) if tokens is not None else None,
        )


class OllamaClient:
    """Client for a local Ollama instance (``/api/generate``, JSON mode)."""

    supports_json_mode = True

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        temperature: float = 0.2,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self._client = httpx.Client(timeout=httpx.Timeout(timeout))

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 512,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ProviderResult:
        # max_retries is accepted for interface parity; Ollama does not retry.
        del max_retries
        request_timeout = self.timeout if timeout is None else timeout
        started = time.monotonic()
        try:
            response = self._client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "system": system_prompt,
                    "prompt": user_prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": self.temperature,
                    },
                },
                timeout=request_timeout,
            )
        except httpx.TimeoutException:
            raise ProviderError("timeout", f"ollama request timed out after {request_timeout}s")
        except httpx.TransportError as exc:
            raise ProviderError("network", f"transport error: {exc}")
        latency_ms = (time.monotonic() - started) * 1000
        if response.status_code != 200:
            raise ProviderError(
                "http",
                f"ollama returned HTTP {response.status_code}: "
                f"{_error_excerpt(response.text)}",
            )
        try:
            data = response.json()
            text = data["response"] or ""
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("invalid_response", f"malformed ollama payload: {exc}")
        if not text.strip():
            raise ProviderError(
                "invalid_response",
                "ollama returned empty content; the model likely exhausted "
                "max_tokens — raise advisor.max_tokens",
            )
        tokens = data.get("eval_count")
        return ProviderResult(
            text=text,
            model=self.model,
            latency_ms=latency_ms,
            tokens_used=int(tokens) if tokens is not None else None,
        )


def create_provider(
    provider: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
    max_retries: int,
    temperature: float = 0.2,
) -> ProviderClient:
    """Factory used by :class:`bot.advisor.service.Advisor`."""
    if provider == "ollama":
        if not base_url:
            raise ValueError("advisor base_url is required for the ollama provider")
        return OllamaClient(
            base_url=base_url, model=model, timeout=timeout, temperature=temperature
        )
    if provider == "openai":
        if not api_key:
            raise ValueError("advisor API key is required for the openai provider")
        return OpenAICompatibleClient(
            base_url=base_url or "https://api.openai.com/v1",
            api_key=api_key,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            temperature=temperature,
        )
    raise ValueError(f"unknown advisor provider: {provider!r}")
