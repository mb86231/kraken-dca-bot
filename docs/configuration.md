# DCA-Bot Configuration Guide

This document is the single source of truth for configuring the DCA-Bot. Other
documents link here instead of duplicating the full reference.

---

## Configuration model

The bot uses two separate sources of configuration:

1. **`config.json`** — trading parameters only. No secrets. Safe to version
   (without real values) and to inspect in the dashboard.
2. **Environment variables / `.env`** — secrets, toggles, and runtime settings
   loaded at container startup. Never commit real values.

### Why split them?

- `config.json` can be edited through the dashboard and backed up safely.
- API keys, password hashes, and session secrets stay in the environment and are
  injected by the deployment pipeline or a local `.env` file.

---

## `config.json` reference

### Example: recurring mode

```json
{
  "mode": "recurring",
  "trading_pair": "XBTCHF",
  "deposit_day": 24,
  "crypto_amount": 0.0001,
  "dip_threshold_percent": 5.0,
  "dip_buy_cooldown_hours": 2.0,
  "poll_interval_seconds": 300,
  "buy_hour": 8,
  "max_price": 65000,
  "max_monthly_amount": 10000
}
```

### Example: lump-sum mode

```json
{
  "mode": "lump_sum",
  "dca_end_date": "2027-06-01",
  "trading_pair": "XBTCHF",
  "deposit_day": 1,
  "crypto_amount": 0.0001,
  "dip_threshold_percent": 5.0,
  "dip_buy_cooldown_hours": 2.0,
  "poll_interval_seconds": 300,
  "buy_hour": 8,
  "max_price": 65000,
  "max_monthly_amount": null
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mode` | `"recurring"` or `"lump_sum"` | yes | DCA strategy mode. |
| `trading_pair` | string | yes | Kraken trading pair, e.g. `XBTCHF`, `XXBTZUSD`. |
| `deposit_day` | int 1–28 | recurring | Day of month the cycle resets and the scheduled buy fires. |
| `buy_hour` | int 0–23 | yes | Hour of day used as the buy/cycle reference. |
| `crypto_amount` | float > 0 | yes | Crypto amount per single buy order. Must meet Kraken's minimum for the pair. |
| `dip_threshold_percent` | float 0–100 | yes | Extra buy when price drops this percent below the last buy price. |
| `dip_buy_cooldown_hours` | float ≥ 0 | yes | Minimum hours between two consecutive dip buys. |
| `poll_interval_seconds` | int ≥ 60 | yes | How often the bot checks price and balance. |
| `max_price` | float or `null` | no | Skip buys when price is above this value. |
| `max_monthly_amount` | float or `null` | no | Maximum fiat to spend per cycle/month. |
| `dca_end_date` | ISO date `"YYYY-MM-DD"` or `null` | lump_sum | Last day buys are allowed. Must be in the future. |
| `dynamic_dca` | object | no | Dynamic DCA tiers. See below. |

### Dynamic DCA tiers

```json
{
  "dynamic_dca": {
    "enabled": true,
    "reference": "last_buy",
    "cooldown_hours": 24.0,
    "tiers": [
      {"threshold_percent": 10.0, "amount": 0.0, "enabled": true},
      {"threshold_percent": 5.0, "amount": 0.00005, "enabled": true},
      {"threshold_percent": -2.0, "amount": 0.0001, "enabled": true},
      {"threshold_percent": -5.0, "amount": 0.00015, "enabled": true},
      {"threshold_percent": -10.0, "amount": 0.0002, "enabled": true},
      {"threshold_percent": -20.0, "amount": 0.0003, "enabled": true}
    ]
  }
}
```

- `enabled`: turn dynamic tiers on or off.
- `reference`: `"last_buy"` or `"avg_buy"` — price used to compute the drawdown.
- `cooldown_hours`: minimum hours between dynamic buys.
- `tiers`: each tier has a `threshold_percent` (price change from reference) and
  an `amount` to buy when that threshold is reached. `amount: 0` means skip.

### Validation

`bot/config.py` validates `config.json` on load. The bot fails fast with a
descriptive error if:

- `mode` is not `"recurring"` or `"lump_sum"`.
- `trading_pair` is missing.
- `deposit_day` is outside 1–28 (recurring mode only).
- `dca_end_date` is missing or in the past (lump_sum mode only).
- `crypto_amount` is not greater than 0.
- `dip_threshold_percent` is not between 0 and 100.
- `poll_interval_seconds` is below 60.
- `buy_hour` is outside 0–23.
- Dynamic DCA tiers are enabled but invalid.

### Trading pairs

Kraken uses internal asset codes. Common pairs:

| Pair | Kraken string |
|------|---------------|
| BTC/CHF | `XBTCHF` |
| BTC/USD | `XXBTZUSD` |
| BTC/EUR | `XXBTZEUR` |
| ETH/USD | `XETHZUSD` |

See the full list at `https://api.kraken.com/0/public/AssetPairs`.

### Minimum order sizes

Kraken enforces a minimum fiat value per order (typically around 10
EUR/CHF/USD). The bot validates `crypto_amount > 0` but does not know Kraken's
exact minimum. If an order is below the minimum, Kraken rejects it and the bot
records a permanent failure in `data/order_attempts.json`.

---

## Environment variables / `.env`

Copy `.env.example` to `.env` and fill in real values. `.env` is in
`.gitignore` and must never be committed.

### Secrets (must be kept private)

| Variable | Purpose |
|----------|---------|
| `KRAKEN_API_KEY` | Kraken API public key. |
| `KRAKEN_API_SECRET` | Kraken API private key. |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token. |
| `TELEGRAM_CHAT_ID` | Telegram chat ID. |
| `WEB_UI_PASSWORD_HASH` | Bcrypt hash of the dashboard admin password. |
| `SESSION_SECRET` | Strong random value for signing session cookies. |
| `OIDC_CLIENT_ID` | OIDC client ID (optional). |
| `OIDC_CLIENT_SECRET` | OIDC client secret (optional). |

### Runtime toggles and non-secret settings

| Variable | Typical value | Purpose |
|----------|---------------|---------|
| `LIVE_TRADING_ENABLED` | `false` / `true` | Safety switch for real orders. |
| `DEMO_MODE` | `false` / `true` | Use synthetic exchange data. |
| `APP_ENV` | `production` / `staging` / unset | Environment label; staging forces demo mode. |
| `WEB_UI_ENABLED` | `true` | Enable the FastAPI dashboard. |
| `WEB_UI_HOST` | `0.0.0.0` | Dashboard bind address inside the container. |
| `WEB_UI_PORT` | `8000` | Dashboard port inside the container. |
| `WEB_UI_PUBLISH` | `0.0.0.0:8003:8000` | Port published on the container host. |
| `WEB_UI_USERNAME` | `admin` | Dashboard admin username. |
| `WEB_UI_SECURE_COOKIE` | `true` / `false` | Set `Secure` flag on cookies (use only with HTTPS). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `DATA_DIR` | `data` | Directory for runtime JSON files. |
| `HEARTBEAT_FILE` | `data/heartbeat.json` | Path to the trading-loop heartbeat file. |
| `HEARTBEAT_MAX_AGE_SECONDS` | `900` | Maximum heartbeat age for readiness. |
| `BACKUP_DIR` | `backups` | Directory for backup archives. |
| `ORDER_ATTEMPTS_FILE` | `data/order_attempts.json` | Persisted order-attempt state. |
| `MONITORING_TOKEN` | `<random>` | Bearer token for `/api/metrics`. |
| `RATE_LIMIT_ENABLED` | `true` | Master switch for rate limiting. |
| `RATE_LIMIT_LOGIN` | `5/minute` | Login rate limit. |
| `RATE_LIMIT_API` | `60/minute` | Authenticated API rate limit. |
| `RATE_LIMIT_MANUAL_BUY` | `3/minute` | Manual-buy rate limit. |
| `RATE_LIMIT_SETTINGS` | `10/minute` | Settings/control rate limit. |
| `DISABLE_RATE_LIMIT` | `false` | Allowed only in non-production environments. |
| `LOGIN_MAX_FAILURES` | `5` | Failed logins before lockout. |
| `LOGIN_LOCKOUT_SECONDS` | `900` | Lockout duration in seconds. |
| `ORDER_RETRY_MAX_ATTEMPTS` | `4` | Max order retry attempts. |
| `ORDER_RETRY_BASE_SECONDS` | `30` | Base retry delay. |
| `ORDER_RETRY_MULTIPLIER` | `4` | Exponential backoff multiplier. |
| `ORDER_RETRY_MAX_JITTER_SECONDS` | `10` | Max random jitter per retry. |
| `ORDER_RECONCILE_WINDOW_SECONDS` | `300` | Window before an unknown order is treated as rejected. |
| `ORDER_UNKNOWN_HOLD_SECONDS` | `1800` | Max time an unknown order stays reconciling before HOLD. |
| `ORDER_RECONCILE_INTERVAL_SECONDS` | `30` | Minimum seconds between reconciliation queries. |
| `PREFLIGHT_FEE_BUFFER_PERCENT` | `0.5` | Extra safety margin used by the optional preflight script. |
| `ORDER_FEE_BUFFER_PERCENT` | `0.5` | Extra safety margin applied by the order executor before placing a live order. |
| `IMAGE_NAME` | `<REGISTRY_HOST>/.../crypto-agent` | Container image name for CI. |
| `IMAGE_TAG` | `<git-sha>` | Container image tag for CI. |

### Demo mode

```env
DEMO_MODE=true
LIVE_TRADING_ENABLED=false
KRAKEN_API_KEY=demo-key
KRAKEN_API_SECRET=demo-secret
```

Demo mode does not call Kraken; prices and balances are synthetic.

### Production requirements

When `APP_ENV=production`, the dashboard refuses to start unless:

- `SESSION_SECRET` is set and at least 16 characters.
- `WEB_UI_PASSWORD_HASH` is set.
- `WEB_UI_SECURE_COOKIE=true`.
- `DISABLE_RATE_LIMIT` is not `true`.

Live orders are controlled by `LIVE_TRADING_ENABLED=true`. The optional
`scripts/preflight_production.py` diagnostic can be run at any time to verify the
production configuration, but it is no longer required before trading. See
[`docs/operations/PRODUCTION_PREFLIGHT.md`](operations/PRODUCTION_PREFLIGHT.md).

---

## Where configuration is loaded from

- `config.json` is loaded by `bot/config.py` and by the dashboard.
- The path defaults to `config.json` in the working directory. In the container
  deployment it is set via `CONFIG_PATH=/app/data/config.json` so the file lives
  inside the writable `/app/data` volume.
- Environment variables are loaded by the container runtime (`env_file` in
  `compose.yaml`) and by `os.environ` at runtime.
- The dashboard can edit `config.json` and persist it atomically. Changes that
  affect the trading pair or API credentials still require a container restart.

---

## Related documents

- [`docs/secrets.md`](secrets.md) — secret management, rotation, and pre-commit hooks.
- [`docs/operations.md`](operations.md) — deployment and day-to-day operations.
- [`docs/WEB_DASHBOARD.md`](WEB_DASHBOARD.md) — dashboard setup and OIDC.
- [`docs/development.md`](development.md) — local development workflow.
- [`docs/operations/PRODUCTION_PREFLIGHT.md`](operations/PRODUCTION_PREFLIGHT.md) — production preflight checks and live-order gate.
