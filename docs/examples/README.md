# Example and Proposal Files

Files in this directory are **not loaded by the runtime**. They are kept as
future-design references and documentation aids.

## `strategy_templates.proposal.json`

This file contains **seven strategy templates** that were proposed for a
pluggable strategy framework. That framework is **not implemented** in
Production Hardened v1.0.

The bot currently implements the following strategies directly in
`bot/core.py`:

- Recurring DCA
- Lump-sum DCA
- Dip buying
- Dynamic DCA tiers

The templates in this file may become the basis of a future `Strategy` ABC,
`StrategyRegistry`, and `RiskController`, but no runtime code reads this file
today. Do not edit it expecting the bot behavior to change.

See `docs/adr/ADR-001-strategy-framework.md` for the decision record.
