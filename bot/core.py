"""Core DCA trading loop and portfolio calculations."""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from bot.api_client import KrakenAPI
from bot.alerts import create_alert
from bot.colors import Colors
from bot.config import Config
from bot.demo import DemoKrakenAPI, is_demo_mode, seed_demo_transactions
from bot.logger import get_logger
from bot.notifier import Notifier
from bot.order_execution import OrderExecutor, OrderState

from bot.state import BotState, RuntimeOverrides
from bot.store import TransactionStore
from bot.utils import APP_VERSION, add_months, atomic_write_json, now_tz, redact_sensitive, utc_now

# Estimated Kraken taker fee for the lowest volume tier (market order via API).
# Replace with actual fee from QueryOrders if/when that endpoint is integrated.
KRAKEN_TAKER_FEE_RATE = 0.0026

# Minimum crypto order size on Kraken for BTC pairs; used as a safe default.
MIN_ORDER_CRYPTO_AMOUNT = 0.0001

# Number of hours after a missed deposit day during which the bot will still buy
# immediately instead of skipping to the next month. 48 hours is enough to catch
# a weekend restart or delayed deposit without being overly aggressive.
CYCLE_GRACE_HOURS = 48


class KrakenDCA:
    """Main DCA application."""

    def __init__(
        self,
        config: Config | None = None,
        store: TransactionStore | None = None,
        state: BotState | None = None,
        overrides: RuntimeOverrides | None = None,
    ):
        self.config = config or Config()

        # Hard startup guard: staging must explicitly enable DEMO_MODE.
        if (os.environ.get("APP_ENV", "").lower() == "staging" and
                os.environ.get("DEMO_MODE", "").lower() not in ("true", "1", "yes", "on")):
            raise RuntimeError(
                "APP_ENV=staging requires DEMO_MODE=true. Real orders are not permitted in staging."
            )

        if is_demo_mode():
            self.api: KrakenAPI | DemoKrakenAPI = DemoKrakenAPI(self.config.api_key, self.config.api_secret)
        else:
            self.api = KrakenAPI(self.config.api_key, self.config.api_secret)
        self.store = store or TransactionStore()
        self.notifier = Notifier()
        self.state = state or BotState(persist=True)
        self.overrides = overrides or RuntimeOverrides()
        self._stop_event = threading.Event()
        self.order_executor = OrderExecutor(
            api=self.api,
            store=self.store,
            notifier=self.notifier,
            state=self.state,
            config=self.config,
        )

        # Seed last_cycle_at from existing scheduled transactions on first run
        # so a restarted bot can recover the correct cycle instead of jumping
        # blindly to the next calendar month.
        if self.state.persist and self.state.last_cycle_at is None:
            self._seed_last_cycle_from_transactions()

        # Seed demo data if in demo mode
        if is_demo_mode():
            seed_demo_transactions(self.store, pair=self.config.trading_pair)

    def write_health(self):
        """Write a structured heartbeat file for container health checks.

        The file is written atomically and contains only non-sensitive runtime
        metadata. It is updated at startup and after every successful poll cycle.
        """
        heartbeat = {
            "timestamp": utc_now().isoformat(),
            "status": self.state.status,
            "mode": self.state.mode,
            "paused": self.state.paused,
            "last_cycle_at": self.state.last_cycle_at.isoformat() if self.state.last_cycle_at else None,
            "next_cycle_at": self.state.next_cycle_at.isoformat() if self.state.next_cycle_at else None,
            "last_price_at": self.state.last_price_at.isoformat() if self.state.last_price_at else None,
            "last_order_at": self.state.last_order_at.isoformat() if self.state.last_order_at else None,
            "runtime_started_at": self.state.runtime_started_at.isoformat() if self.state.runtime_started_at else None,
            "version": APP_VERSION,
        }
        try:
            atomic_write_json(Path("data/heartbeat.json"), heartbeat, indent=2)
        except Exception:
            # Heartbeat is advisory; never crash the trading loop because of it.
            pass

    def request_stop(self) -> None:
        """Signal the trading loop to shut down gracefully.

        Used by SIGTERM handlers so the container can exit cleanly instead of
        being killed after the stop grace period.
        """
        self._stop_event.set()
        self.state.update(status="stopped")
        self.state.wake()

    def show_banner(self):
        """Display startup banner."""
        banner = (
            f"\n{Colors.CYAN}{Colors.BOLD}"
            "    KRAKEN DCA - Automated Dollar Cost Averaging\n"
            f"{Colors.RESET}\n"
            f"    {Colors.WHITE}Web UI: {Colors.GREEN}enabled{Colors.RESET}\n"
        )
        print(banner)

    def test_connection(self):
        """Test API connection"""
        print(f"{Colors.BOLD}Testing Kraken API Connection...{Colors.RESET}")
        try:
            self.api.test_connection()
            self.state.update(exchange_connected=True)
            print(f"{Colors.GREEN}✓ API Connection Successful{Colors.RESET}\n")
        except Exception as e:
            safe_error = redact_sensitive(str(e))
            self.state.update(exchange_connected=False, last_error=safe_error, last_error_at=now_tz())
            print(f"{Colors.RED}✗ {safe_error}{Colors.RESET}")
            sys.exit(1)

    def get_fiat_currency(self) -> str:
        """Extract fiat currency from trading pair"""
        pair = self.config.trading_pair
        if pair.startswith("X"):
            pair = pair[1:]
        quote_currency = pair[-3:]
        if quote_currency.startswith("Z"):
            quote_currency = quote_currency[1:]
        return quote_currency

    def get_fiat_balance(self, balance: Dict) -> float:
        """Get fiat balance, handling Kraken's Z-prefix format"""
        currency = self.get_fiat_currency()
        return balance.get(f"Z{currency}", balance.get(currency, 0.0))

    def get_reference_price(self) -> float:
        """Return the price reference used for dynamic DCA tier matching."""
        total_amount, avg_price, last_price, _ = self.store.get_statistics(
            self.config.trading_pair
        )
        if self.config.dynamic_dca_reference == "avg_buy":
            return avg_price if avg_price > 0 else last_price
        return last_price

    def _match_tier(self, price_change_percent: float) -> Tuple[Optional[float], float]:
        """Return (threshold_percent, amount) of the first matching tier.

        ``threshold_percent`` is None when no tier matched and the base amount
        is used as fallback.
        """
        for tier in sorted(
            self.config.dynamic_dca_tiers,
            key=lambda t: t.threshold_percent,
            reverse=True,
        ):
            if not tier.enabled:
                continue
            if price_change_percent >= tier.threshold_percent:
                return tier.threshold_percent, tier.amount
        return None, self.config.crypto_amount

    def resolve_buy_amount(self, current_price: float) -> float:
        """Return the crypto amount to buy for the current market conditions.

        When dynamic DCA is disabled this always returns the configured
        crypto_amount. When enabled, it matches the price change against the
        configured tiers and returns the matching tier amount (0 means skip).
        """
        if not self.config.dynamic_dca_enabled:
            return self.config.crypto_amount

        reference_price = self.get_reference_price()
        if reference_price <= 0:
            # No previous purchase: use the base amount.
            return self.config.crypto_amount

        price_change_percent = (current_price - reference_price) / reference_price * 100.0
        _, amount = self._match_tier(price_change_percent)
        return amount

    def _last_buy_time(self) -> Optional[datetime]:
        """Return the timestamp of the most recent transaction, if any."""
        pair = self.config.trading_pair
        if self.store.get_transaction_count(pair) == 0:
            return None
        last_tx = self.store.get_transactions(pair)[-1]
        return datetime.fromisoformat(last_tx.date)

    def _dynamic_cooldown_ok(self) -> bool:
        """Return True if the dynamic-buy cooldown has passed since the last buy."""
        last_buy = self._last_buy_time()
        if last_buy is None:
            return True
        return (now_tz() - last_buy).total_seconds() / 3600 >= self.config.dynamic_dca_cooldown_hours

    def _seed_last_cycle_from_transactions(self) -> None:
        """Set last_cycle_at from the most recent scheduled transaction, if any."""
        pair_txs = [
            t
            for t in self.store.get_transactions(self.config.trading_pair)
            if t.strategy == "scheduled"
        ]
        if not pair_txs:
            return
        last_tx = max(pair_txs, key=lambda t: datetime.fromisoformat(t.date))
        self.state.update(last_cycle_at=datetime.fromisoformat(last_tx.date))

    def _normalize_cycle_time(self, dt: datetime) -> datetime:
        """Snap a datetime to the configured deposit day and buy hour."""
        return dt.replace(
            day=self.config.deposit_day,
            hour=self.config.buy_hour,
            minute=0,
            second=0,
            microsecond=0,
        )

    def _require_end_date(self) -> datetime:
        """Return the configured DCA end date as a non-optional datetime.

        Config validation already requires a future end date for lump-sum mode;
        this helper makes that invariant visible to type checkers and raises a
        clear error if the invariant is ever violated at runtime.
        """
        end_date = self.config.dca_end_date
        if end_date is None:
            raise RuntimeError(
                "dca_end_date is required when mode is 'lump_sum' (expected by calculate_next_buy)"
            )
        return end_date

    def _next_cycle_after(self, last_cycle: datetime) -> datetime:
        """Return the first expected cycle boundary after ``last_cycle``."""
        candidate = self._normalize_cycle_time(last_cycle)
        if candidate > last_cycle:
            return candidate
        return add_months(candidate, 1)

    def _mark_cycle_executed(self, cycle_time: Optional[datetime], strategy: str) -> None:
        """Persist the cycle time once a scheduled buy has actually been recorded."""
        if strategy == "scheduled" and cycle_time is not None:
            self.state.update(last_cycle_at=cycle_time)

    def calculate_next_buy(self) -> Tuple[datetime, float, int, int]:
        """Calculate next buy time based on mode.

        In recurring mode the bot uses the persisted ``last_cycle_at`` to recover
        the real cycle boundary. If a cycle was missed (e.g. the bot was down) and
        we are still within ``CYCLE_GRACE_HOURS`` of the boundary, the buy is
        scheduled immediately instead of being silently skipped to the next month.

        Returns: (next_buy_datetime, hours_until_buy, remaining_hours_in_period, max_buys)
        """
        now = now_tz()
        buy_hour = self.config.buy_hour

        if self.config.mode == "lump_sum":
            period_end = self._require_end_date()
            if now >= period_end:
                print(f"{Colors.YELLOW}DCA end date reached ({period_end.strftime('%Y-%m-%d')}), no more scheduled buys.{Colors.RESET}")
                return period_end, 0.0, 0, 0
            remaining_hours = (period_end - now).total_seconds() / 3600
        else:
            if self.state.last_cycle_at is not None:
                next_cycle = self._next_cycle_after(self.state.last_cycle_at)
                grace = timedelta(hours=CYCLE_GRACE_HOURS)
                # Skip cycles that were missed by more than the grace window.
                while next_cycle < now and next_cycle + grace < now:
                    next_cycle = add_months(next_cycle, 1)
                period_end = next_cycle
            else:
                current_day = now.day
                if current_day < self.config.deposit_day:
                    period_end = now.replace(day=self.config.deposit_day, hour=buy_hour, minute=0, second=0, microsecond=0)
                elif current_day == self.config.deposit_day and now.hour <= buy_hour:
                    period_end = now.replace(hour=buy_hour, minute=0, second=0, microsecond=0)
                elif now.month == 12:
                    period_end = now.replace(year=now.year + 1, month=1, day=self.config.deposit_day, hour=buy_hour, minute=0, second=0, microsecond=0)
                else:
                    period_end = now.replace(month=now.month + 1, day=self.config.deposit_day, hour=buy_hour, minute=0, second=0, microsecond=0)
            remaining_hours = (period_end - now).total_seconds() / 3600

        # Get current price and available balance
        current_price = self.api.get_ticker(self.config.trading_pair)
        balance = self.api.get_balance()
        quote_currency = self.get_fiat_currency()
        available_fiat = self.get_fiat_balance(balance)

        if available_fiat <= 0:
            print(f"{Colors.YELLOW}Warning: No {quote_currency} balance available{Colors.RESET}")
            return period_end, 24.0, int(remaining_hours), 0

        cost_per_buy = self.config.crypto_amount * current_price
        max_buys_balance = int(available_fiat / cost_per_buy)

        if max_buys_balance <= 0:
            print(f"{Colors.YELLOW}Warning: Insufficient balance for buy (need {cost_per_buy:.2f} {quote_currency}){Colors.RESET}")
            return period_end, 24.0, int(remaining_hours), 0

        max_buys = max_buys_balance
        if self.config.max_monthly_amount is not None:
            monthly_spent = self.store.get_monthly_spent(
                self.config.trading_pair, self.config.deposit_day, self.config.buy_hour
            )
            remaining_budget = max(0.0, self.config.max_monthly_amount - monthly_spent)
            max_buys_budget = int(remaining_budget / cost_per_buy)
            if max_buys_budget < max_buys:
                max_buys = max_buys_budget
                print(f"{Colors.YELLOW}Max monthly budget limits buys to {max_buys} this cycle ({remaining_budget:.2f} {quote_currency} remaining){Colors.RESET}")
            if max_buys <= 0:
                print(f"{Colors.YELLOW}Warning: Monthly budget reached ({monthly_spent:.2f} / {self.config.max_monthly_amount:.2f} {quote_currency}){Colors.RESET}")
                return period_end, 24.0, int(remaining_hours), 0

        if period_end <= now:
            # Missed-cycle catch-up: buy immediately.
            next_buy_time = now
            hours_between_buys = 0.0
            remaining_hours = 0
        else:
            hours_between_buys = remaining_hours / max_buys
            next_buy_time = now + timedelta(hours=hours_between_buys)

        return next_buy_time, hours_between_buys, int(remaining_hours), max(1, max_buys)

    def execute_buy(self, strategy: str = "scheduled", cycle_time: Optional[datetime] = None):
        """Execute a buy order via the idempotent order executor.

        ``cycle_time`` is the scheduled cycle time that triggered this buy. When
        a scheduled buy is successfully recorded, it is persisted as
        ``last_cycle_at`` so the bot can recover the correct cycle boundary after
        a restart.

        Dry-run/simulated orders are recorded directly without calling the real
        Kraken AddOrder endpoint. Only production live orders flow through the
        full order-attempt state machine.
        """
        try:
            if self.config.mode == "lump_sum":
                end_date = self._require_end_date()
                if now_tz() >= end_date:
                    print(f"{Colors.YELLOW}⚠ Buy skipped: DCA end date {end_date.strftime('%Y-%m-%d')} has been reached{Colors.RESET}")
                    return

            # Get current price
            current_price = self.api.get_ticker(self.config.trading_pair)
            self.state.update(last_price=current_price, last_price_at=now_tz())

            effective_max_price = self.overrides.temporary_max_price or self.config.max_price
            if effective_max_price is not None and current_price > effective_max_price:
                print(f"{Colors.YELLOW}⚠ Buy skipped: price {current_price:.2f} is above max_price {effective_max_price:.2f}{Colors.RESET}")
                return

            if self.config.max_monthly_amount is not None:
                monthly_spent = self.store.get_monthly_spent(self.config.trading_pair, self.config.deposit_day, self.config.buy_hour)
                buy_cost = self.config.crypto_amount * current_price
                if monthly_spent + buy_cost > self.config.max_monthly_amount:
                    print(f"{Colors.YELLOW}⚠ Buy skipped: monthly spend {monthly_spent:.2f} + {buy_cost:.2f} would exceed limit {self.config.max_monthly_amount:.2f}{Colors.RESET}")
                    return

            buy_amount = self.resolve_buy_amount(current_price)

            if buy_amount <= 0:
                print(f"{Colors.YELLOW}⚠ Buy skipped: dynamic tier amount is 0 for current price {current_price:.2f}{Colors.RESET}")
                return

            if buy_amount < MIN_ORDER_CRYPTO_AMOUNT:
                print(f"{Colors.YELLOW}⚠ Buy skipped: resolved amount {buy_amount:.8f} is below minimum order size{Colors.RESET}")
                return

            # Record which dynamic tier triggered (for display as "Dynamic -5%").
            dynamic_tier: Optional[float] = None
            if strategy == "dynamic" and self.config.dynamic_dca_enabled:
                reference_price = self.get_reference_price()
                if reference_price > 0:
                    change = (current_price - reference_price) / reference_price * 100.0
                    tier_threshold, _ = self._match_tier(change)
                    dynamic_tier = tier_threshold

            if self.config.max_monthly_amount is not None:
                monthly_spent = self.store.get_monthly_spent(self.config.trading_pair, self.config.deposit_day, self.config.buy_hour)
                buy_cost = buy_amount * current_price
                if monthly_spent + buy_cost > self.config.max_monthly_amount:
                    print(f"{Colors.YELLOW}⚠ Buy skipped: monthly spend {monthly_spent:.2f} + {buy_cost:.2f} would exceed limit {self.config.max_monthly_amount:.2f}{Colors.RESET}")
                    return

            fiat_currency = self.get_fiat_currency()
            simulated = not self.config.live_trading_enabled
            order_number = self.store.get_transaction_count(self.config.trading_pair) + 1

            print(f"\n{Colors.BOLD}Executing buy order #{order_number}...{Colors.RESET}")
            print(f"  Amount: {buy_amount:.8f} BTC at {current_price:.2f} {fiat_currency}")

            trade_value = buy_amount * current_price
            estimated_fee = trade_value * KRAKEN_TAKER_FEE_RATE

            if simulated:
                print(f"{Colors.YELLOW}⚠ DRY RUN: skipped placing order (LIVE_TRADING_ENABLED is not true){Colors.RESET}")
                self.notifier.send(
                    f"🔶 DRY RUN buy\n"
                    f"Order #{order_number}\n"
                    f"Amount: {buy_amount:.8f} BTC\n"
                    f"Price: {current_price:.2f} {fiat_currency}\n"
                    f"No real order was placed."
                )
                self.store.add_transaction(
                    self.config.trading_pair,
                    buy_amount,
                    current_price,
                    fee=estimated_fee,
                    strategy=strategy,
                    simulated=True,
                    notes="Dry-run buy",
                    dynamic_tier=dynamic_tier,
                )
                self._mark_cycle_executed(cycle_time, strategy)
                self.display_statistics(current_price)
                return

            # Live order: submit through the executor with idempotency + retry.
            def _on_confirmed(attempt):
                self._mark_cycle_executed(cycle_time, strategy)
                print(f"{Colors.GREEN}✓ Order placed successfully{Colors.RESET}")
                self.notifier.send(
                    f"✅ Buy order placed\n"
                    f"Order #{order_number}\n"
                    f"Amount: {buy_amount:.8f} BTC\n"
                    f"Price: {current_price:.2f} {fiat_currency}\n"
                    f"Total: {(buy_amount * current_price):.2f} {fiat_currency}"
                )
                self.display_statistics(current_price)

            attempt = self.order_executor.submit_buy(
                pair=self.config.trading_pair,
                amount=buy_amount,
                price=current_price,
                strategy=strategy,
                simulated=False,
                cycle_time=cycle_time,
                dynamic_tier=dynamic_tier,
                on_confirmed=_on_confirmed,
            )

            if attempt.state == OrderState.CONFIRMED.value:
                # on_confirmed already handled confirmation messaging.
                pass
            elif attempt.state == OrderState.HOLD.value:
                print(f"{Colors.RED}✗ Order attempt on HOLD: {attempt.final_outcome}{Colors.RESET}")
            elif attempt.state == OrderState.UNKNOWN.value:
                print(f"{Colors.YELLOW}⚠ Order outcome unknown; reconciling before retry{Colors.RESET}")
            elif attempt.state in (OrderState.FAILED_TRANSIENT.value, OrderState.RETRY_SCHEDULED.value):
                next_retry = attempt.next_retry_at or "soon"
                print(f"{Colors.YELLOW}⚠ Order attempt failed transiently; retry scheduled at {next_retry}{Colors.RESET}")
            elif attempt.state == OrderState.SUBMITTED.value:
                print(f"{Colors.CYAN}⏳ Order submitted, awaiting confirmation{Colors.RESET}")

        except Exception as e:
            safe_error = redact_sensitive(str(e))
            get_logger().error(f"Buy execution error: {safe_error}")
            print(f"{Colors.RED}✗ Error executing buy: {safe_error}{Colors.RESET}")
            self.state.update(last_error=safe_error, last_error_at=now_tz())
            self.state.warning(safe_error)
            create_alert(f"Buy failed: {safe_error}", "error")
            self.notifier.send(f"❌ Buy error\n{safe_error}")

    def display_statistics(self, current_price: float, next_buy_time: Optional[datetime] = None):
        """Display trading statistics in table format"""
        total_amount, avg_price, last_price, total_spent = self.store.get_statistics(
            self.config.trading_pair
        )

        if total_amount == 0:
            return

        # Get fiat currency
        fiat_currency = self.get_fiat_currency()

        # Calculate P/L
        current_value = total_amount * current_price
        pl_fiat = current_value - total_spent
        pl_percent = (pl_fiat / total_spent * 100) if total_spent > 0 else 0

        # Determine color
        color = Colors.GREEN if pl_fiat >= 0 else Colors.RED

        if self.config.mode == "lump_sum":
            end_date = self._require_end_date()
            days_total = max(1, (end_date - now_tz()).days)
            mode_line = f"Mode: Lump Sum  |  DCA End Date: {end_date.strftime('%Y-%m-%d')}  |  {days_total}d remaining"
        else:
            mode_line = f"Mode: Recurring  |  Deposit Day: {self.config.deposit_day}  |  Buy Hour: {self.config.buy_hour:02d}:00"

        print(f"\n{Colors.BOLD}{'='*110}{Colors.RESET}")
        print(f"{Colors.BOLD}{'PORTFOLIO SUMMARY':<110}{Colors.RESET}")
        print(f"{Colors.CYAN}{mode_line:<110}{Colors.RESET}")
        print(f"{Colors.BOLD}{'='*110}{Colors.RESET}")

        # Table header - First row with fixed column widths
        print(f"{Colors.BOLD}{'Crypto Amount':<30}{'Avg Buy Price (' + fiat_currency + ')':<30}{'Last Buy Price (' + fiat_currency + ')':<30}{'P/L %':<20}{Colors.RESET}")
        print(f"{'-'*110}")

        # Table data - First row with matching column widths
        pl_percent_str = f"{pl_percent:.2f}%"
        print(f"{total_amount:<30.8f}{avg_price:<30.2f}{last_price:<30.2f}{color}{pl_percent_str:<20}{Colors.RESET}")

        # Table header - Second row with fixed column widths
        print(f"\n{Colors.BOLD}{'Current Price (' + fiat_currency + ')':<30}{'Total Invested (' + fiat_currency + ')':<30}{'Current Value (' + fiat_currency + ')':<30}{'P/L Fiat (' + fiat_currency + ')':<20}{Colors.RESET}")
        print(f"{'-'*110}")

        # Table data - Second row with matching column widths
        print(f"{current_price:<30.2f}{total_spent:<30.2f}{current_value:<30.2f}{color}{pl_fiat:<20.2f}{Colors.RESET}")

        # Table header - Third row: spend info and end condition
        max_price_str = f"{self.config.max_price:.2f}" if self.config.max_price else "disabled"
        if self.config.mode == "lump_sum":
            end_date = self._require_end_date()
            days_left = max(0, (end_date - now_tz()).days)
            end_date_str = f"{end_date.strftime('%Y-%m-%d')} ({days_left}d left)"
            print(f"\n{Colors.BOLD}{'DCA End Date':<55}{'Max Buy Price (' + fiat_currency + ')':<55}{Colors.RESET}")
            print(f"{'-'*110}")
            print(f"{Colors.CYAN}{end_date_str:<55}{max_price_str:<55}{Colors.RESET}")
        else:
            monthly_spent = self.store.get_monthly_spent(self.config.trading_pair, self.config.deposit_day, self.config.buy_hour)
            cycle_spent_str = f"{monthly_spent:.2f}"
            if self.config.max_monthly_amount is not None:
                cycle_spent_str += f" / {self.config.max_monthly_amount:.2f}"
            print(f"\n{Colors.BOLD}{'Cycle Spent (' + fiat_currency + ')':<55}{'Max Buy Price (' + fiat_currency + ')':<55}{Colors.RESET}")
            print(f"{'-'*110}")
            print(f"{Colors.CYAN}{cycle_spent_str:<55}{max_price_str:<55}{Colors.RESET}")

        # Table header - Fourth row: next buy schedule
        if next_buy_time is not None:
            now = now_tz()
            hours_until_buy = max(0.0, (next_buy_time - now).total_seconds() / 3600)
            formatted_next_buy = next_buy_time.strftime("%Y-%m-%d %H:%M:%S %Z")
            print(f"\n{Colors.BOLD}{'Next Scheduled Buy':<55}{'Hours Until Next Buy':<55}{Colors.RESET}")
            print(f"{'-'*110}")
            print(f"{Colors.CYAN}{formatted_next_buy:<55}{hours_until_buy:<55.1f}{Colors.RESET}")

        print(f"{Colors.BOLD}{'='*110}{Colors.RESET}\n")

    def _check_runtime_overrides(self) -> bool:
        """Reload runtime overrides and return True if a manual cycle is requested."""
        self.overrides._load()
        if self.overrides.stop_requested:
            self._stop_event.set()
            self.overrides.clear_stop()
            return False
        if self.overrides.paused:
            self.state.update(paused=True, status="paused")
            return False
        self.state.update(paused=False)
        if self.overrides.manual_cycle_requested:
            self.overrides.clear_manual_cycle()
            return True
        return False

    def run(self):
        """Main application loop"""
        self.show_banner()

        if not self.config.configured:
            # First boot (or credentials removed): keep the process and the
            # web dashboard alive, but do not touch the exchange until API
            # credentials have been provided via the dashboard. The config
            # object is shared with the web app and re-evaluated on reload,
            # so saving credentials (or settings) wakes the bot without a
            # restart.
            print(
                f"{Colors.YELLOW}{Colors.BOLD}Kraken API credentials not configured.{Colors.RESET}"
            )
            print(
                f"{Colors.YELLOW}The bot is idling. Open the dashboard and add your API keys "
                f"(Settings -> API Keys) to start.{Colors.RESET}\n"
            )
            self.state.update(status="waiting")
            while (
                not self._stop_event.is_set()
                and not getattr(self.config, "configured", False)
            ):
                self._stop_event.wait(5)
            if self._stop_event.is_set():
                return
            print(f"{Colors.GREEN}Credentials configured — starting the trading loop.{Colors.RESET}\n")

        self.test_connection()

        print(f"{Colors.BOLD}Configuration:{Colors.RESET}")
        print(f"  Mode: {Colors.CYAN}{'Lump Sum' if self.config.mode == 'lump_sum' else 'Recurring'}{Colors.RESET}")
        print(f"  Trading Pair: {Colors.CYAN}{self.config.trading_pair}{Colors.RESET}")
        if self.config.mode == "lump_sum":
            print(f"  DCA End Date: {Colors.CYAN}{self.config.dca_end_date.strftime('%Y-%m-%d')}{Colors.RESET}")
        else:
            print(f"  Deposit Day: {Colors.CYAN}{self.config.deposit_day} at {self.config.buy_hour}:00{Colors.RESET}")
        print(f"  Crypto Amount per Buy: {Colors.CYAN}{self.config.crypto_amount}{Colors.RESET}")
        if self.config.dynamic_dca_enabled:
            print(f"  Dynamic DCA: {Colors.CYAN}enabled ({self.config.dynamic_dca_reference}){Colors.RESET}")
            print(f"  Dynamic Cooldown: {Colors.CYAN}{self.config.dynamic_dca_cooldown_hours}h{Colors.RESET}")
        else:
            print(f"  Dip Threshold: {Colors.CYAN}{self.config.dip_threshold_percent}%{Colors.RESET}")
            print(f"  Dip Buy Cooldown: {Colors.CYAN}{self.config.dip_buy_cooldown_hours}h{Colors.RESET}")
        print(f"  Max Price: {Colors.CYAN}{self.config.max_price:.2f} {self.get_fiat_currency()}{Colors.RESET}" if self.config.max_price else f"  Max Price: {Colors.CYAN}disabled{Colors.RESET}")
        print(f"  Max Monthly: {Colors.CYAN}{self.config.max_monthly_amount:.2f} {self.get_fiat_currency()}{Colors.RESET}" if self.config.max_monthly_amount else f"  Max Monthly: {Colors.CYAN}disabled{Colors.RESET}")
        print(f"  Poll Interval: {Colors.CYAN}{self.config.poll_interval_seconds}s{Colors.RESET}\n")

        # Display existing portfolio if we have transactions
        total_amount, _, _, _ = self.store.get_statistics(self.config.trading_pair)
        if total_amount > 0:
            print(f"{Colors.BOLD}Loading existing portfolio...{Colors.RESET}")
            try:
                current_price = self.api.get_ticker(self.config.trading_pair)
                next_buy_preview, _, _, _ = self.calculate_next_buy()
                self.display_statistics(current_price, next_buy_preview)
            except Exception as e:
                safe_error = redact_sensitive(str(e))
                print(f"{Colors.YELLOW}Warning: Could not fetch current price: {safe_error}{Colors.RESET}\n")

        if self.config.live_trading_enabled:
            print(f"{Colors.RED}{Colors.BOLD}⚠ LIVE TRADING ENABLED — real orders will be placed{Colors.RESET}\n")
        else:
            print(f"{Colors.GREEN}Dry-run / validation mode — no real orders will be placed{Colors.RESET}\n")

        mode = "Lump Sum" if self.config.mode == "lump_sum" else "Recurring"
        self.notifier.send(
            f"🤖 Crypto Agent started\n"
            f"Mode: {mode}\n"
            f"Pair: {self.config.trading_pair}\n"
            f"Amount: {self.config.crypto_amount}\n"
            f"Live trading: {'ON' if self.config.live_trading_enabled else 'DRY RUN'}"
        )

        self.state.update(
            status="running",
            mode=self.config.mode,
            simulated=not self.config.live_trading_enabled,
            live_trading_enabled=self.config.live_trading_enabled,
            runtime_started_at=now_tz(),
            telegram_connected=self.notifier.enabled,
        )

        print(f"{Colors.GREEN}Application started successfully!{Colors.RESET}")
        print(f"{Colors.YELLOW}Press Ctrl+C to stop{Colors.RESET}\n")

        try:
            self.write_health()
            last_dip_buy_time = None

            while not self._stop_event.is_set():
                # Advance pending order retries and reconciliation without blocking.
                self.order_executor.process_pending()
                self._check_runtime_overrides()
                if self.state.paused:
                    self.state.update(status="paused")
                    self.write_health()
                    # Wait interruptibly so resume/stop/manual requests wake the bot immediately.
                    self.state._wake_event.wait(self.config.poll_interval_seconds)
                    self.state._wake_event.clear()
                    if self._stop_event.is_set():
                        continue
                    continue

                self.state.update(status="waiting")
                next_buy_time, hours_until_buy, remaining_hours, max_buys = self.calculate_next_buy()
                self.state.update(next_cycle_at=next_buy_time, estimated_buys=max_buys)

                # Get current balance and calculate stats
                current_price = self.api.get_ticker(self.config.trading_pair)
                self.state.update(last_price=current_price, last_price_at=now_tz())
                balance = self.api.get_balance()
                fiat_currency = self.get_fiat_currency()
                available_fiat = self.get_fiat_balance(balance)

                # Calculate estimated buy actions
                cost_per_buy = self.config.crypto_amount * current_price
                estimated_buys = int(available_fiat / cost_per_buy) if cost_per_buy > 0 else 0

                # Get last buy price for dip detection
                _, _, last_buy_price, _ = self.store.get_statistics(self.config.trading_pair)
                dip_factor = 1.0 - (self.config.dip_threshold_percent / 100.0)
                dip_threshold = last_buy_price * dip_factor if last_buy_price > 0 else 0

                # Format the next buy time with timezone
                formatted_time = next_buy_time.strftime("%Y-%m-%d %H:%M:%S %Z")
                poll_minutes = self.config.poll_interval_seconds // 60

                print(f"{Colors.BOLD}Next scheduled buy: {Colors.CYAN}{formatted_time}{Colors.RESET}")
                print(f"Current price: {Colors.CYAN}{current_price:.2f} {fiat_currency}{Colors.RESET}")
                if self.config.dynamic_dca_enabled:
                    reference_price = self.get_reference_price()
                    change_pct = ((current_price - reference_price) / reference_price * 100) if reference_price > 0 else 0.0
                    resolved = self.resolve_buy_amount(current_price)
                    print(f"Dynamic reference: {Colors.CYAN}{reference_price:.2f} {fiat_currency}{Colors.RESET}")
                    print(f"Price change: {Colors.CYAN}{change_pct:+.1f}%{Colors.RESET}")
                    print(f"Resolved buy amount: {Colors.CYAN}{resolved:.8f} BTC{Colors.RESET}")
                    print(f"Dynamic cooldown: {Colors.CYAN}{self.config.dynamic_dca_cooldown_hours}h{Colors.RESET}")
                else:
                    print(f"Dip buy threshold ({self.config.dip_threshold_percent}%): {Colors.CYAN}{dip_threshold:.2f} {fiat_currency}{Colors.RESET}")
                    print(f"Dip buy cooldown: {Colors.CYAN}{self.config.dip_buy_cooldown_hours}h{Colors.RESET}")
                print(f"Current fiat available: {Colors.CYAN}{available_fiat:.2f} {fiat_currency}{Colors.RESET}")
                print(f"Estimated buy actions till deposit day: {Colors.CYAN}{estimated_buys}{Colors.RESET}")
                print(f"Remaining hours until deposit day: {Colors.CYAN}{remaining_hours}{Colors.RESET}")
                print(f"Monitoring every {poll_minutes} minutes...\n")

                # Track balance for deposit detection
                previous_fiat = available_fiat

                # Poll until next buy time
                last_check_time = time.monotonic()
                while not self._stop_event.is_set():
                    # Wake every second so manual cycle / pause / stop requests
                    # are handled quickly, while still limiting Kraken API polls to
                    # the configured interval.
                    if self._stop_event.wait(1):
                        break

                    # Check runtime overrides each wake so Buy Now / Pause / Resume react quickly
                    if self._check_runtime_overrides():
                        print(f"{Colors.BOLD}Manual cycle requested.{Colors.RESET}")
                        self.execute_buy(strategy="manual")
                        break

                    if self.state.paused:
                        break

                    now = now_tz()

                    # Check if it's time for the scheduled buy
                    if now >= next_buy_time:
                        print(f"{Colors.BOLD}Scheduled buy time reached.{Colors.RESET}")
                        self.execute_buy(strategy="scheduled", cycle_time=next_buy_time)
                        break

                    if time.monotonic() - last_check_time < self.config.poll_interval_seconds:
                        continue
                    last_check_time = time.monotonic()

                    try:
                        current_price = self.api.get_ticker(self.config.trading_pair)
                        self.state.update(last_price=current_price, last_price_at=now_tz())
                        balance = self.api.get_balance()
                        available_fiat = self.get_fiat_balance(balance)

                        if self.config.dynamic_dca_enabled:
                            # Dynamic tier extra buy: trigger when price dropped into a tier
                            # that calls for a larger-than-base purchase and cooldown passed.
                            base_amount = self.config.crypto_amount
                            target_amount = self.resolve_buy_amount(current_price)
                            if target_amount > base_amount and self._dynamic_cooldown_ok():
                                print(f"\n{Colors.MAGENTA}{Colors.BOLD}DYNAMIC BUY TRIGGERED!{Colors.RESET} "
                                      f"Price {current_price:.2f} resolves to buy amount {target_amount:.8f} BTC (base {base_amount:.8f})")
                                self.execute_buy(strategy="dynamic")
                                # Recalculate schedule after dynamic buy
                                next_buy_time, hours_until_buy, remaining_hours, max_buys = self.calculate_next_buy()
                                self.state.update(next_cycle_at=next_buy_time, estimated_buys=max_buys)
                                formatted_time = next_buy_time.strftime("%Y-%m-%d %H:%M:%S %Z")
                                print(f"{Colors.BOLD}Recalculated next buy: {Colors.CYAN}{formatted_time}{Colors.RESET}\n")
                            elif target_amount > base_amount and not self._dynamic_cooldown_ok():
                                last_buy = self._last_buy_time()
                                remaining_cooldown = self.config.dynamic_dca_cooldown_hours - (now - last_buy).total_seconds() / 3600  # type: ignore[operator]
                                print(f"[{now.strftime('%H:%M:%S')}] Dynamic tier triggered but cooldown active ({remaining_cooldown:.1f}h remaining)")
                        else:
                            # Legacy dip detection: buy if price dropped below threshold
                            _, _, last_buy_price, _ = self.store.get_statistics(self.config.trading_pair)
                            dip_factor = 1.0 - (self.config.dip_threshold_percent / 100.0)
                            dip_threshold = last_buy_price * dip_factor if last_buy_price > 0 else 0

                            if dip_threshold > 0 and current_price <= dip_threshold:
                                cooldown_ok = (
                                    last_dip_buy_time is None or
                                    (now - last_dip_buy_time).total_seconds() / 3600 >= self.config.dip_buy_cooldown_hours
                                )
                                if cooldown_ok:
                                    print(f"\n{Colors.MAGENTA}{Colors.BOLD}DIP DETECTED!{Colors.RESET} "
                                          f"Price {current_price:.2f} is ≥{self.config.dip_threshold_percent}% below last buy price {last_buy_price:.2f}")
                                    self.execute_buy(strategy="dip")
                                    last_dip_buy_time = now
                                    # Recalculate schedule after dip buy
                                    next_buy_time, hours_until_buy, remaining_hours, max_buys = self.calculate_next_buy()
                                    self.state.update(next_cycle_at=next_buy_time, estimated_buys=max_buys)
                                    formatted_time = next_buy_time.strftime("%Y-%m-%d %H:%M:%S %Z")
                                    print(f"{Colors.BOLD}Recalculated next buy: {Colors.CYAN}{formatted_time}{Colors.RESET}\n")
                                else:
                                    remaining_cooldown = self.config.dip_buy_cooldown_hours - (now - last_dip_buy_time).total_seconds() / 3600  # type: ignore[operator]
                                    print(f"[{now.strftime('%H:%M:%S')}] Dip detected but cooldown active ({remaining_cooldown:.1f}h remaining)")

                            print(f"[{now.strftime('%H:%M:%S')}] Price: {current_price:.2f} | "
                                  f"Dip at: {dip_threshold:.2f} | "
                                  f"Balance: {available_fiat:.2f} {fiat_currency}")

                        # Deposit detection: new fiat arrived
                        if available_fiat > previous_fiat:
                            deposit_amount = available_fiat - previous_fiat
                            print(f"\n{Colors.GREEN}{Colors.BOLD}NEW DEPOSIT DETECTED!{Colors.RESET} "
                                  f"+{deposit_amount:.2f} {fiat_currency} "
                                  f"(balance: {available_fiat:.2f} {fiat_currency})")
                            previous_fiat = available_fiat
                            # Recalculate schedule with new balance
                            next_buy_time, hours_until_buy, remaining_hours, max_buys = self.calculate_next_buy()
                            self.state.update(next_cycle_at=next_buy_time, estimated_buys=max_buys)
                            formatted_time = next_buy_time.strftime("%Y-%m-%d %H:%M:%S %Z")
                            print(f"{Colors.BOLD}Recalculated next buy: {Colors.CYAN}{formatted_time}{Colors.RESET}\n")

                        if self.config.dynamic_dca_enabled:
                            print(f"[{now.strftime('%H:%M:%S')}] Price: {current_price:.2f} | "
                                  f"Balance: {available_fiat:.2f} {fiat_currency}")
                        self.display_statistics(current_price, next_buy_time)
                        self.write_health()

                    except Exception as e:
                        safe_error = redact_sensitive(str(e))
                        get_logger().warning(f"Check cycle error: {safe_error}")
                        self.state.update(last_error=safe_error, last_error_at=now_tz())
                        self.state.warning(safe_error)
                        create_alert(f"Check cycle error: {safe_error}", "error")
                        print(f"{Colors.YELLOW}Warning: Check cycle error: {safe_error}{Colors.RESET}")

        except KeyboardInterrupt:
            print(f"\n\n{Colors.YELLOW}Application stopped by user{Colors.RESET}")
            self.state.update(status="stopped")
            sys.exit(0)

        self.state.update(status="stopped")
