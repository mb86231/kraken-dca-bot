"""Tests for the revocable server-side session registry (F7)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from web.sessions import SessionRegistry


@pytest.fixture
def registry(tmp_path: Path) -> SessionRegistry:
    return SessionRegistry(path=tmp_path / "sessions.json")


def test_issue_and_validate_roundtrip(registry: SessionRegistry):
    jti = registry.issue("admin", ttl_seconds=3600)
    assert registry.validate(jti) == "admin"


def test_unknown_jti_is_rejected(registry: SessionRegistry):
    assert registry.validate("no-such-id") is None
    assert registry.validate("") is None


def test_revoked_session_fails(registry: SessionRegistry):
    jti = registry.issue("admin", ttl_seconds=3600)
    registry.revoke(jti)
    assert registry.validate(jti) is None


def test_expired_session_fails(registry: SessionRegistry):
    jti = registry.issue("admin", ttl_seconds=0)
    time.sleep(0.01)
    assert registry.validate(jti) is None


def test_revoke_all_invalidates_every_session(registry: SessionRegistry):
    a = registry.issue("admin", ttl_seconds=3600)
    b = registry.issue("admin", ttl_seconds=3600)
    registry.revoke_all()
    assert registry.validate(a) is None
    assert registry.validate(b) is None


def test_corrupt_file_fails_closed(registry: SessionRegistry, tmp_path: Path):
    jti = registry.issue("admin", ttl_seconds=3600)
    (tmp_path / "sessions.json").write_text("not json", encoding="utf-8")
    # A corrupt registry must not admit sessions.
    assert registry.validate(jti) is None


def test_missing_file_fails_closed(registry: SessionRegistry):
    assert registry.validate("anything") is None


def test_issue_prunes_expired_entries(registry: SessionRegistry, tmp_path: Path):
    expired = registry.issue("admin", ttl_seconds=0)
    time.sleep(0.01)
    assert registry.validate(expired) is None
    registry.issue("admin", ttl_seconds=3600)
    import json

    data = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert len(data) == 1


def test_default_path_follows_secrets_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.delenv("SESSIONS_PATH", raising=False)
    reg = SessionRegistry()
    assert reg._file.filepath == tmp_path / "sessions.json"
    # Explicit override wins.
    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "custom.json"))
    assert reg._file.filepath == tmp_path / "custom.json"
