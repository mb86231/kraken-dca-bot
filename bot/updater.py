"""Update ticker: notify when a newer DCA-Bot release exists on GitHub.

Phase 1 of the update feature (see backlog). This module is strictly
informational — it never downloads release artefacts, never modifies the
running installation and never triggers an update by itself.

Design:

- A small background thread polls the GitHub public API
  (``repos/<owner>/<repo>/releases/latest``; no token required) at the
  configured interval, compares the release tag with :data:`APP_VERSION`
  and persists what it saw in a JSON state file under ``data/``.
- A release is announced at most once per tag (deduplicated via the
  persisted state) through a caller-supplied callback — the composition
  root wires Telegram and the alert store.
- The dashboard reads the persisted state through
  :func:`status_payload`; the banner is rendered exclusively via
  ``textContent`` (no HTML injection from the network).
- Every failure mode (offline, API error, malformed payload) is silent
  by design: the ticker must never disturb trading, logging or startup.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from bot.utils import APP_VERSION

logger = logging.getLogger(__name__)

GITHUB_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/mb86231/kraken-dca-bot/releases/latest"
)
REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_CHECK_INTERVAL_HOURS = 24
DEFAULT_STATE_PATH = Path("data/update_state.json")

# A release is announced at most once per (tag, running version) pair so a
# long-ignored update does not re-notify on every restart.
NotifyCallback = Callable[[str, str], None]


def parse_version(tag: str) -> Optional[tuple[int, ...]]:
    """Parse a release tag like ``v1.3.3``/``1.3.3`` into a numeric tuple.

    Returns ``None`` for anything that is not a plain dotted version —
    pre-releases and rolling tags must never compare as "newer".
    """
    cleaned = tag.strip().lstrip("vV")
    if not cleaned or not all(part.isdigit() for part in cleaned.split(".")):
        return None
    return tuple(int(part) for part in cleaned.split("."))


def is_newer_version(latest: str, current: str) -> bool:
    """True when ``latest`` is a strictly newer plain version than ``current``."""
    latest_tuple = parse_version(latest)
    current_tuple = parse_version(current)
    if latest_tuple is None or current_tuple is None:
        return False
    length = max(len(latest_tuple), len(current_tuple))
    padded_latest = latest_tuple + (0,) * (length - len(latest_tuple))
    padded_current = current_tuple + (0,) * (length - len(current_tuple))
    return padded_latest > padded_current


def fetch_latest_release(
    client: httpx.Client, url: str = GITHUB_RELEASES_LATEST_URL
) -> Optional[dict[str, str]]:
    """Fetch the newest GitHub release; ``None`` on any failure.

    Only three fields are consumed and only ever rendered as text:
    tag name, publication time and the release-notes URL.
    """
    try:
        response = client.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return None
        payload = response.json()
        tag = str(payload.get("tag_name") or "")
        if not tag:
            return None
        return {
            "tag": tag,
            "published_at": str(payload.get("published_at") or ""),
            "url": str(payload.get("html_url") or ""),
        }
    except (httpx.HTTPError, ValueError):
        return None


class UpdateState:
    """Small JSON state: what we last saw, what the user dismissed."""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class UpdateChecker:
    """Periodic GitHub release check with deduplicated notifications."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        interval_hours: int = DEFAULT_CHECK_INTERVAL_HOURS,
        state: Optional[UpdateState] = None,
        on_new_release: Optional[NotifyCallback] = None,
        current_version: str = APP_VERSION,
    ):
        self.enabled = enabled
        self.interval_seconds = max(1, int(interval_hours)) * 3600
        self.state = state or UpdateState()
        self.on_new_release = on_new_release
        self.current_version = current_version
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- checking -----------------------------------------------------------

    def check_now(self, client: Optional[httpx.Client] = None) -> dict[str, Any]:
        """One check cycle: fetch, compare, persist, notify.

        Returns the updated state; never raises — the ticker must be
        invisible when GitHub is unreachable.
        """
        data = self.state.load()
        data["last_check_at"] = datetime.now(timezone.utc).isoformat()
        own_client = client is None
        if client is None:
            client = httpx.Client()
        try:
            release = fetch_latest_release(client)
        finally:
            if own_client:
                client.close()
        if release is not None:
            previous_tag = data.get("latest_seen_tag")
            data["latest_seen_tag"] = release["tag"]
            data["latest_published_at"] = release["published_at"]
            data["latest_url"] = release["url"]
            newly_seen = release["tag"] != previous_tag
            if (
                newly_seen
                and is_newer_version(release["tag"], self.current_version)
                and data.get("notified_tag") != release["tag"]
            ):
                data["notified_tag"] = release["tag"]
                self.state.save(data)
                if self.on_new_release is not None and self.enabled:
                    try:
                        self.on_new_release(release["tag"], release["url"])
                    except Exception:  # notification must never break the check
                        logger.exception("update notification failed")
            else:
                self.state.save(data)
        else:
            self.state.save(data)
        return data

    def check_if_due(self) -> dict[str, Any]:
        """Check only when the configured interval has passed since the last one."""
        data = self.state.load()
        last = data.get("last_check_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (
                    datetime.now(timezone.utc) - last_dt
                ).total_seconds() < self.interval_seconds:
                    return data
            except ValueError:
                pass
        return self.check_now()

    # -- background loop ------------------------------------------------------

    def start_background(self) -> None:
        """Start the daemon thread; no-op when disabled or already running."""
        if not self.enabled or self._thread is not None:
            return

        def _loop() -> None:
            # First check promptly after startup so a long-running instance
            # hears about releases without waiting a full interval.
            try:
                self.check_now()
            except Exception:
                logger.exception("update check failed")
            while not self._stop.wait(self.interval_seconds):
                try:
                    self.check_now()
                except Exception:
                    logger.exception("update check failed")

        self._thread = threading.Thread(
            target=_loop, name="update-checker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            self._thread = None


def status_payload(
    config: Any, checker: Optional[UpdateChecker] = None
) -> dict[str, Any]:
    """What the dashboard banner needs; pure read of persisted state."""
    enabled = bool(getattr(config, "update_check_enabled", True)) if config else True
    state = (checker.state if checker else UpdateState()).load()
    latest = state.get("latest_seen_tag") or ""
    dismissed = state.get("dismissed_tag") or ""
    available = (
        enabled
        and bool(latest)
        and latest != dismissed
        and is_newer_version(latest, APP_VERSION)
    )
    return {
        "enabled": enabled,
        "current_version": APP_VERSION,
        "latest_version": latest,
        "update_available": available,
        "release_url": state.get("latest_url") or "",
        "published_at": state.get("latest_published_at") or "",
        "last_check_at": state.get("last_check_at") or "",
        "dismissed": bool(latest) and latest == dismissed,
    }
