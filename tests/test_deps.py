"""Tests for FastAPI dependencies and dependency injection path consistency."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from web import deps


def _request_with_state(attrs: dict | None = None) -> MagicMock:
    request = MagicMock()
    request.app.state = SimpleNamespace(**(attrs or {}))
    return request


@pytest.fixture(autouse=True)
def _clear_app_env(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)


class TestStrictDependencies:
    def test_get_config_missing_raises_in_production(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        request = _request_with_state()
        with pytest.raises(deps.HTTPException) as exc:
            deps.get_config(request)
        assert exc.value.status_code == 503
        assert "config" in exc.value.detail.lower()

    def test_get_store_missing_raises_in_staging(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "staging")
        request = _request_with_state()
        with pytest.raises(deps.HTTPException) as exc:
            deps.get_store(request)
        assert "transaction store" in exc.value.detail.lower()

    def test_get_state_missing_raises_in_production(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        request = _request_with_state()
        with pytest.raises(deps.HTTPException) as exc:
            deps.get_state(request)
        assert "bot state" in exc.value.detail.lower()

    def test_get_overrides_missing_raises_in_production(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        request = _request_with_state()
        with pytest.raises(deps.HTTPException) as exc:
            deps.get_overrides(request)
        assert "runtime overrides" in exc.value.detail.lower()

    def test_get_attempt_store_missing_raises_in_production(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        request = _request_with_state()
        with pytest.raises(deps.HTTPException) as exc:
            deps.get_attempt_store(request)
        assert "order attempt store" in exc.value.detail.lower()

    def test_strict_env_detects_staging(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "staging")
        assert deps._strict_env() is True

    def test_strict_env_defaults_to_production(self, monkeypatch):
        monkeypatch.delenv("APP_ENV", raising=False)
        assert deps._strict_env() is True


class TestDevelopmentFallbacks:
    def test_get_config_returns_app_state_when_present(self):
        sentinel = object()
        request = _request_with_state({"config": sentinel})
        assert deps.get_config(request) is sentinel

    def test_get_store_returns_app_state_when_present(self):
        sentinel = object()
        request = _request_with_state({"store": sentinel})
        assert deps.get_store(request) is sentinel

    def test_get_state_returns_app_state_when_present(self):
        sentinel = object()
        request = _request_with_state({"bot_state": sentinel})
        assert deps.get_state(request) is sentinel

    def test_get_overrides_returns_app_state_when_present(self):
        sentinel = object()
        request = _request_with_state({"overrides": sentinel})
        assert deps.get_overrides(request) is sentinel

    def test_get_attempt_store_returns_app_state_when_present(self):
        sentinel = object()
        request = _request_with_state({"attempt_store": sentinel})
        assert deps.get_attempt_store(request) is sentinel

    def test_development_fallback_uses_default_instances(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "development")
        # Patch the fallback constructors so the test does not depend on real files.
        monkeypatch.setattr(deps, "Config", Mock(return_value="config-fallback"))
        monkeypatch.setattr(deps, "TransactionStore", Mock(return_value="store-fallback"))
        monkeypatch.setattr(deps, "BotState", Mock(return_value="state-fallback"))
        monkeypatch.setattr(deps, "RuntimeOverrides", Mock(return_value="overrides-fallback"))
        monkeypatch.setattr(deps, "OrderAttemptStore", Mock(return_value="attempt-fallback"))
        # Provide no state attributes so the fallback constructors are used.
        request = _request_with_state()
        assert deps.get_config(request) == "config-fallback"
        assert deps.get_store(request) == "store-fallback"
        assert deps.get_state(request) == "state-fallback"
        assert deps.get_overrides(request) == "overrides-fallback"
        assert deps.get_attempt_store(request) == "attempt-fallback"
