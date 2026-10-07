"""Tests for the interactive Telegram command bot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.config import Config
from bot.core import KrakenDCA
from bot.state import BotState
from bot.store import TransactionStore
from bot.telegram_commands import TelegramCommandBot


class FakeTelegramApi:
    """Records sent messages instead of calling the network."""

    def __init__(self):
        self.sent: list[dict] = []
        self.updates: list[dict] = []
        self.fail_send = False

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        return []

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        if self.fail_send:
            from bot.telegram_commands import TelegramApiError

            raise TelegramApiError("boom")
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True}

    def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, "edit": message_id})

    def answer_callback(self, callback_id: str) -> None:
        pass

    # -- test helpers ------------------------------------------------------

    def last_text(self) -> str:
        return self.sent[-1]["text"]

    def last_keyboard(self) -> dict | None:
        return self.sent[-1]["reply_markup"]

    @staticmethod
    def _update(update_id: int, chat_id: str, text: str) -> dict:
        return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}

    @staticmethod
    def _callback(update_id: int, chat_id: str, callback_id: str, data: str) -> dict:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": callback_id,
                "data": data,
                "message": {"chat": {"id": chat_id}},
            },
        }


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> KrakenDCA:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"max_monthly_amount": 10000, '
        '"dynamic_dca": {"enabled": true, "reference": "last_buy", "cooldown_hours": 24.0, '
        '"tiers": ['
        '{"threshold_percent": 10.0, "amount": 0.0, "enabled": true}, '
        '{"threshold_percent": 5.0, "amount": 0.00005, "enabled": true}, '
        '{"threshold_percent": -2.0, "amount": 0.0001, "enabled": true}, '
        '{"threshold_percent": -5.0, "amount": 0.00015, "enabled": true}, '
        '{"threshold_percent": -10.0, "amount": 0.0002, "enabled": true}, '
        '{"threshold_percent": -20.0, "amount": 0.0003, "enabled": true}'
        ']}}'
    )
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    config = Config(config_path=config_path)
    store = TransactionStore(filepath=tmp_path / "transactions.json")
    state = BotState(filepath=tmp_path / "state.json", persist=False)
    from bot.state import RuntimeOverrides

    overrides = RuntimeOverrides(filepath=tmp_path / "runtime_overrides.json")
    bot = KrakenDCA(config=config, store=store, state=state, overrides=overrides)
    bot.store.clear()
    return bot


@pytest.fixture
def tg(app: KrakenDCA, tmp_path: Path, monkeypatch) -> tuple[TelegramCommandBot, FakeTelegramApi]:
    fake = FakeTelegramApi()
    monkeypatch.setattr(fake, "get_updates", lambda offset, timeout=25: [])
    tgb = TelegramCommandBot(
        app,
        api=fake,
        audit_path=tmp_path / "audit_log.json",
        alerts_path=tmp_path / "alerts.json",
    )
    # Force-enable even without a real token; allow-list "4242".
    tgb.enabled = True
    tgb.allowed_chat_ids = ["4242"]
    return tgb, fake


ALLOWED = "4242"
STRANGER = "9999"


class TestAuthorization:
    def test_unknown_chat_is_silently_ignored(self, tg):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, STRANGER, "/status"))
        assert fake.sent == []

    def test_unknown_chat_callback_is_ignored(self, tg):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._callback(1, STRANGER, "cb1", "tg:ok:abc"))
        assert fake.sent == []

    def test_unknown_command_gets_help_hint(self, tg):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/frobnicate"))
        assert "Unknown command" in fake.last_text()


class TestCommands:
    def test_help(self, tg):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/help"))
        assert "/status" in fake.last_text()

    def test_status(self, tg, app):
        tgb, fake = tg
        app.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/status"))
        assert "Portfolio" in fake.last_text()
        assert "P/L" in fake.last_text()

    def test_price_shows_active_tier(self, tg, app):
        tgb, fake = tg
        app.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        app.api.get_ticker = lambda pair: 47500.0  # -5%
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/price"))
        assert "Active tier: -5" in fake.last_text()
        assert "0.00015" in fake.last_text()

    def test_budget(self, tg):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/budget"))
        assert "Monthly budget" in fake.last_text()

    def test_last_transactions(self, tg, app):
        tgb, fake = tg
        app.store.add_transaction("XBTCHF", 0.00015, 47500.0, strategy="dynamic", dynamic_tier=-5.0)
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/last"))
        assert "dynamic -5%" in fake.last_text()

    def test_alerts_empty(self, tg):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/alerts"))
        assert "No open alerts" in fake.last_text()

    def test_command_with_botname_suffix(self, tg):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/help@SomeBot"))
        assert "/status" in fake.last_text()


class TestConfirmFlow:
    def _extract_token(self, fake: FakeTelegramApi) -> str:
        keyboard = fake.last_keyboard()
        assert keyboard is not None
        return keyboard["inline_keyboard"][0][0]["callback_data"].split(":", 2)[2]

    def test_pause_requires_confirmation(self, tg, app):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/pause"))
        assert "Pause the bot?" in fake.last_text()
        assert fake.last_keyboard() is not None
        # Not yet paused — the confirm button must be pressed.
        assert app.state.paused is False

    def test_pause_confirmed(self, tg, app):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/pause"))
        token = self._extract_token(fake)
        tgb._handle_update(FakeTelegramApi._callback(2, ALLOWED, "cb1", f"tg:ok:{token}"))
        assert app.state.paused is True
        assert "paused" in fake.last_text().lower()

    def test_cancel_leaves_state_unchanged(self, tg, app):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/pause"))
        token = self._extract_token(fake)
        tgb._handle_update(FakeTelegramApi._callback(2, ALLOWED, "cb1", f"tg:no:{token}"))
        assert app.state.paused is False
        assert "Cancelled" in fake.last_text()

    def test_confirmation_token_is_single_use(self, tg, app):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/pause"))
        token = self._extract_token(fake)
        tgb._handle_update(FakeTelegramApi._callback(2, ALLOWED, "cb1", f"tg:ok:{token}"))
        assert app.state.paused is True
        app.state.update(paused=False)
        tgb._handle_update(FakeTelegramApi._callback(3, ALLOWED, "cb2", f"tg:ok:{token}"))
        assert "expired" in fake.last_text().lower()
        assert app.state.paused is False

    def test_confirmation_cannot_be_replayed_from_other_chat(self, tg, app):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/pause"))
        token = self._extract_token(fake)
        sent_before = len(fake.sent)
        tgb._handle_update(FakeTelegramApi._callback(2, STRANGER, "cb1", f"tg:ok:{token}"))
        assert app.state.paused is False
        assert len(fake.sent) == sent_before  # stranger gets silence

    def test_buy_request_sets_manual_cycle(self, tg, app):
        tgb, fake = tg
        app.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/buy"))
        token = self._extract_token(fake)
        assert app.overrides.manual_cycle_requested is False
        tgb._handle_update(FakeTelegramApi._callback(2, ALLOWED, "cb1", f"tg:ok:{token}"))
        assert app.overrides.manual_cycle_requested is True

    def test_buy_over_budget_warns_and_sets_one_shot_override(self, tg, app):
        tgb, fake = tg
        app.config.max_monthly_amount = 1.0  # any buy exceeds the budget
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/buy"))
        assert "exceeds the monthly budget" in fake.last_text()
        token = self._extract_token(fake)
        tgb._handle_update(FakeTelegramApi._callback(2, ALLOWED, "cb1", f"tg:ok:{token}"))
        assert app.overrides.manual_cycle_requested is True
        assert app.overrides.manual_buy_over_budget is True

    def test_buy_within_budget_has_no_over_budget_warning(self, tg, app):
        tgb, fake = tg
        app.config.max_monthly_amount = 10000.0
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/buy"))
        assert "exceeds the monthly budget" not in fake.last_text()
        token = self._extract_token(fake)
        tgb._handle_update(FakeTelegramApi._callback(2, ALLOWED, "cb1", f"tg:ok:{token}"))
        assert app.overrides.manual_buy_over_budget is False

    def test_expired_confirmation_rejected(self, tg, app):
        tgb, fake = tg
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/pause"))
        token = self._extract_token(fake)
        # Force expiry: monotonic() is always > 0, so this is always stale.
        tgb._pending[token]["expires"] = 0.0
        tgb._handle_update(FakeTelegramApi._callback(2, ALLOWED, "cb1", f"tg:ok:{token}"))
        assert app.state.paused is False
        assert "expired" in fake.last_text().lower()


class TestRateLimit:
    def test_flood_is_throttled(self, tg):
        tgb, fake = tg
        for i in range(3):
            tgb._handle_update(FakeTelegramApi._update(i, ALLOWED, "/help"))
        throttled = any("Slow down" in m["text"] for m in fake.sent)
        assert throttled

    def test_send_failure_does_not_raise(self, tg):
        tgb, fake = tg
        fake.fail_send = True
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/help"))  # must not raise


class TestAudit:
    def test_actions_are_audited(self, tg, tmp_path: Path):
        tgb, fake = tg
        tgb.min_command_interval_seconds = 0.0  # avoid rate limit between calls
        tgb._handle_update(FakeTelegramApi._update(1, ALLOWED, "/help"))
        tgb._handle_update(FakeTelegramApi._update(2, ALLOWED, "/pause"))
        token = TestConfirmFlow()._extract_token(fake)
        tgb._handle_update(FakeTelegramApi._callback(3, ALLOWED, "cb", f"tg:ok:{token}"))
        entries = json.loads((tmp_path / "audit_log.json").read_text())
        actions = [e["action"] for e in entries]
        assert "telegram_command" in actions
        assert "telegram_bot_paused" in actions
        assert all(e["source_ip"] == "telegram" for e in entries)
        assert all(not e["user"].startswith("telegram:4242") for e in entries)  # chat id masked


class TestLifecycle:
    def test_disabled_without_token(self, app, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        tgb = TelegramCommandBot(app, api=None, audit_path=tmp_path / "a.json", alerts_path=tmp_path / "al.json")
        assert tgb.enabled is False
        assert tgb.start() is False

    def test_explicitly_disabled(self, app, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
        monkeypatch.setenv("TELEGRAM_COMMANDS_ENABLED", "false")
        tgb = TelegramCommandBot(app, api=None, audit_path=tmp_path / "a.json", alerts_path=tmp_path / "al.json")
        assert tgb.enabled is False

    def test_allow_list_from_env(self, app, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111, 222,abc,-333")
        tgb = TelegramCommandBot(app, api=None, audit_path=tmp_path / "a.json", alerts_path=tmp_path / "al.json")
        assert tgb.allowed_chat_ids == ["111", "222", "-333"]
        assert tgb.enabled is True

    def test_fallback_to_notifier_chat_id(self, app, monkeypatch, tmp_path):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
        tgb = TelegramCommandBot(app, api=None, audit_path=tmp_path / "a.json", alerts_path=tmp_path / "al.json")
        assert tgb.allowed_chat_ids == ["4242"]
        assert tgb.enabled is True
