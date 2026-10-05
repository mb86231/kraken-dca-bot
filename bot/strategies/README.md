# Strategy Framework (Deferred)

This package is intentionally empty in Production Hardened v1.0.

The trading logic for v1.0 lives in `bot/core.py` and supports:

- Recurring DCA
- Lump-sum DCA
- Dip buying
- Dynamic DCA tiers

A pluggable strategy framework (`Strategy` ABC, `StrategyRegistry`,
`RiskController`, `OrderExecutor`) was planned but deferred to a future
milestone. See `docs/adr/ADR-001-strategy-framework.md`.

This directory is kept as a placeholder so that future strategy work can use
the package without re-creating it.
