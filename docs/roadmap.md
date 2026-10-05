# Development Roadmap

> This is a living plan. The current priority is **getting the bot running reliably in production**; everything else is queued behind that.

---

## Phase 0 — Production go-live (completed)

**Goal:** Bot is deployed, healthy, and placing real orders without errors.

### Checklist

- [x] Dashboard deployed and accessible
- [x] Pause/resume and manual buy working
- [x] Live trading enabled
- [x] First successful real buy confirmed
- [x] Telegram notifications for live buys confirmed
- [x] `crypto_amount` tuned so `crypto_amount × price` is above Kraken's minimum
- [x] Logs are rotating and readable
- [x] Backups running on a schedule

### Notes

- The first manual buy initially failed with `EGeneral:Invalid arguments:volume minimum not met`.
- `crypto_amount` was increased so the order value exceeds Kraken's minimum.
- A confirmed successful live buy has been recorded.
- Do **not** move to Phase 1 until at least one scheduled buy and one dip buy have completed successfully in live mode.

---

## Phase 1 — Stabilisation & observability (current)

**Goal:** Make the bot boringly reliable before adding features.

### Planned work

- Add external health monitoring (e.g., Uptime Kuma or Prometheus node-exporter) that alerts if the container is unhealthy.
- Alert if no Telegram message is received for an extended period.
- Add a simple “last successful buy” timestamp to the dashboard.
- Review and tighten log retention.
- Test backup restore procedure end-to-end.
- Document runbook for common alerts.

### Success criteria

- Bot runs for 30 days without manual intervention.
- All alerts are actionable and tested.
- Backup restore has been practised once.

---

## Phase 2 — Volatility-adaptive dip buying (ATR)

**Goal:** Replace the fixed `dip_threshold_percent` with a threshold that adapts to market volatility.

### Why ATR

- In calm markets, small dips are mostly noise → require a larger % drop.
- In volatile markets, large moves are normal → accept a smaller % drop.
- It is a natural extension of the existing `% dip` logic without introducing complex indicators.

### Proposed implementation

1. Fetch recent OHLC data (the bot already has `get_ohlc()`).
2. Compute **ATR(14)** from the candles.
3. Calculate adaptive threshold:
   ```
   dynamic_threshold = max(min_threshold, (ATR / current_price) × multiplier)
   ```
   Example:
   - `min_threshold = 3.0` (never buy on less than 3%)
   - `multiplier = 1.5`
4. Compare the drop from the last buy price against `dynamic_threshold` instead of the fixed `dip_threshold_percent`.

### Config changes

Add to `config.json`:

```json
{
  "dip_threshold_mode": "fixed",
  "dip_threshold_percent": 5.0,
  "dip_atr_multiplier": 1.5,
  "dip_atr_period": 14,
  "dip_atr_min_threshold": 3.0
}
```

`dip_threshold_mode` can be `"fixed"` (today's behaviour) or `"atr"`.

### Risks

- Easy to overfit the multiplier on historical data.
- Must keep `dip_buy_cooldown_hours` and `max_monthly_amount` to prevent blow-ups.
- Needs dry-run testing with real market data before going live.

---

## Phase 3 — Multi-indicator dip confirmation

**Goal:** Reduce false dip signals by combining multiple conditions.

### Proposed rule

Trigger a dip buy only when:

1. Price dropped at least `dip_threshold_percent` from last buy, **and**
2. RSI(14) is below `dip_rsi_oversold` (e.g., 35), **and/or**
3. Price is still above the long-term moving average (e.g., 200-period MA).

This prevents buying every small pullback and avoids catching falling knives in a full bear market.

### Config changes

```json
{
  "dip_confirm_rsi": true,
  "dip_rsi_period": 14,
  "dip_rsi_oversold": 35,
  "dip_confirm_ma": true,
  "dip_ma_period": 200
}
```

### Notes

- Requires more OHLC history than ATR alone.
- Should be optional; the bot must still work if only the fixed threshold is used.

---

## Phase 4 — Tiered / laddered dip buys

**Goal:** Deploy more capital as the dip gets deeper.

### Proposed rule

| Drop from last buy | Buy size |
|--------------------|----------|
| -5% | 1× `crypto_amount` |
| -10% | 2× `crypto_amount` |
| -15% | 3× `crypto_amount` |

### Config changes

```json
{
  "dip_ladder": [
    { "drop_percent": 5.0, "multiplier": 1.0 },
    { "drop_percent": 10.0, "multiplier": 2.0 },
    { "drop_percent": 15.0, "multiplier": 3.0 }
  ]
}
```

### Risks

- Can deplete the monthly budget very fast in a crash.
- Must be combined with strict `max_monthly_amount` and a daily/cycle cap on dip buys.

---

## Phase 5 — Multi-pair support

**Goal:** Run DCA for multiple trading pairs from one bot.

### Considerations

- Each pair needs its own `crypto_amount`, `max_monthly_amount`, and possibly its own dip settings.
- Portfolio and P/L calculations must aggregate across pairs.
- Minimum order sizes differ per pair on Kraken.

### Config idea

```json
{
  "portfolios": [
    { "trading_pair": "XBTCHF", "crypto_amount": 0.0001, "allocation_percent": 70 },
    { "trading_pair": "XETHCHF", "crypto_amount": 0.001, "allocation_percent": 30 }
  ]
}
```

---

## Phase 6 — Paper trading mode

**Goal:** Test new strategies without placing real orders.

### Difference from demo mode

- Demo mode uses **mock** Kraken responses.
- Paper trading would use **real** Kraken prices and balances but record simulated buys instead of calling `AddOrder`.

### Use case

Validate ATR or RSI strategies against real market conditions for a few weeks before enabling `LIVE_TRADING_ENABLED`.

---

## Phase 7 — Storage migration to SQLite

**Goal:** Improve reliability for long-running bots.

### Why

- `transactions.json` and `audit_log.json` grow indefinitely.
- JSON files are vulnerable to corruption if the container crashes mid-write.
- SQLite gives atomic writes, indexing, and easier querying.

### Path

A migration script already exists: `scripts/migrate_to_sqlite.py`.

---

## Deferred to post-v1.0

### Pluggable strategy framework

A full `Strategy` ABC, `StrategyRegistry`, `RiskController`, `OrderExecutor`,
and the seven templates previously stored in `data/strategy_templates.json` are
**not** part of v1.0. The current implementation in `bot/core.py` handles the
v1.0 strategy set directly.

This work is deferred until after the bot has proven live trading reliability.
When picked up, it needs its own architecture and migration plan. See
`docs/adr/ADR-001-strategy-framework.md`.

## Phase ordering rationale

| Phase | Priority | Reason |
|-------|----------|--------|
| 0 | Now | Bot must work before anything else |
| 1 | Next | Reliability before complexity |
| 2 | After stabilisation | ATR is the simplest smart improvement |
| 3 | Later | More indicators add complexity |
| 4 | Later | Higher risk; needs careful budgeting |
| 5 | Much later | Major architecture change |
| 6 | Anytime after Phase 0 | Useful for testing strategies safely |
| 7 | When needed | Only if JSON storage becomes a problem |
| Strategy framework | Post-v1.0 | Requires stable core and dedicated design |

---

*Last updated: 2026-07-25*
