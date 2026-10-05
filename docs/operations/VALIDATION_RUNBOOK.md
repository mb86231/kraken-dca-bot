# Validation Runbook

This document describes how to validate the DCA-Bot before enabling unattended live trading.

## Goal

Confirm that:

1. Staging / demo mode never places real orders.
2. Production safety guards are active.
3. A single, small, manually triggered live order works end to end.
4. Order attempts, transactions, notifications, and health endpoints reflect the trade.

## Staging validation

Staging mode uses the mock exchange and synthetic data. It should be run first.

### Environment

```bash
export APP_ENV=staging
export DEMO_MODE=true
export LIVE_TRADING_ENABLED=false
export KRAKEN_API_KEY=demo
export KRAKEN_API_SECRET=demo
export WEB_UI_ENABLED=true
```

### Automated tests

```bash
# Windows (Git Bash)
PYTHONPATH=. .venv/Scripts/pytest tests/test_validation.py -v

# Linux / macOS
PYTHONPATH=. .venv/bin/pytest tests/test_validation.py -v
```

Expected result: all tests pass.

### Manual smoke test

1. Build and start the container in staging mode.
2. Open the dashboard and confirm:
   - Status shows `demo_mode: true`
   - `live_trading_enabled: false`
   - Balance is synthetic
3. Trigger **Buy Now** from the dashboard.
4. Confirm a simulated transaction appears, but no `data/order_attempts.json` entry is created.
5. Confirm Telegram test notification works (if configured).

## Live-trade validation

This places one real market order using production Kraken credentials. Only run this after staging validation is successful.

### Prerequisites

- [ ] A Kraken API key with **only** these permissions:
  - Query Funds
  - Create & Modify Orders
- [ ] Sufficient fiat balance for the validation amount.
- [ ] `APP_ENV=production` or `APP_ENV` unset.
- [ ] `DEMO_MODE=false`
- [ ] `LIVE_TRADING_ENABLED=true`
- [ ] `SESSION_SECRET` set to a stable random value.
- [ ] `WEB_UI_PASSWORD_HASH` or OIDC configured.
- [ ] Telegram configured and tested (optional but strongly recommended).
- [ ] Backup script and timer installed.
- [ ] Health checks respond correctly.

### Recommended validation amount

Start with the smallest amount your fiat currency and pair support, for example:

- 10 CHF / EUR / USD for XBTCHF / XBTEUR / XBTUSD

Never validate with more than you are willing to lose to a configuration error.

### Procedure

1. Start the bot with production environment variables.
2. Verify readiness:
   ```bash
   curl -f http://localhost:8000/health/ready
   ```
3. Run the validation script:
   ```bash
   # Windows (Git Bash)
   PYTHONPATH=. .venv/Scripts/python scripts/validate_live_trade.py --amount 10

   # Linux / macOS
   PYTHONPATH=. .venv/bin/python scripts/validate_live_trade.py --amount 10
   ```
4. Type the confirmation phrase when prompted.
5. Observe the output:
   - API connectivity OK
   - Balance sufficient
   - Order placed
   - Attempt state is `CONFIRMED`, `SUBMITTED`, or `UNKNOWN`
6. Check the dashboard **Orders** page and **Transactions** page.
7. Verify the order appears on Kraken itself.
8. If the state is `UNKNOWN`, wait up to `ORDER_RECONCILE_WINDOW_SECONDS` (default 300s) and check again.

### What to do if validation fails

| Symptom | Action |
|---------|--------|
| `LiveOrderBlockedError` | Confirm `APP_ENV=production` and `DEMO_MODE=false`. |
| Insufficient funds | Deposit the smallest amount and retry. |
| Invalid credentials | Verify `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`; check key permissions. |
| Order state `UNKNOWN` | Check Kraken directly; use dashboard resolve actions if needed. |
| No Telegram alert | Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; run `/telegram/test` from the dashboard. |

### Re-enable normal operation

After a successful single-order validation:

1. Review `data/order_attempts.json` and `data/transactions.json`.
2. Set the normal `crypto_amount` in `config.json`.
3. Restart the bot.
4. Monitor the first scheduled cycle closely.

## Rollback

If anything looks wrong during validation:

1. Stop the bot.
2. Set `LIVE_TRADING_ENABLED=false`.
3. Restore `config.json` from the latest backup if needed.
4. Open an issue describing the unexpected behavior and attach sanitized logs.
