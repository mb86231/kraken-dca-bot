"""Tests for the Telegram notifier without network calls."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from bot.notifier import Notifier


def test_notifier_disabled_without_config(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = Notifier()
    assert notifier.enabled is False
    assert notifier.send("test") is False


def test_notifier_disabled_with_only_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = Notifier()
    assert notifier.enabled is False


def test_notifier_send_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    notifier = Notifier()
    assert notifier.enabled is True

    fake_response = MagicMock()
    fake_response.read.return_value = b'{"ok": true}'
    fake_response.__enter__ = MagicMock(return_value=fake_response)
    fake_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=fake_response):
        assert notifier.send("hello") is True


def test_notifier_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    notifier = Notifier()

    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        assert notifier.send("hello") is False


def test_notifier_test_disabled(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = Notifier()
    success, message = notifier.test()
    assert success is False
    assert "not configured" in message.lower()


def test_notifier_to_dict_masks_token():
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
    os.environ["TELEGRAM_CHAT_ID"] = "12345"
    notifier = Notifier()
    data = notifier.to_dict(mask_secrets=True)
    assert data["bot_token"] == "••••••••••"
    assert data["chat_id"] == "••••••••"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
