"""Tests for the dynamic DCA amount adjustment feature."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import Config, DEFAULT_DYNAMIC_TIERS
from bot.core import KrakenDCA
from bot.state import BotState
from bot.store import TransactionStore


@pytest.fixture
def dynamic_config_path(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8, '
        '"dip_buy_cooldown_hours": 2.0, "max_price": 65000, "max_monthly_amount": 10000, '
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
    return config_path


@pytest.fixture
def dynamic_config(dynamic_config_path: Path, monkeypatch):
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    return Config(config_path=dynamic_config_path)


def test_config_loads_dynamic_dca_defaults(dynamic_config: Config):
    assert dynamic_config.dynamic_dca_enabled is True
    assert dynamic_config.dynamic_dca_reference == "last_buy"
    assert dynamic_config.dynamic_dca_cooldown_hours == 24.0
    assert len(dynamic_config.dynamic_dca_tiers) == 6


def test_config_default_tiers_when_missing(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dip_threshold_percent": 5.0, "poll_interval_seconds": 300, "buy_hour": 8}'
    )
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    config = Config(config_path=config_path)
    assert config.dynamic_dca_enabled is False
    assert len(config.dynamic_dca_tiers) == len(DEFAULT_DYNAMIC_TIERS)


def test_config_rejects_invalid_reference(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dynamic_dca": {"enabled": true, "reference": "invalid", "tiers": '
        '[{"threshold_percent": -2, "amount": 0.0001, "enabled": true}]}}'
    )
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    with pytest.raises(Exception, match="dynamic_dca.reference must be"):
        Config(config_path=config_path)


def test_config_rejects_no_enabled_tiers(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"trading_pair": "XBTCHF", "deposit_day": 24, "crypto_amount": 0.0001, '
        '"dynamic_dca": {"enabled": true, "tiers": '
        '[{"threshold_percent": -2, "amount": 0.0001, "enabled": false}]}}'
    )
    monkeypatch.setenv("KRAKEN_API_KEY", "demo-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "demo-secret")
    with pytest.raises(Exception, match="at least one enabled tier with amount"):
        Config(config_path=config_path)


class TestResolveBuyAmount:
    """Unit tests for KrakenDCA.resolve_buy_amount()."""

    @pytest.fixture
    def bot(self, dynamic_config: Config, tmp_path: Path):
        store = TransactionStore(filepath=tmp_path / "transactions.json")
        state = BotState(filepath=tmp_path / "state.json", persist=False)
        bot = KrakenDCA(config=dynamic_config, store=store, state=state)
        # Clear demo-seeded transactions so each test has a controlled history.
        bot.store.clear()
        return bot

    def test_dynamic_disabled_uses_crypto_amount(self, bot: KrakenDCA):
        bot.config.dynamic_dca_enabled = False
        assert bot.resolve_buy_amount(current_price=50000) == bot.config.crypto_amount

    def test_no_previous_buy_uses_base_amount(self, bot: KrakenDCA):
        assert bot.resolve_buy_amount(current_price=50000) == 0.0001

    def test_skip_tier_when_price_up_10_percent(self, bot: KrakenDCA):
        # Seed a buy at 50'000; current price 55'000 -> +10% -> skip
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=55000.0) == 0.0

    def test_reduced_amount_when_price_up_5_percent(self, bot: KrakenDCA):
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=52500.0) == 0.00005

    def test_base_amount_when_price_flat(self, bot: KrakenDCA):
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=50000.0) == 0.0001
        assert bot.resolve_buy_amount(current_price=51000.0) == 0.0001

    def test_larger_amount_when_price_down_5_percent(self, bot: KrakenDCA):
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=47500.0) == 0.00015

    def test_big_amount_when_price_down_10_percent(self, bot: KrakenDCA):
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=45000.0) == 0.0002

    def test_largest_amount_when_price_down_20_percent(self, bot: KrakenDCA):
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=40000.0) == 0.0003

    def test_disabled_tier_is_skipped(self, bot: KrakenDCA):
        # Disable the +5% tier; +7% should fall through to the -2% tier
        for tier in bot.config.dynamic_dca_tiers:
            if tier.threshold_percent == 5.0:
                tier.enabled = False
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=53500.0) == 0.0001

    def test_avg_buy_reference(self, bot: KrakenDCA):
        bot.config.dynamic_dca_reference = "avg_buy"
        bot.store.add_transaction("XBTCHF", 0.0001, 40000.0, strategy="scheduled")
        bot.store.add_transaction("XBTCHF", 0.0001, 60000.0, strategy="scheduled")
        # avg = 50'000, current = 45'000 -> -10% -> 0.0002
        assert bot.resolve_buy_amount(current_price=45000.0) == 0.0002


def test_dynamic_dca_to_dict_includes_block(dynamic_config: Config):
    data = dynamic_config.to_dict()
    assert "dynamic_dca" in data
    assert data["dynamic_dca"]["enabled"] is True
    assert data["dynamic_dca"]["reference"] == "last_buy"
    assert len(data["dynamic_dca"]["tiers"]) == 6


class TestMatchTier:
    """Unit tests for KrakenDCA._match_tier()."""

    @pytest.fixture
    def bot(self, dynamic_config: Config, tmp_path: Path):
        store = TransactionStore(filepath=tmp_path / "transactions.json")
        state = BotState(filepath=tmp_path / "state.json", persist=False)
        bot = KrakenDCA(config=dynamic_config, store=store, state=state)
        bot.store.clear()
        return bot

    def test_returns_threshold_and_amount(self, bot: KrakenDCA):
        assert bot._match_tier(-5.0) == (-5.0, 0.00015)
        assert bot._match_tier(-20.0) == (-20.0, 0.0003)
        assert bot._match_tier(10.0) == (10.0, 0.0)

    def test_fallback_returns_none_threshold(self, bot: KrakenDCA):
        # Below the lowest tier: the deepest tier still applies (crash
        # protection) — a crash must never buy less than the deepest tier.
        threshold, amount = bot._match_tier(-30.0)
        assert threshold == -20.0
        assert amount == 0.0003

    def test_disabled_tier_skipped(self, bot: KrakenDCA):
        for tier in bot.config.dynamic_dca_tiers:
            if tier.threshold_percent == 5.0:
                tier.enabled = False
        # +7% reaches neither the disabled +5% tier nor the +10% tier, and a
        # rise must never match a dip tier — base amount.
        assert bot._match_tier(7.0) == (None, bot.config.crypto_amount)


class TestMatchTierDipSemantics:
    """Regression tests for the tier-boundary bug: a dip tier must only
    fire when the drop actually reached the threshold."""

    @pytest.fixture
    def bot(self, dynamic_config: Config, tmp_path: Path):
        store = TransactionStore(filepath=tmp_path / "transactions.json")
        state = BotState(filepath=tmp_path / "state.json", persist=False)
        bot = KrakenDCA(config=dynamic_config, store=store, state=state)
        bot.store.clear()
        return bot

    def test_small_dip_below_first_tier_uses_base(self, bot: KrakenDCA):
        # -0.39% is shallower than the -2% tier: no dynamic buy at all.
        assert bot._match_tier(-0.39) == (None, bot.config.crypto_amount)

    def test_rise_never_matches_dip_tier(self, bot: KrakenDCA):
        # The prod incident: price +1.18% still produced "Dynamic -2%" buys.
        assert bot._match_tier(1.18) == (None, bot.config.crypto_amount)

    def test_exact_tier_boundary_matches(self, bot: KrakenDCA):
        assert bot._match_tier(-2.0) == (-2.0, 0.0001)

    def test_moderate_dip_matches_crossed_mark(self, bot: KrakenDCA):
        # -3% crossed the -2% mark but not -5%: the -2% tier applies, and
        # the label truthfully reflects the crossed threshold.
        assert bot._match_tier(-3.0) == (-2.0, 0.0001)

    def test_resolve_buy_amount_no_tier_amount_on_small_dip(self, bot: KrakenDCA):
        """Prod config shape: the -2% tier amount exceeds the base amount.
        A dip shallower than 2% must yield the BASE amount, not the tier
        amount — otherwise the bot buys 'dynamic' every day."""
        for tier in bot.config.dynamic_dca_tiers:
            if tier.threshold_percent == -2.0:
                tier.amount = 0.00025  # > base (0.0001), as in production
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        assert bot.resolve_buy_amount(current_price=49805.0) == 0.0001  # -0.39%
        assert bot.resolve_buy_amount(current_price=48750.0) == 0.00025  # -2.5%


class TestDynamicTierRecordedOnOrder:
    """Dynamic orders must carry the matched tier for display (Dynamic -5%)."""

    @pytest.fixture
    def bot(self, dynamic_config: Config, tmp_path: Path, monkeypatch):
        store = TransactionStore(filepath=tmp_path / "transactions.json")
        state = BotState(filepath=tmp_path / "state.json", persist=False)
        bot = KrakenDCA(config=dynamic_config, store=store, state=state)
        bot.store.clear()
        monkeypatch.setattr(bot.api, "get_ticker", lambda pair: 47500.0)
        return bot

    def test_dynamic_buy_records_matched_tier(self, bot: KrakenDCA):
        # Reference (last buy) at 50'000; current 47'500 -> -5% tier.
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        bot.execute_buy(strategy="dynamic")
        txn = bot.store.get_transactions("XBTCHF")[-1]
        assert txn.strategy == "dynamic"
        assert txn.dynamic_tier == -5.0

    def test_scheduled_buy_has_no_tier(self, bot: KrakenDCA):
        bot.store.add_transaction("XBTCHF", 0.0001, 50000.0, strategy="scheduled")
        bot.execute_buy(strategy="scheduled")
        txn = bot.store.get_transactions("XBTCHF")[-1]
        assert txn.strategy == "scheduled"
        assert txn.dynamic_tier is None

    def test_dynamic_buy_below_deepest_tier_records_deepest_tier(self, bot: KrakenDCA):
        # Price far below the lowest tier: the deepest tier applies (crash
        # protection) and is recorded for display as "Dynamic -20%".
        bot.store.add_transaction("XBTCHF", 0.0001, 100000.0, strategy="scheduled")
        # 47'500 vs reference 100'000 is -52.5%: beyond the -20% tier.
        bot.execute_buy(strategy="dynamic")
        txn = bot.store.get_transactions("XBTCHF")[-1]
        assert txn.dynamic_tier == -20.0


class TestTransactionDynamicTierPersistence:
    """dynamic_tier must round-trip through serialization."""

    def test_to_from_dict_preserves_tier(self, tmp_path: Path):
        store = TransactionStore(filepath=tmp_path / "transactions.json")
        txn = store.add_transaction("XBTCHF", 0.00015, 47500.0, strategy="dynamic", dynamic_tier=-5.0)
        assert txn.dynamic_tier == -5.0

        reloaded = TransactionStore(filepath=tmp_path / "transactions.json")
        assert reloaded.transactions[-1].dynamic_tier == -5.0

    def test_legacy_records_default_to_none(self, tmp_path: Path):
        from bot.store import Transaction

        txn = Transaction.from_dict(
            {
                "date": "2026-10-01T00:00:00+00:00",
                "trading_pair": "XBTCHF",
                "amount": 0.0001,
                "price": 50000.0,
            }
        )
        assert txn.dynamic_tier is None

