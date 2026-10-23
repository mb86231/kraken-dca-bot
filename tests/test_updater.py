"""Tests for the update ticker (bot/updater.py).

The ticker is strictly informational: these tests pin down the version
comparison, the persisted state round-trip, the deduplicated notification
behaviour and the dashboard status payload.
"""

from pathlib import Path

import pytest

from bot.updater import (
    UpdateChecker,
    UpdateState,
    fetch_latest_release,
    is_newer_version,
    parse_version,
    status_payload,
)
from bot.utils import APP_VERSION


class FakeClient:
    """Minimal stand-in for httpx.Client returning a canned response."""

    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self._status_code = status_code
        self.requests = []

    def get(self, url, timeout=None):
        self.requests.append(url)

        class _Resp:
            def __init__(self, payload, status_code):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                if self._payload is None:
                    raise ValueError("no json")
                return self._payload

        return _Resp(self._payload, self._status_code)

    def close(self):
        pass


# -- version parsing / comparison ------------------------------------------


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v1.3.3", (1, 3, 3)),
        ("1.3.3", (1, 3, 3)),
        ("V2.0", (2, 0)),
        ("v1.3.3-beta.1", None),
        ("latest", None),
        ("", None),
        ("v1.3.x", None),
    ],
)
def test_parse_version(tag, expected):
    assert parse_version(tag) == expected


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("v1.3.3", "1.3.2", True),
        ("v1.3.2", "1.3.2", False),
        ("v1.3.2", "1.3.3", False),
        ("v1.10.0", "v1.9.9", True),
        ("v2.0", "v1.3.2", True),
        ("v1.3.2.1", "v1.3.2", True),
        ("v1.3.3-beta", "1.3.2", False),
        ("garbage", "1.3.2", False),
    ],
)
def test_is_newer_version(latest, current, expected):
    assert is_newer_version(latest, current) is expected


# -- fetch_latest_release ----------------------------------------------------


def test_fetch_latest_release_ok():
    client = FakeClient(
        {
            "tag_name": "v1.4.0",
            "published_at": "2026-10-24T00:00:00Z",
            "html_url": "https://github.com/mb86231/kraken-dca-bot/releases/tag/v1.4.0",
        }
    )
    release = fetch_latest_release(client)
    assert release is not None
    assert release["tag"] == "v1.4.0"
    assert release["url"].startswith("https://github.com/")


def test_fetch_latest_release_ignores_prerelease():
    client = FakeClient({"tag_name": "v1.4.0-beta.1"})
    release = fetch_latest_release(client)
    assert release is not None
    assert release["tag"] == "v1.4.0-beta.1"
    # rejection happens in is_newer_version, not in fetch
    assert is_newer_version(release["tag"], "1.3.2") is False


def test_fetch_latest_release_non_200():
    assert fetch_latest_release(FakeClient(status_code=403)) is None


def test_fetch_latest_release_malformed():
    assert fetch_latest_release(FakeClient(payload={})) is None
    assert fetch_latest_release(FakeClient(payload=None)) is None


# -- UpdateState round-trip ---------------------------------------------------


def test_update_state_roundtrip(tmp_path: Path):
    state = UpdateState(tmp_path / "update_state.json")
    assert state.load() == {}
    state.save({"latest_seen_tag": "v1.4.0", "dismissed_tag": "v1.3.9"})
    assert state.load()["latest_seen_tag"] == "v1.4.0"


def test_update_state_corrupt_file_is_empty(tmp_path: Path):
    path = tmp_path / "update_state.json"
    path.write_text("not json", encoding="utf-8")
    assert UpdateState(path).load() == {}


# -- UpdateChecker notification dedupe ----------------------------------------


def _checker(tmp_path: Path, on_new_release, current_version="1.3.2"):
    return UpdateChecker(
        enabled=True,
        interval_hours=24,
        state=UpdateState(tmp_path / "update_state.json"),
        on_new_release=on_new_release,
        current_version=current_version,
    )


def test_check_now_notifies_once_per_tag(tmp_path: Path):
    notified = []
    checker = _checker(tmp_path, lambda tag, url: notified.append((tag, url)))
    client = FakeClient(
        {
            "tag_name": "v1.4.0",
            "published_at": "2026-10-24T00:00:00Z",
            "html_url": "https://example.com/release",
        }
    )

    checker.check_now(client)
    checker.check_now(client)  # same tag again → no second notification

    assert notified == [("v1.4.0", "https://example.com/release")]
    data = checker.state.load()
    assert data["notified_tag"] == "v1.4.0"
    assert data["latest_seen_tag"] == "v1.4.0"


def test_check_now_ignores_older_release(tmp_path: Path):
    notified = []
    checker = _checker(tmp_path, lambda tag, url: notified.append(tag))
    client = FakeClient({"tag_name": "v1.0.0", "html_url": "https://example.com/x"})

    checker.check_now(client)

    assert notified == []
    assert checker.state.load()["latest_seen_tag"] == "v1.0.0"


def test_check_now_ignores_prerelease_tag(tmp_path: Path):
    notified = []
    checker = _checker(tmp_path, lambda tag, url: notified.append(tag))
    client = FakeClient({"tag_name": "v2.0.0-beta.1", "html_url": "https://example.com/x"})

    checker.check_now(client)

    assert notified == []


def test_check_now_survives_fetch_failure(tmp_path: Path):
    checker = _checker(tmp_path, lambda tag, url: None)
    data = checker.check_now(FakeClient(status_code=500))
    assert "latest_seen_tag" not in data
    assert data["last_check_at"]  # check timestamp still persisted


def test_check_now_callback_failure_does_not_raise(tmp_path: Path):
    def boom(tag, url):
        raise RuntimeError("telegram down")

    checker = _checker(tmp_path, boom)
    client = FakeClient({"tag_name": "v1.4.0", "html_url": "https://example.com/x"})

    data = checker.check_now(client)  # must not raise

    assert data["notified_tag"] == "v1.4.0"


def test_disabled_checker_never_notifies(tmp_path: Path):
    notified = []
    checker = _checker(tmp_path, lambda tag, url: notified.append(tag))
    checker.enabled = False
    client = FakeClient({"tag_name": "v1.4.0", "html_url": "https://example.com/x"})

    checker.check_now(client)

    assert notified == []
    # state still records what was seen, so the banner can decide later
    assert checker.state.load()["latest_seen_tag"] == "v1.4.0"


def test_check_if_due_respects_interval(tmp_path: Path, monkeypatch):
    calls = []

    def _fake_fetch(client, url=None):
        calls.append(url)
        return None

    monkeypatch.setattr("bot.updater.fetch_latest_release", _fake_fetch)
    checker = _checker(tmp_path, lambda tag, url: None)

    checker.check_if_due()  # no last_check_at → performs a check
    checker.check_if_due()  # within the interval → no-op

    assert len(calls) == 1


# -- status_payload -------------------------------------------------------------


class _Cfg:
    update_check_enabled = True


def test_status_payload_update_available(tmp_path: Path):
    state = UpdateState(tmp_path / "update_state.json")
    state.save(
        {
            "latest_seen_tag": "v1.4.0",
            "latest_url": "https://example.com/release",
            "latest_published_at": "2026-10-24T00:00:00Z",
        }
    )
    checker = UpdateChecker(state=state, current_version="1.3.2")
    payload = status_payload(_Cfg(), checker)
    assert payload["update_available"] is True
    assert payload["latest_version"] == "v1.4.0"
    assert payload["current_version"] == APP_VERSION
    assert payload["release_url"] == "https://example.com/release"


def test_status_payload_dismissed(tmp_path: Path):
    state = UpdateState(tmp_path / "update_state.json")
    state.save({"latest_seen_tag": "v1.4.0", "dismissed_tag": "v1.4.0"})
    checker = UpdateChecker(state=state, current_version="1.3.2")
    payload = status_payload(_Cfg(), checker)
    assert payload["update_available"] is False
    assert payload["dismissed"] is True


def test_status_payload_disabled(tmp_path: Path):
    state = UpdateState(tmp_path / "update_state.json")
    state.save({"latest_seen_tag": "v1.4.0"})
    checker = UpdateChecker(state=state, current_version="1.3.2")
    cfg = _Cfg()
    cfg.update_check_enabled = False
    payload = status_payload(cfg, checker)
    assert payload["update_available"] is False
    assert payload["enabled"] is False


def test_status_payload_no_state(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = status_payload(None, None)
    assert payload["update_available"] is False
    assert payload["current_version"] == APP_VERSION


# -- config wiring ---------------------------------------------------------------


def test_config_accepts_update_check_fields(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    from bot.config import Config

    cfg = Config()
    assert cfg.update_check_enabled is True
    assert cfg.update_check_interval_hours >= 1
    assert cfg.to_dict(mask_secrets=True)["update_check_enabled"] is True


def test_config_rejects_invalid_update_interval(monkeypatch, tmp_path: Path):
    import json

    monkeypatch.chdir(tmp_path)
    from bot.config import Config

    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "config.json").write_text(
        json.dumps(
            {
                "update_check_interval_hours": 0,
                "crypto_amount": 0.0005,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        Config(config_path=tmp_path / "data" / "config.json")
