# Development Roadmap

> This is a living plan. The bot is **live in production and hardened**
> (security wave S1–S5 shipped in v1.3.0). Open feature work: Phase 2
> (ATR-based volatility adaptation) and Phase 5 (multi-pair — options and
> effort assessed); Phase 3 stays queued behind Phase 2.

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

## Phase 1 — Stabilisation & observability (completed)

**Goal:** Make the bot boringly reliable before adding features.

### Delivered

- External health monitoring: `/api/metrics` bearer-protected endpoint,
  `MONITORING_TOKEN`, and a monitoring runbook
  ([`docs/operations/MONITORING.md`](operations/MONITORING.md)). Standing up
  the external checker (Uptime Kuma/Prometheus) itself is a deployment-side
  task, not a code task.
- Alerting surface: alert rules with acknowledge support in the dashboard,
  alert runbooks ([`INCIDENT_RUNBOOK.md`](operations/INCIDENT_RUNBOOK.md),
  [`ORDER_FAILURE_RUNBOOK.md`](operations/ORDER_FAILURE_RUNBOOK.md)).
- Buy transparency: full buy history with timestamps in the dashboard, last
  buys via Telegram `/last`.
- Log hygiene: rotating JSON logs with retention configuration
  (`RETENTION_DAYS`).
- Backups: scheduled rotation (daily/weekly, configurable counts) and a
  documented [`BACKUP_RESTORE_RUNBOOK.md`](operations/BACKUP_RESTORE_RUNBOOK.md).
  Practising a restore end-to-end remains an operations TODO.

### Success criteria

- [x] All alerts are actionable, with runbooks.
- [x] Backups run on a schedule with rotation.
- [ ] Bot runs for 30 days without manual intervention (continuous).
- [ ] Restore drill performed once (operations TODO, no code change).

---

## Phase 2 — Volatility-adaptive dip buying (ATR)

**Goal:** Replace the fixed `dip_threshold_percent` with a threshold that adapts to market volatility.

> **Status note (2026-10-09):** still open. Dynamic DCA tiers (Phase 4,
> shipped) cover part of the motivation — buy sizes already adapt to the
> price trend — but the *trigger threshold* is still a fixed percentage.
> ATR would make the trigger itself volatility-aware. Assessed in the
> private backlog as the cheapest volatility adaptation before any
> AI-assisted strategy work.

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

## Phase 4 — Tiered / laddered dip buys (completed)

**Goal:** Deploy more capital as the dip gets deeper.

> **Status note (2026-10-09):** shipped as **Dynamic DCA tiers** (v1.1/v1.2)
> and since refined (tier labels on orders, fiat-value display, live Kraken
> ordermin floor). The tier table implements exactly this ladder — as
> absolute amounts per threshold instead of multipliers — with the risk
> mitigations from below already in place: per-pair cooldown between buys,
> the monthly budget checked against the resolved tier amount, and a tier
> amount of `0` to skip buying into rallies. The configuration and the
> resolution rules are documented in
> [`docs/configuration.md`](configuration.md) → *Dynamic DCA tiers*; the
> table is editable in the dashboard (Settings → Strategy).

### Original proposal (kept for reference)

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

> **Status note (2026-10-09):** still open. Assessed in the private backlog
> (options: one container per pair vs. in-app portfolios; recommended v1
> scope; global budget guardrail as the first story). The short-term
> workaround — a second container with its own volumes, budget, and
> Telegram bot — works today without code changes.

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

## Phase 6 — Paper trading mode (completed)

**Goal:** Test new strategies without placing real orders.

> **Status note (2026-10-09):** covered by demo mode as implemented.
> `DemoKrakenAPI` simulates orders and balances but uses **live Kraken
> public prices** (synthetic prices only as a fallback when the public API
> is unreachable). Running with `DEMO_MODE=true` therefore validates
> strategies against real market conditions without any `AddOrder` calls —
> exactly the use case described below. The original demo-vs-paper
> distinction is obsolete.

### Use case

Validate ATR or RSI strategies against real market conditions for a few weeks before enabling `LIVE_TRADING_ENABLED`.

---

## Phase 7 — Storage migration to SQLite (removed)

Removed 2026-10-09 by owner decision: JSON storage is sufficient at the
current scale. Writes are atomic (temp-file + rename) and cross-process
locked, transaction volume is a handful of entries per week, and no
corruption or performance issue has ever been observed. The research
prototype `scripts/migrate_to_sqlite.py` remains in the repository as a
documented future path; the decision record lives in
[`docs/adr/ADR-002-persistence-storage.md`](adr/ADR-002-persistence-storage.md).
Should storage ever become a problem, the phase returns via a new design,
not by default.

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

| Phase | Status | Reason |
|-------|----------|--------|
| 0 | Completed | Bot must work before anything else |
| 1 | Completed | Reliability before complexity |
| 2 | Next candidate | ATR is the simplest smart improvement; dynamic tiers already cover adaptive sizes |
| 3 | Later | More indicators add complexity |
| 4 | Completed | Shipped as dynamic DCA tiers |
| 5 | Later, assessed | Major architecture change; container-per-pair workaround exists |
| 6 | Completed | Demo mode uses live prices with simulated orders |
| 7 | Removed 2026-10-09 | JSON storage sufficient; script kept as documented path (ADR-002) |
| Strategy framework | Post-v1.0 | Requires stable core and dedicated design |

---

*Last updated: 2026-10-09*
