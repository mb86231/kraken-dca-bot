# Production Preflight

The production preflight is a read-only safety check that proves the bot is
configured correctly before any real Kraken order can be placed. It is mandatory
for live trading: the order executor will refuse to submit a real order unless a
current, green preflight result exists.

## What it does

The preflight runs a checklist of checks grouped by category. Each check is
classified as **PASS**, **WARN**, or **FAIL**. A single FAIL makes the overall
result FAIL and blocks live orders.

The preflight never places an order. It may call:

- Kraken public endpoints (e.g. `AssetPairs`, `Ticker`) to validate the pair and price.
- Kraken read-only private endpoints (e.g. `Balance`) to validate credentials and funds.
- A harmless Telegram test message only when `--telegram-test` is explicitly requested.

## Running the preflight

### Command line

From the project root, with `APP_ENV=production` and `DEMO_MODE=false`:

```bash
python scripts/preflight_production.py
```

Output is human-readable by default. Use `--json` for machine-readable output:

```bash
python scripts/preflight_production.py --json
```

Validate a specific pair and amount without buying:

```bash
python scripts/preflight_production.py --pair XBTCHF --amount 0.0002 --json
```

Send a harmless Telegram test message:

```bash
python scripts/preflight_production.py --telegram-test
```

The result is persisted to `data/preflight.json` by default. Use `--output` to
change the path.

### Unattended live trading

Scheduled live buys are controlled by `LIVE_TRADING_ENABLED=true`. The preflight
script is advisory and is no longer required before trading. You can run it
periodically to verify the production environment, or skip it once the bot is
configured and the order executor's placement-time checks are passing.

### Dashboard

Open `/preflight` while signed in. The page displays every check, the overall
status, and whether live orders are currently blocked. Click **Refresh Checks**
to re-run the preflight via `POST /api/preflight/run`.

## Checks performed

| Category | Check | Notes |
|----------|-------|-------|
| Environment | `app_env_production` | `APP_ENV` must be `production` |
| Environment | `demo_mode_disabled` | `DEMO_MODE` must not be enabled |
| Environment | `live_trading_enabled` | `LIVE_TRADING_ENABLED` should be `true` |
| Environment | `bot_not_paused` | Warn if paused |
| Environment | `trading_loop_state` | Bot status must not be `stopped`, `error`, or `hold` |
| Credentials | `kraken_credentials_present` | Key and secret are set |
| Credentials | `kraken_authentication` | Balance call succeeds |
| Credentials | `kraken_minimum_permissions` | WARN: verify manually (Kraken does not expose permissions) |
| Credentials | `kraken_withdrawal_permission` | WARN: verify manually that withdrawal is disabled |
| Security | `session_secret_present` | `SESSION_SECRET` set and >= 16 characters |
| Security | `password_or_oidc_configured` | Password hash or OIDC enabled |
| Security | `secure_cookie` | `WEB_UI_SECURE_COOKIE=true` in production |
| Security | `csrf_active` | Session signing secret present |
| Security | `rate_limiting_active` | `DISABLE_RATE_LIMIT` must be false, `RATE_LIMIT_ENABLED` not disabled |
| Filesystem | `data_dir_writable` | Data directory writable |
| Filesystem | `logs_dir_writable` | Logs directory writable |
| Filesystem | `backups_dir_writable` | Backups directory writable |
| Market | `pair_exists` | Pair resolves in Kraken `AssetPairs` |
| Market | `pair_metadata_loaded` | Metadata like `lot_decimals`, `pair_decimals` loaded |
| Market | `ticker_available` | Current price fetched |
| Amount | `amount_positive` | Amount > 0 |
| Amount | `amount_precision` | Matches Kraken `lot_decimals` |
| Amount | `volume_above_minimum` | Amount >= Kraken `ordermin` |
| Amount | `cost_above_minimum` | `amount * price` >= Kraken `costmin` |
| Balance | `balance_readable` | Account balance fetched |
| Balance | `balance_sufficient` | Available fiat covers cost + fee buffer |
| Balance | `fee_buffer_included` | Estimated fee + safety buffer included |
| Order state | `no_hold_state` | No bot-level HOLD |
| Order state | `no_unknown_orders` | No `UNKNOWN` order attempts |
| Order state | `no_duplicate_attempts` | No duplicate non-terminal attempts |
| Order state | `next_cycle_sensible` | Next cycle time is plausible |
| Order state | `heartbeat_recent` | `data/heartbeat.json` is recent |
| Telegram | `telegram_configured` | Token and chat ID set |
| Telegram | `telegram_test` | Only when `--telegram-test` is used |
| System | `system_time_plausible` | System clock and timezone look reasonable |

## Result expiry

A preflight result expires after `PREFLIGHT_EXPIRY_SECONDS` (default 3600).
Because the preflight is now advisory, expiry does not block live orders. The
dashboard still shows the result age so operators know how current the diagnostic
is.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PREFLIGHT_EXPIRY_SECONDS` | `3600` | How long a result remains valid |
| `PREFLIGHT_HEARTBEAT_MAX_AGE_SECONDS` | `900` | Maximum acceptable heartbeat age |
| `PREFLIGHT_FEE_BUFFER_PERCENT` | `0.5` | Extra safety margin on top of the Kraken fee |


## Live-order validation

Immediately before calling Kraken, the order executor validates the live order
using current values fetched from the exchange:

- `APP_ENV` is `production` and demo mode is disabled.
- Live trading is enabled.
- The bot is not in `HOLD` and no `UNKNOWN` order attempt is unresolved.
- The idempotency key / client reference is unique (duplicate prevention).
- Current available quote-currency balance covers the order cost plus fee buffer.
- Current pair metadata (minimum volume, minimum cost, volume decimals, pair
  decimals) is loaded from Kraken.
- Calculated volume is above the current minimum and rounded to current precision.
- A conservative cost estimate using the latest price plus expected fee and
  configured fee buffer fits the balance.

The preflight result is advisory and is no longer checked here. If any of the
live checks fails, the executor records a structured reason, moves the attempt to
`HOLD`, notifies the operator, and submits **zero** orders.

1. (Optional) Run the preflight CLI to verify the environment.
2. Enable `LIVE_TRADING_ENABLED=true` and wait for the next scheduled cycle.

## Troubleshooting

### `Amount ... is below Kraken ordermin`

Increase `crypto_amount` in the dashboard or `config.json` so it is at least the
pair's `ordermin`. The preflight and order executor both display the
Kraken-provided minimum.

### `Available ... is less than estimated cost ...`

Deposit more fiat or reduce `crypto_amount`. The estimated cost includes the
Kraken taker fee plus the configured fee-buffer safety margin.

## Related documents

- [`docs/operations/GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md)
- [`docs/operations/BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md)
- [`docs/configuration.md`](../configuration.md)
