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

Dynamic DCA scales the size of every buy with the price trend: bigger buys when
the price drops below your reference, smaller buys or a full skip when it rises.

```json
{
  "dynamic_dca": {
    "enabled": true,
    "reference": "last_buy",
    "cooldown_hours": 24.0,
    "tiers": [
      {"threshold_percent": 10.0, "amount": 0.0, "enabled": true},
      {"threshold_percent": 5.0, "amount": 0.0005, "enabled": true},
      {"threshold_percent": -2.0, "amount": 0.00075, "enabled": true},
      {"threshold_percent": -5.0, "amount": 0.001, "enabled": true},
      {"threshold_percent": -10.0, "amount": 0.0015, "enabled": true},
      {"threshold_percent": -20.0, "amount": 0.002, "enabled": true}
    ]
  }
}
```

- `enabled`: turn dynamic tiers on or off.
- `reference`: `"last_buy"` or `"avg_buy"` — the price the current price is
  compared against. `"last_buy"` reacts quickly to recent dips; `"avg_buy"`
  smooths over your whole purchase history.
- `cooldown_hours`: minimum hours between dynamic buys. Counted from your last
  buy of any strategy, so a scheduled buy also starts the cooldown.
- `tiers`: each tier has a `threshold_percent` (price change from reference)
  and an `amount` to buy when that threshold is reached. `amount: 0` means
  skip the buy entirely. Each tier also has an optional
  `override_monthly_budget` flag (default `false`, see below).

#### How a buy amount is resolved

1. Compute the price change: `(current_price - reference) / reference * 100`.
2. **Dips** (negative change): sort the enabled dip tiers (threshold ≤ 0)
   from deepest to shallowest and pick the first tier the drop actually
   reached (`change <= threshold`). The deepest crossed tier wins, so a
   crash never falls back to the smaller base amount.
3. **Rises** (positive change): sort the enabled rise tiers (threshold > 0)
   from highest to lowest and pick the first tier whose threshold is
   reached (`change >= threshold`). The highest reached tier wins, so a
   +10 % rise skips the buy instead of matching the shallower +5 % tier.
4. A rise never triggers a dip tier and vice versa. If no tier matched,
   fall back to the base amount (`crypto_amount`). An amount of `0` skips
   the buy completely.

With the default tiers above (amounts for BTC pairs; Kraken's ordermin is
0.00005 XBT for XXBTZEUR, and the bot always enforces the live ordermin —
scale the set proportionally for other assets or budgets):

| Price vs reference | Buy amount | Why |
|--------------------|-----------|-----|
| ≥ +10 % | none | Buying deep into a rally is skipped |
| +5 % … < +10 % | 0.0005 | Keep accumulating, but small |
| > −2 % … < +5 % | 0.0005 (base) | No tier applies — normal conditions |
| > −5 % … ≤ −2 % | 0.00075 | Crossed the −2 % mark |
| > −10 % … ≤ −5 % | 0.001 | Crossed the −5 % mark |
| > −20 % … ≤ −10 % | 0.0015 | Strong dip |
| ≤ −20 % | 0.002 | Deepest tier — crash protection, never the smaller base amount |

#### Per-tier budget override

Each tier accepts an optional `override_monthly_budget` flag (default
`false`). When set to `true`, a buy matched by that tier:

- ignores the `max_monthly_amount` budget limit — the buy goes through even
  when the monthly budget is already exhausted,
- does **not** count toward the monthly budget (it is excluded from
  `get_monthly_spent`, so the remaining budget for scheduled buys is
  untouched),
- is labelled `dynamic -20 % · budget override` in the transactions list,
  and the Telegram notification names the override explicitly.

Typical use: crash protection on the deepest dip tier, where you want the
bot to keep buying through a prolonged crash even though the regular monthly
budget is spent.

> **Risk warning**: the override lifts the budget cap, not the laws of your
> bank account. The buys are still charged against your Kraken fiat balance.
> With `reference: "last_buy"` an ongoing crash re-triggers the tier after
> every cooldown window; with `reference: "avg_buy"` every further drop
> against the falling average can trigger again — in a long downturn this
> can drain the fiat balance well beyond the configured monthly budget.
> Enable it only on tiers you deliberately want to act as uncapped crash
> buys.
>
> The override never bypasses the balance pre-check: if the account cannot
> fund the buy, it is skipped with an explicit alert (and the failure starts
> the dynamic cooldown, so a failing setup cannot fire one alert per poll).
> It also only applies to *dynamic* buys matched by that tier — scheduled and
> manual buys are unaffected, and the manual "buy anyway" one-shot override
> keeps its existing semantics (the buy counts toward the budget).

Example — the deepest tier buys through an exhausted budget:

```json
{
  "threshold_percent": -20.0,
  "amount": 0.002,
  "enabled": true,
  "override_monthly_budget": true
}
```

#### Where the tiers apply

- **Scheduled buys** (deposit day): the resolved tier amount is used, so a
  scheduled buy is reduced or skipped too when the market ran up.
- **Dynamic extra buys**: on every poll, if the resolved amount is *larger*
  than the base amount and the cooldown has passed, the bot buys immediately
  and recalculates the remaining schedule. Since v1.1.5 these orders are
  labelled with the tier that fired (e.g. `dynamic -5 %`).
- **Manual "Buy Now"**: also uses the resolved tier amount.
- **Budget and limits still apply**: the monthly budget is checked against the
  resolved tier amount (not the base amount), and `max_price` can still skip
  the buy.

The legacy fixed-percentage dip buy (`dip_threshold_percent`) is disabled
while dynamic DCA is enabled — the tier table replaces it.

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
| `OIDC_ALLOWED_SUBJECTS` | Comma-separated identities (sub, username, or email) allowed to log in via OIDC. **Fail closed: when OIDC is enabled and this is empty, no OIDC login is accepted.** |
| `SECRETS_PATH` | Path of the dashboard secrets store (default `data/secrets.json`, `/app/data/secrets.json` in the container). |

### Runtime toggles and non-secret settings

| Variable | Typical value | Purpose |
|----------|---------------|---------|
| `LIVE_TRADING_ENABLED` | `false` / `true` | Safety switch for real orders. |
| `DEMO_MODE` | `false` / `true` | Use synthetic exchange data. |
| `APP_ENV` | `production` / `staging` / unset | Environment label; staging forces demo mode. |
| `WEB_UI_ENABLED` | `true` | Enable the FastAPI dashboard. |
| `WEB_UI_HOST` | `0.0.0.0` | Dashboard bind address inside the container. |
| `WEB_UI_PORT` | `8000` | Dashboard port inside the container. |
| `WEB_UI_PUBLISH` | `0.0.0.0:8003:8000` | Port published on the container host (used by `compose.yaml`, the build-from-source setup). |
| `IMAGE_NAME` | `<REGISTRY_HOST>/.../crypto-agent` | Container image name for CI. |
| `IMAGE_TAG` | `<git-sha>` | Container image tag for CI. |
| `WEB_UI_USERNAME` | `admin` | Dashboard admin username. |
| `WEB_UI_SECURE_COOKIE` | `true` / `false` | Set `Secure` flag on cookies (use only with HTTPS). |
| `SESSION_TTL_HOURS` | `24` | Session lifetime in hours; logout and credential changes revoke sessions immediately. |
| `OIDC_ENABLED` | `false` / `true` | Show the "Sign in with SSO" button on the login page. |
| `OIDC_ISSUER_URL` | `https://authentik.example.com/application/o/dca-bot/` | OIDC issuer URL. |
| `OIDC_REDIRECT_URI` | `https://bot.example.com/auth/callback` | OIDC redirect URI (must match the SSO provider). |
| `OIDC_SCOPES` | `openid email profile` | OIDC scopes requested at login. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `DATA_DIR` | `data` | Directory for runtime JSON files. |
| `HEARTBEAT_FILE` | `data/heartbeat.json` | Path to the trading-loop heartbeat file. |
| `HEARTBEAT_MAX_AGE_SECONDS` | `900` | Maximum heartbeat age for readiness. |
| `BACKUP_DIR` | `backups` | Directory for backup archives. |
| `BACKUP_DAILY_COUNT` | `14` | Daily backups kept by the backup rotation. |
| `BACKUP_WEEKLY_COUNT` | `8` | Weekly backups kept by the backup rotation. |
| `RETENTION_DAYS` | `30` | Age after which backup archives are pruned. |
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
| `LOGIN_FAILURE_WINDOW_SECONDS` | `900` | Failures older than this decay instead of accumulating (defaults to the lockout window). |
| `LOGIN_MAX_TRACKED_KEYS` | `10000` | Bound for the per-key failure map; protects against memory exhaustion via random usernames. |
| `ORDER_RETRY_MAX_ATTEMPTS` | `4` | Max order retry attempts. |
| `ORDER_RETRY_BASE_SECONDS` | `30` | Base retry delay. |
| `ORDER_RETRY_MULTIPLIER` | `4` | Exponential backoff multiplier. |
| `ORDER_RETRY_MAX_JITTER_SECONDS` | `10` | Max random jitter per retry. |
| `ORDER_RECONCILE_WINDOW_SECONDS` | `300` | Window before an unknown order is treated as rejected. |
| `ORDER_UNKNOWN_HOLD_SECONDS` | `1800` | Max time an unknown order stays reconciling before HOLD. |
| `ORDER_RECONCILE_INTERVAL_SECONDS` | `30` | Minimum seconds between reconciliation queries. |
| `ORDER_FEE_BUFFER_PERCENT` | `0.5` | Extra safety margin applied by the order executor before placing a live order. |
| `PREFLIGHT_EXPIRY_SECONDS` | `3600` | Preflight result lifetime (used by the optional preflight script). |
| `PREFLIGHT_HEARTBEAT_MAX_AGE_SECONDS` | `900` | Maximum heartbeat age for the preflight readiness check. |
| `PREFLIGHT_FEE_BUFFER_PERCENT` | `0.5` | Extra safety margin used by the optional preflight script. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | `123456,-789` | Comma-separated chat IDs allowed to send Telegram commands. Defaults to `TELEGRAM_CHAT_ID`. |
| `TELEGRAM_ALLOWED_USER_IDS` | `123456` | Comma-separated Telegram user IDs allowed to issue commands/confirmations. When unset, only **private chats** are accepted — group members cannot drive the bot. |
| `TELEGRAM_COMMANDS_ENABLED` | `true` / `false` | Master switch for the interactive Telegram command bot. |
| `SESSIONS_PATH` | `data/sessions.json` | Revocable server-side session store (next to the secrets file by default). |

### AI advisor (beta)

The optional AI advisor layer (planned beta track) is **off by default**
and strictly advisory: it never places orders, never receives exchange
credentials, and its failure never changes the base strategy. All advisor
behaviour degrades to "no advice"; outages are visible in the dashboard
and the logs.

`config.json` section (defaults shown):

```json
"advisor": {
  "enabled": false,
  "features": {"briefing": false, "explainer": false},
  "provider": "openai",
  "base_url": "",
  "model": "",
  "timeout_seconds": 30.0,
  "max_retries": 2,
  "max_parallel": 2,
  "daily_request_limit": 50,
  "daily_token_limit": 100000,
  "daily_cost_limit": 1.0,
  "briefing_time": "07:00",
  "briefing_frequency": "daily",
  "briefing_weekday": 0,
  "briefing_month_day": 1,
  "briefing_sections": {
    "market": true,
    "portfolio": true,
    "budget": true,
    "bot": true,
    "news_general": false,
    "news_pair": false,
    "narrative": false
  },
  "narrative_focus": ["market", "portfolio", "plan"],
  "custom_prompt": "",
  "narrative_max_words": 120,
  "news_feeds": [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss"
  ],
  "temperature": 0.2,
  "max_tokens": 1024
}
```

- `provider`: `openai` (any OpenAI-compatible endpoint, e.g. OpenAI or
  Kimi Open Platform) or `ollama` (local).
- `features`: per-feature switches; each advisory feature ships disabled.
- **Briefing schedule**: `briefing_time` (`HH:MM`, Europe/Zurich,
  DST-aware) combined with `briefing_frequency` (`daily`, `weekly` or
  `monthly`). Weekly uses `briefing_weekday` (0=Monday … 6=Sunday);
  monthly uses `briefing_month_day` (1–28, capped so the slot exists in
  every month).
- **Briefing content**: `briefing_sections` selects the message blocks —
  `market` (price), `portfolio` (invested, holdings, value now, P/L in
  amount and percent), `budget` (monthly budget, next cycle, estimated
  buys left), `bot` (status, live trading) and the two news blocks
  (`news_general`, `news_pair`). All are bot-computed and consume no AI
  tokens: the bot fetches the configured RSS/Atom `news_feeds` itself
  (short cache, failing feeds are skipped, never blocking) and filters
  pair news by the base asset (XBT→Bitcoin, ETH→Ethereum, …). The AI
  provider never goes to the internet. The optional `narrative` block
  adds an AI-written paragraph; it ships disabled so the default briefing
  costs nothing. When enabled, the bot still supplies every number — the
  AI only writes the prose, and an unavailable provider never blocks the
  message. Briefings are archived on the dashboard.
- **Narrative customization**: `narrative_focus` (`market`, `portfolio`,
  `plan`, `news`) tells the AI which aspects to cover; `news` only takes
  effect when headlines were fetched. `narrative_max_words` bounds the
  length (40–400, default 120). `custom_prompt` replaces the default task
  text — use placeholders `{pair} {price} {invested} {holdings}
  {value_now} {pnl_pct} {budget_spent} {budget_limit} {next_run} {buys}
  {bot_status}`; unknown placeholders stay untouched, the safety rules
  (no invented values, no financial advice) always apply. Empty =
  default prompt.
- `temperature`: sampling temperature (0–2, default 0.2). Some reasoning
  models only accept `1`; if the provider rejects the request with an
  "invalid temperature" error, set this to `1`.
- `max_tokens`: response token budget (default 1024). Reasoning models
  also count internal thinking towards this limit; if the provider
  returns empty content (`finish_reason=length`), raise this (e.g. 4096).
- **Send Test Briefing** (Settings → Reporting): sends the real briefing
  message once — snapshot → (optional AI narrative) → Telegram — without
  consuming the scheduled slot or writing to the briefing history. With
  the narrative section enabled, provider failures are reported as-is
  (never masked); with it disabled, the deterministic message is tested
  and the provider is never called. Provider, model, temperature and key
  changes only take effect on startup — use the **Restart** button in the
  top bar after saving.
- The provider API key is **never** written to `config.json` — set it in the
  dashboard (Settings → AI Advisor → Provider API Key), which stores it in
  the secrets store (`data/secrets.json`, 0600), or via the environment.

| Variable | Typical value | Purpose |
|----------|---------------|---------|
| `AI_ADVISOR_API_KEY` | `<provider key>` | Provider API key (OpenAI-compatible providers only; Ollama needs none). Environment takes precedence over the dashboard-stored key. |
| `AI_ADVISOR_ENABLED` | `false` | Kill switch: any of `false`/`0`/`off` forces the advisor off regardless of `config.json` or dashboard settings. |

Every interaction is audit-logged to `logs/advisor.json` (provider/model,
prompt and schema versions, status, latency, tokens/cost, error category,
and a prompt hash — never the prompt text or any secret).

### Telegram command bot

Besides one-way notifications, the bot can receive commands via long polling
(outbound HTTPS only — no port is exposed). Commands: `/status`, `/price`,
`/budget`, `/last [n]`, `/alerts`, `/buy`, `/pause`, `/resume`, `/help`.
See [docs/TELEGRAM.md](TELEGRAM.md) for the full guide: setup, command
reference, security model, multiple operators, and troubleshooting.

Security properties:

- Only chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS` (or the notification chat ID)
  are served; unknown chats are silently ignored.
- Only the sender may drive the bot: with `TELEGRAM_ALLOWED_USER_IDS` set,
  only those Telegram user IDs are accepted (even in group chats). Without
  it, only private chats are accepted — a group member cannot issue commands
  or press the confirmation button for someone else's action.
- `/buy`, `/pause`, and `/resume` require a one-time inline confirmation that
  expires after 60 seconds, is bound to the requesting user, and cannot be
  replayed.
- Per-chat rate limiting (2 s between commands, 20/minute).
- Every command and executed action is written to the audit log.
- Secrets are never accepted or printed by any command.

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
  affect the trading pair or API credentials still require a container restart
  — use the **Restart** button in the top bar of the dashboard.

---

## Related documents

- [`docs/secrets.md`](secrets.md) — secret management, rotation, and pre-commit hooks.
- [`docs/operations.md`](operations.md) — deployment and day-to-day operations.
- [`docs/WEB_DASHBOARD.md`](WEB_DASHBOARD.md) — dashboard setup and OIDC.
- [`docs/development.md`](development.md) — local development workflow.
- [`docs/operations/PRODUCTION_PREFLIGHT.md`](operations/PRODUCTION_PREFLIGHT.md) — production preflight checks and live-order gate.
