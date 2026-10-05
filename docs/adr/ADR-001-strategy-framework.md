# ADR-001: Defer the Pluggable Strategy Framework to Post-v1.0

## Status

Accepted — 2026-07-25

## Context

The repository contains a number of artifacts that suggest a pluggable strategy framework:

- Empty directories `bot/strategies/` and `tests/strategies/`.
- `data/strategy_templates.json` with seven strategy templates.
- `docs/staging-implementation-plan.md` sections describing a `Strategy` ABC,
  `StrategyContext`, `BuySignal`, `StrategyRegistry`, `OrderExecutor`, and
  `RiskController`.
- References to strategy preview/simulation endpoints and template CRUD.

However, no runtime code currently loads or executes these templates. The
implemented trading behavior lives in `bot/core.py` (`KrakenDCA`) and
`bot/order_execution.py` and consists of:

- Recurring DCA (monthly deposit-day cycle).
- Lump-sum DCA (spread until a target end date).
- Dip buying (extra buy when price drops below a configured threshold).
- Dynamic DCA tiers (increasing buy size at configured drawdown levels).

Production hardening has been completed and the first successful live
Kraken purchase has been confirmed.

## Decision

For **Production Hardened v1.0**, the pluggable strategy framework will **not**
be implemented. The existing, tested strategy logic in `bot/core.py` will remain
unchanged.

The strategy framework is deferred to a future milestone that requires its own
architecture and migration plan.

## Consequences

### Positive

- Production hardening stays focused on reliability, security, and
  observability.
- No risky rewrite of working trading logic during the critical go-live phase.
- Documentation and repository artifacts are aligned with actual code.
- Users cannot accidentally believe the seven templates are active.

### Negative

- Advanced strategies (volatility-adjusted, RSI-weighted, MA-based, etc.) are
  not available in v1.0.
- The dynamic DCA tiers feature remains embedded in `bot/core.py` rather than
  expressed as a reusable strategy plugin.

## Actions Taken

1. `data/strategy_templates.json` moved to
   `docs/examples/strategy_templates.proposal.json` and marked as inactive.
2. `bot/strategies/` and `tests/strategies/` kept as empty placeholders with
   READMEs explaining the deferred framework.
3. `docs/staging-implementation-plan.md` updated to state the framework is
   deferred and to list what a future implementation requires.
4. `docs/roadmap.md` updated to clearly separate v1.0 work from the future
   strategy framework milestone.
5. `bot/backup.py` and `docs/operations/BACKUP_RESTORE_RUNBOOK.md` updated so
   the inactive proposal file is no longer treated as production data.
6. A repository test ensures the proposal file cannot be accidentally loaded as
   active configuration.

## Future Design Requirements

When the strategy framework is picked up again, the design must include:

- A `Strategy` abstract base class with well-defined lifecycle hooks
  (`on_schedule`, `on_price_tick`, `on_deposit`).
- Typed `Signal` / `BuySignal` objects returned by strategies.
- A `RiskController` that enforces global and per-strategy limits before any
  order is sent.
- A dedicated `OrderExecutor` that translates signals into `OrderAttempt`
  objects and integrates with retry/reconciliation.
- A configuration schema that maps a bot instance to one active strategy.
- A `StrategyRegistry` for discovery and metadata.
- Migration path from existing `config.json` settings to strategy parameters.
- Backtesting compatibility (historic price and balance replay).
- Comprehensive unit tests for each strategy, risk limits, and executor behavior.
- Dashboard UI integration for strategy selection, parameter editing, and
  simulation.

## Related Documents

- `docs/roadmap.md`
- `docs/staging-implementation-plan.md`
- `docs/examples/strategy_templates.proposal.json`
- `bot/strategies/README.md`
