# Crypto-Agent Operations Guide

This document describes the production deployment, daily operations, Git workflow, secret management, and troubleshooting for the Kraken DCA bot.

> **Deployment-specific values** in this document are written as placeholders
> (e.g., `<PRODUCTION_HOST>`, `<DEPLOYMENT_PATH>`). Replace them with your own
> infrastructure values when following these procedures. See
> [`docs/configuration.md`](../docs/configuration.md) for the complete environment
> reference and [`docs/secrets.md`](../docs/secrets.md) for secret management.

---

## 1. System Overview

The **crypto-agent** is a self-hosted, containerized Dollar-Cost Averaging (DCA) bot for Kraken. It connects to the Kraken API, monitors a configured trading pair, and automatically places small market buy orders according to a schedule. An optional FastAPI web dashboard is built into the same container for monitoring and control.

### What it does

- **Recurring DCA**: Spreads buys evenly between now and the next monthly deposit day.
- **Lump-sum DCA**: Spreads buys evenly until a configured end date (`mode: lump_sum`).
- **Dip buying**: Places extra buys when the price drops a configured percentage below the last buy price.
- **Deposit detection**: Recalculates the buy schedule when new fiat balance arrives.
- **Safety limits**: Respects `max_price` and `max_monthly_amount` settings.
- **Manual controls**: Pause, resume, immediate "Buy Now", and stop from the dashboard or API.
- **Telegram notifications**: Reports startup, every placed order, and order errors.
- **Structured logging**: JSON-lines logs with Europe/Zurich timestamps and automatic secret redaction.
- **Backups**: Scheduled, validated, checksummed archives of all persistent JSON/DB data; excludes secrets and caches.

### Infrastructure

| Component | Details |
|-----------|---------|
| **Host** | `<PRODUCTION_HOST>` |
| **Container runtime** | `<CONTAINER_RUNTIME>` |
| **Users** | `<RUNTIME_USER>` (runs container), `<DEPLOY_USER>` (SSH deployment) |
| **Deployment directory** | `<DEPLOYMENT_PATH>` |
| **Source / CI** | `<GITEA_HOST>` |
| **Runner** | `<GITEA_RUNNER_NAME>` |
| **Network** | Dedicated container network `crypto-agent` |
| **Dashboard** | FastAPI on container port `<CONTAINER_PORT>`, published via `WEB_UI_PUBLISH` to host port `<DASHBOARD_PORT>`; Nginx external HTTPS port `<NGINX_PORT>` |
| **Outbound traffic** | `api.kraken.com`, container registry, Telegram API |

### Repository layout

```
dca-bot/
├── main.py                 # Entrypoint
├── bot/                    # Trading engine
│   ├── core.py             # Trading loop, scheduling, dip/deposit logic
│   ├── api_client.py       # Kraken API client
│   ├── config.py           # Configuration loader/validator
│   ├── store.py            # Atomic JSON transaction storage
│   ├── state.py            # Shared runtime state + wake event
│   ├── notifier.py         # Telegram notifier
│   ├── logger.py           # JSON logging + secret redaction
│   ├── alerts.py           # Alert generation
│   ├── demo.py             # Mock exchange / demo data
│   └── utils.py            # Timezone helpers
├── web/                    # FastAPI dashboard
│   ├── app.py              # App factory, page routes
│   ├── server.py           # Uvicorn runner
│   ├── auth.py             # Session auth + CSRF
│   ├── deps.py             # FastAPI dependencies
│   ├── schemas.py          # API schemas
│   └── routers/api.py      # REST API endpoints
├── web/templates/          # Jinja2 HTML templates
├── web/static/             # CSS/JS assets
├── scripts/                # Runtime and maintenance scripts
│   ├── entrypoint.sh
│   ├── healthcheck.sh
│   ├── backup.sh
│   ├── rollback.sh
│   ├── generate_password_hash.py
│   └── generate_demo_data.py
├── systemd/crypto-agent.service
├── nginx/crypto-agent.conf # Example Nginx reverse-proxy config
├── config.json             # Bot configuration (no secrets)
├── .env.example            # Environment variable template
├── .pre-commit-config.yaml # Git hooks
├── .secrets.baseline       # detect-secrets baseline
├── requirements.txt        # Runtime dependencies
└── requirements-dev.txt    # Test/scan dependencies
```

### Directory layout on the host

```
<DEPLOYMENT_PATH>/
├── .current-image          # Last successfully deployed image tag
├── .env                    # compose variables (IMAGE_NAME, IMAGE_TAG, WEB_UI_PUBLISH)
├── .image-env              # systemd EnvironmentFile (IMAGE_NAME, IMAGE_TAG)
├── compose.yaml            # Podman Compose manifest
├── data/
│   ├── config.json         # Bot configuration (managed by dashboard + CI)
│   ├── transactions.json   # Trade history
│   └── audit_log.json      # Config/control change audit log
├── config/
│   └── .env                # Container secrets (Kraken API, Telegram, LIVE_TRADING, dashboard)
├── scripts/                # Host-side scripts copied by CI
├── systemd/                # systemd unit copied by CI
├── logs/                   # JSON-lines logs written by the bot
└── backups/                # Timestamped tar.gz backups
```

---

## 2. Repository & Code

See [`ARCHITECTURE.md`](../ARCHITECTURE.md) for a high-level module overview.

Key modules:

- `bot/core.py` — `KrakenDCA` trading loop, scheduling, dip detection, deposit detection, manual buy handling.
- `bot/api_client.py` — `KrakenAPI`: authenticated and public Kraken requests, OHLC data, balance.
- `bot/config.py` — `Config`: loads `config.json` and environment variables, validates settings.
- `bot/store.py` — `TransactionStore`: atomic JSON persistence with schema migration.
- `bot/state.py` — `BotState`: shared runtime state, warnings, and `wake()` event for immediate pause/resume/manual buy reaction.
- `bot/notifier.py` — `TelegramNotifier`: optional Telegram messages.
- `bot/logger.py` — JSON-lines logging, ANSI stripping, secret redaction, `stdout` capture.
- `web/app.py` / `web/routers/api.py` — FastAPI dashboard and REST API.
- `web/auth.py` — Session authentication, bcrypt password hashing, CSRF tokens.

### Safety features

- `LIVE_TRADING_ENABLED` defaults to `false`; the bot runs in dry-run mode unless explicitly enabled.
- API credentials are read **only** from environment variables (`KRAKEN_API_KEY`, `KRAKEN_API_SECRET`), never from `config.json`.
- The dashboard password is stored as a bcrypt hash, not plaintext.
- The container runs as non-root user `<RUNTIME_USER>`.
- Container capabilities are dropped (`cap_drop: ALL`) and `no-new-privileges:true` is set.
- The container root filesystem is read-only; only `/app/data`, `/app/logs`, `/app/backups`, and `/tmp` (tmpfs) are writable.
- A structured heartbeat file `data/heartbeat.json` is written atomically every poll cycle for the container health check.
- Config changes made in the dashboard are written atomically and the bot calls `config.reload()` so they take effect without a restart (trading-pair changes still require a restart).

### Telegram notifier

Sends plain-text messages on:

1. Bot startup (mode, pair, amount, live/dry-run status).
2. Every placed buy order (or dry-run buy).
3. Buy execution errors.

Enabled only when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. Notification failures are logged but never crash the bot.

---

## 3. Container & Deployment

### `Dockerfile`

- Base image: `python:3.11.12-slim-bookworm`
- Creates non-root user `<RUNTIME_USER>`
- Installs `curl` for HTTP health checks
- Installs pinned Python dependencies from `requirements.txt`
- Copies `main.py`, `config.json`, `bot/`, `web/`, and runtime scripts

### `compose.yaml`

- Project name `crypto-agent`
- Image resolved from `${IMAGE_NAME}:${IMAGE_TAG}`
- Reads container env file from `<DEPLOYMENT_PATH>/config/.env`
- Mounts:
  - `config.json`
  - `data/` directory (transactions, audit log, heartbeat, runtime overrides)
  - `logs/` directory
  - `backups/` directory
- Non-root runtime user, `cap_drop: ALL`, `no-new-privileges:true`
- Read-only root filesystem (`read_only: true`)
- `/tmp` mounted as a `tmpfs` volume
- Resource limits: 1.0 CPU, 512 MB memory limit, 128 MB memory reservation
- Process limit: `pids_limit: 100`
- Graceful shutdown: `stop_signal: SIGTERM`, `stop_grace_period: 60s`
- Dashboard port published via `${WEB_UI_PUBLISH:-0.0.0.0:<DASHBOARD_PORT>:<CONTAINER_PORT>}`
- Container health check via `/app/scripts/healthcheck.sh` (HTTP probes to `/health/live` and `/health/ready`)

### systemd unit

`/etc/systemd/system/crypto-agent.service` is installed by the pipeline. It:

- Sources `<DEPLOYMENT_PATH>/.image-env` for the image tag
- Starts the container on boot using `<CONTAINER_RUNTIME>-compose up -d --no-recreate`
- Stops the container on shutdown with a 60-second timeout (`TimeoutStopSec=60`)

### Reverse proxy

The production host uses Nginx to terminate HTTPS and forward to the container's published port. An example config is in `nginx/crypto-agent.conf`.

---

## 4. CI/CD Pipeline

File: `.gitea/workflows/deploy.yml`

### Triggers

- Manual `workflow_dispatch` only — pushes to `main` do **not** deploy automatically.
  A production release always requires a human to run the workflow (see `docs/RELEASE.md`).

### Concurrency

Only one production deploy runs at a time (`production-deploy` group).

### Pipeline steps

1. **Checkout**
2. **Validate repository contents**
   - Checks required files exist
   - Ensures `config.json` does not contain `api_key` / `api_secret`
   - Python syntax check (`py_compile`)
3. **Set immutable image tag** — uses Git SHA as `IMAGE_TAG`
4. **Build production image** — Podman build, tags with SHA and `latest`
5. **Run tests inside built image** — installs dev deps, runs `pytest`, runs `detect-secrets` scan
6. **Login to Gitea registry** — on the runner
7. **Push image** — SHA tag + `latest`
8. **Check Telegram secrets are defined**
9. **Deploy to production host**
   - SSH as `<DEPLOY_USER>` to `<PRODUCTION_HOST>`
   - Copies `compose.yaml`, scripts, and systemd unit
   - Copies `config.json` **only if it does not already exist** on the host
   - Ensures `transactions.json` exists
   - Writes `<DEPLOYMENT_PATH>/config/.env` from Gitea secrets/variables (mode `600`, root-owned)
   - Writes `<DEPLOYMENT_PATH>/.env` with `IMAGE_NAME`, `IMAGE_TAG`, `WEB_UI_PUBLISH`
   - Pulls the new image and starts container with `<CONTAINER_RUNTIME>-compose up -d --force-recreate`
10. **Wait for health** — polls container health status up to 30 times / 5 seconds
11. **Persist successful tag** — writes `.current-image` and `.image-env`
12. **Install systemd unit** — copies service file, daemon-reload, enable
13. **Rollback on failure** (only if deploy step fails)
    - Pulls previous tag
    - Rewrites `.env` with rollback image
    - Restarts container
    - Updates `.current-image` and `.image-env`
14. **Clean up old images** — keeps current and rollback tags, removes others

### Required Gitea Actions configuration

All values are configured in the Gitea repository under **Settings → Secrets / Variables → Actions**.

#### Secrets (must be marked **Secret**)

| Name | Purpose |
|------|---------|
| `KRAKEN_API_KEY` | Kraken API public key |
| `KRAKEN_API_SECRET` | Kraken API private key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `WEB_UI_PASSWORD_HASH` | Bcrypt hash of the dashboard admin password |
| `SESSION_SECRET` | Strong random value for signing session cookies |
| `OIDC_CLIENT_ID` | Authentik client ID for production (optional) |
| `OIDC_CLIENT_SECRET` | Authentik client secret for production (optional) |
| `REGISTRY_USERNAME` | Gitea container registry username |
| `REGISTRY_PASSWORD` | Gitea container registry password or token |
| `SSH_PRIVATE_KEY` | SSH private key for the `<DEPLOY_USER>` user on the production host |

#### Variables (may be **Variable**, not Secret)

| Name | Typical value | Purpose |
|------|---------------|---------|
| `LIVE_TRADING_ENABLED` | `false` → `true` | Safety switch for real orders |
| `WEB_UI_ENABLED` | `true` | Enable the FastAPI dashboard |
| `WEB_UI_HOST` | `0.0.0.0` | Dashboard bind address inside the container |
| `WEB_UI_PORT` | `8000` | Dashboard port inside the container |
| `WEB_UI_PUBLISH` | `0.0.0.0:<DASHBOARD_PORT>:<CONTAINER_PORT>` | Port published on the Docker/Podman host |
| `WEB_UI_SECURE_COOKIE` | `true` | Set `Secure` on session cookies (use only with HTTPS) |
| `OIDC_ENABLED` | `false` | `true` to enable Authentik OIDC login |
| `OIDC_ISSUER_URL` | `https://auth.example.com/application/o/dca-bot/` | Authentik issuer URL |
| `OIDC_REDIRECT_URI` | `https://dashboard.example.com/auth/callback` | OIDC callback URL |
| `OIDC_SCOPES` | `openid email profile` | OIDC scopes to request |

### Rollback

The pipeline records the previously deployed tag in `<DEPLOYMENT_PATH>/.current-image`. If a deploy fails the health check, it automatically rolls back to that tag.

Manual rollback:

```bash
ssh <DEPLOY_USER>@<PRODUCTION_HOST>
cd <DEPLOYMENT_PATH>
# Replace with the desired previous SHA tag
echo '<ROLLBACK_TAG>' > .current-image
echo "IMAGE_NAME=<REGISTRY_HOST>/<IMAGE_PATH>" > .env
echo "IMAGE_TAG=<ROLLBACK_TAG>" >> .env
sudo <CONTAINER_RUNTIME>-compose -p crypto-agent -f compose.yaml up -d --force-recreate
```

---

## 5. Configuration

For the complete `config.json` and `.env` reference, see [`docs/configuration.md`](configuration.md).

### `config.json`

Example recurring-mode configuration:

```json
{
  "mode": "recurring",
  "dca_end_date": null,
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

**Important:** Never put API keys in `config.json`. The CI pipeline rejects commits that contain `"api_key"` or `"api_secret"` in `config.json`.

### Environment variables / secrets

| Name | Source | Purpose |
|------|--------|---------|
| `KRAKEN_API_KEY` | Gitea secret / `.env` | Kraken API public key |
| `KRAKEN_API_SECRET` | Gitea secret / `.env` | Kraken API private key |
| `LIVE_TRADING_ENABLED` | Gitea variable / `.env` | `true` to place real orders, otherwise dry-run |
| `TELEGRAM_BOT_TOKEN` | Gitea secret / `.env` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Gitea secret / `.env` | Telegram chat ID |
| `WEB_UI_ENABLED` | Gitea variable / `.env` | `true` to start the dashboard |
| `WEB_UI_HOST` | Gitea variable / `.env` | Dashboard bind address |
| `WEB_UI_PORT` | Gitea variable / `.env` | Dashboard port |
| `WEB_UI_USERNAME` | Gitea variable / `.env` | Dashboard admin username |
| `WEB_UI_PASSWORD_HASH` | Gitea secret / `.env` | Bcrypt password hash |
| `SESSION_SECRET` | Gitea secret / `.env` | Session cookie signing secret |
| `WEB_UI_SECURE_COOKIE` | Gitea variable / `.env` | `true` for HTTPS-only cookies |
| `DEMO_MODE` | `.env` / env | `true` disables real exchange calls |
| `LOG_LEVEL` | `.env` / env | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `OIDC_ENABLED` | `.env` / env | `true` to enable OIDC (Authentik) login |
| `OIDC_ISSUER_URL` | `.env` / env | OIDC provider issuer URL |
| `OIDC_CLIENT_ID` | `.env` / env | OIDC client ID |
| `OIDC_CLIENT_SECRET` | `.env` / env | OIDC client secret |
| `OIDC_REDIRECT_URI` | `.env` / env | OIDC callback URL (optional, defaults to `/auth/callback`) |
| `OIDC_SCOPES` | `.env` / env | OIDC scopes to request (default: `openid email profile`) |
| `IMAGE_NAME` | Workflow / `.env` | Registry image path |
| `IMAGE_TAG` | Workflow / `.env` | Git SHA of deployed commit |
| `WEB_UI_PUBLISH` | Gitea variable / `.env` | Host port mapping for the dashboard |

### `.env` files

- `<DEPLOYMENT_PATH>/.env` — used by `<CONTAINER_RUNTIME>-compose` for variable substitution (`IMAGE_NAME`, `IMAGE_TAG`, `WEB_UI_PUBLISH`).
- `<DEPLOYMENT_PATH>/config/.env` — loaded by the container at runtime for secrets and runtime config. Written by CI from Gitea secrets/variables.
- `<DEPLOYMENT_PATH>/.image-env` — used by systemd on boot.
- `.env.example` — template in the repo; never contains real values.

---

## 6. Security

### Host / OS

- Container runtime runs **rootful** Podman to match the existing finance-dashboard setup.
- `<DEPLOY_USER>` user has passwordless sudo only for specific Podman and systemd commands.
- SSH access uses a dedicated deploy key stored as `SSH_PRIVATE_KEY` in Gitea.
- Outbound traffic is limited to `api.kraken.com`, the Gitea registry, and Telegram.

### Container

- Runs as non-root user `<RUNTIME_USER>`.
- Capabilities dropped (`cap_drop: ALL`) and `no-new-privileges:true`.
- Only `/tmp`, `/app/data`, `/app/logs`, and `/app/backups` are writable.
- No shell or package manager left exposed beyond the Python runtime.

### Kraken API

- API key requires only **Query Funds** and **Create & Modify Orders**.
- **Withdraw Funds** must remain disabled.
- Credentials are never committed; they are injected via Gitea secrets at deploy time.

### Dashboard

- Session-based authentication with bcrypt password hashing.
- Signed session cookies (`HttpOnly`, `Secure` when behind HTTPS, `SameSite=Lax`).
- CSRF tokens on all state-changing requests.
- Secrets are masked in UI and API responses.
- Logs automatically redact API keys, secrets, tokens, and passwords.

### Secret scanning

- `.pre-commit-config.yaml` runs `detect-secrets` and blocks committed `.env` files or `api_key`/`api_secret` in `config.json`.
- The CI pipeline runs `detect-secrets scan --baseline .secrets.baseline --all-files` inside the test container.
- `.secrets.baseline` is checked in and must be updated when new false positives are approved. See [Git & Secrets Management](#11-git--secrets-management) for how to maintain it.

---

## 7. Web Dashboard

See [`docs/WEB_DASHBOARD.md`](WEB_DASHBOARD.md) for detailed setup and reverse-proxy guidance.

### Navigation

The dashboard header is visible on every page and contains:

- **Page title**
- **Quick controls**: Buy Now, Pause/Resume, Stop
- **Status badges**: bot status, exchange connection, Telegram connection, warning count, live/dry-run mode banner

Main pages:

1. **Dashboard** — KPI cards, market chart, recent transactions, next cycle, monthly budget.
2. **Performance** — portfolio P/L, charts, metrics.
3. **Transactions** — search, filter, sort, paginate, export.
4. **Settings** — edit `config.json` with validation, backup/restore, runtime overrides.
5. **Logs** — view, filter, archive, and download JSON-lines logs.
6. **Alerts** — active alerts and acknowledgement.
7. **Audit** — configuration and control changes.
8. **Backups** — create and list host-side backups.

### Controls

- **Pause/Resume** — stops/resumes scheduled and dip buys immediately via `state.wake()`.
- **Buy Now** — triggers one manual buy immediately if the bot is running and not paused.
- **Stop** — stops the bot process. Restart via systemd or the pipeline.

### Configuration persistence

- Saving settings in the dashboard writes `config.json` on the host and calls `config.reload()` in the trading loop.
- Changes to `trading_pair` or API credentials still require a container restart to take full effect.
- Every save creates an entry in the audit log and a backup copy of the previous `config.json`.

---

## 8. Operations

### View logs

Container logs:

```bash
sudo <CONTAINER_RUNTIME> logs --tail 50 crypto-agent
```

Follow logs:

```bash
sudo <CONTAINER_RUNTIME> logs -f crypto-agent
```

Bot JSON-lines logs on the host:

```bash
sudo tail -f <DEPLOYMENT_PATH>/logs/app.json
```

Archive older logs from the dashboard or manually:

```bash
sudo ls -la <DEPLOYMENT_PATH>/logs/
```

### Check container health

The container health check now probes two HTTP endpoints exposed by the dashboard:

```bash
# Liveness: confirms the application process responds
curl -fsS http://127.0.0.1:<DASHBOARD_PORT>/health/live

# Readiness: confirms configuration, storage, and recent trading-loop heartbeat
curl -fsS http://127.0.0.1:<DASHBOARD_PORT>/health/ready
```

Inside the container the same endpoints are available on port `<CONTAINER_PORT>`:

```bash
sudo <CONTAINER_RUNTIME> exec crypto-agent curl -fsS http://127.0.0.1:<CONTAINER_PORT>/health/live
sudo <CONTAINER_RUNTIME> exec crypto-agent curl -fsS http://127.0.0.1:<CONTAINER_PORT>/health/ready
```

Docker/Podman also reports the composed health status:

```bash
sudo <CONTAINER_RUNTIME> ps --filter name=crypto-agent
sudo <CONTAINER_RUNTIME> inspect --format='{{.State.Health.Status}}' crypto-agent
```

### Inspect container environment

```bash
sudo <CONTAINER_RUNTIME> exec crypto-agent env
```

**Do not log or share the full output** — it contains secrets.

### View transactions

```bash
sudo cat <DEPLOYMENT_PATH>/data/transactions.json
```

### Pause / resume / manual buy

Use the dashboard header buttons or call the API directly:

```bash
# Pause
curl -X POST https://your-host/api/bot/pause

# Resume
curl -X POST https://your-host/api/bot/resume

# Manual buy now (rejected if paused or stopped)
curl -X POST https://your-host/api/bot/cycle
```

Pause/resume and manual buy react within ~1 second because the trading loop waits on `state.wake()`.

### Restart the container

```bash
sudo <CONTAINER_RUNTIME>-compose -p crypto-agent -f <DEPLOYMENT_PATH>/compose.yaml restart
```

### Stop the container

```bash
sudo <CONTAINER_RUNTIME>-compose -p crypto-agent -f <DEPLOYMENT_PATH>/compose.yaml down
```

### Start on boot

The systemd unit is installed by the pipeline:

```bash
sudo systemctl status crypto-agent.service
sudo systemctl enable crypto-agent.service
sudo systemctl start crypto-agent.service
```

### Trigger a redeploy

Push any commit to `main`, or run manually from the Gitea UI.

### Force a manual rollback

```bash
ssh <DEPLOY_USER>@<PRODUCTION_HOST>
cd <DEPLOYMENT_PATH>
# Replace with the desired previous SHA tag
echo '920d62e...' > .current-image
echo "IMAGE_NAME=<REGISTRY_HOST>/<IMAGE_PATH>" > .env
echo "IMAGE_TAG=920d62e..." >> .env
sudo <CONTAINER_RUNTIME>-compose -p crypto-agent -f compose.yaml up -d --force-recreate
```

### Backups

Backups are created by `scripts/backup.sh` and the `dca-bot-backup.service` timer.
They are validated, checksummed, and pruned according to a daily/weekly retention
policy. See `docs/operations/BACKUP_RESTORE_RUNBOOK.md` for the complete runbook.

Run a host-side backup manually:

```bash
sudo <DEPLOYMENT_PATH>/scripts/backup.sh
```

Install and enable the scheduled systemd timer:

```bash
sudo cp <DEPLOYMENT_PATH>/systemd/dca-bot-backup.service \
        <DEPLOYMENT_PATH>/systemd/dca-bot-backup.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dca-bot-backup.timer
```

Verify:

```bash
sudo systemctl list-timers dca-bot-backup.timer
sudo systemctl status dca-bot-backup.service
```

Backups include `config.json`, `transactions.json`, `audit_log.json`,
`runtime_overrides.json`, `strategy_state.json`, `heartbeat.json`, `state.json`,
and `transactions.db` if present.

Secrets in `config/.env` are intentionally excluded. Caches, logs, container
layers, and temporary files are also excluded.

---

## 9. Telegram Notifications

### Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot, and copy the token.
2. Get your chat ID:
   - Message [@userinfobot](https://t.me/userinfobot) and copy the **Id**, or
   - Message your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.
3. In Gitea, add repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Redeploy.

### Messages you will receive

- **Startup**: mode, pair, amount, live/dry-run status
- **Dry-run buy**: order number, amount, price, "no real order placed"
- **Live buy**: order number, amount, price, total cost
- **Error**: error description if a buy fails

---

## 10. Enabling Live Trading

**Only proceed after dry-run has been running without errors.**

### Pre-flight checklist

1. Confirm dry-run logs show `DRY RUN: skipped placing order` and no errors.
2. Confirm Telegram startup and dry-run messages are arriving.
3. Deposit fiat into your Kraken account.
4. Verify the Kraken API key has only **Query Funds** and **Create & Modify Orders**.
5. Ensure `crypto_amount` is large enough to meet Kraken's minimum order size (see below).
6. Keep `crypto_amount` small for the first real test.

### Kraken minimum order size

Kraken rejects orders where `crypto_amount × current_price` is below the minimum (typically ~10 EUR/CHF/USD depending on the pair). If you see:

```
Kraken API Error: EGeneral:Invalid arguments:volume minimum not met
```

Increase `crypto_amount` in the dashboard until `crypto_amount × price` is above the minimum, then save.

### Enable live trading

1. Run the production preflight and confirm a green result:
   ```bash
   sudo <CONTAINER_RUNTIME> exec crypto-agent python scripts/preflight_production.py --json
   ```
   Look for `"overall": "PASS"` and `"can_place_live_orders": true`.
2. Go to `https://<REGISTRY_HOST>/<IMAGE_PATH>/settings/variables/actions`.
3. Set `LIVE_TRADING_ENABLED` to `true`.
4. Trigger a redeploy (push an empty commit or run workflow manually).

If the preflight is not green, the bot will refuse to place real orders even when
`LIVE_TRADING_ENABLED=true`.

### Verify

```bash
sudo <CONTAINER_RUNTIME> logs --tail 30 crypto-agent
```

Look for:

```
WARNING: LIVE TRADING IS ENABLED. REAL MARKET ORDERS WILL BE PLACED.
```

Then confirm:

- **Orders → Closed** on Kraken shows the market buy order.
- `transactions.json` contains the new trade.
- Telegram shows `✅ Buy order placed`.

### Disable live trading

Set `LIVE_TRADING_ENABLED=false` in Gitea variables and redeploy.

---

## 11. Git & Secrets Management

This section covers the repository-side controls that prevent secrets from being committed.

### Files that must never be committed

- `.env` (any `.env.*` except `.env.example`)
- `config.json.bak`
- `*.key`, `*.pem`, `*.secret`
- Runtime files: `data/transactions.json`, `data/runtime_overrides.json`, `data/heartbeat.json`
- Local directories: `data/`, `backups/`, `logs/`, `exports/`, `.venv/`, `__pycache__/`, `.pytest_cache/`

These are already listed in `.gitignore`.

### Pre-commit hooks

Install the hooks once per clone:

```bash
pip install pre-commit detect-secrets
pre-commit install
```

The hooks run on every commit:

1. `detect-secrets` — scans staged files against `.secrets.baseline`.
2. `check-for-env-files` — rejects any file matching `\.env$`.
3. `check-for-secrets-in-config` — rejects `config.json` containing `api_key` or `api_secret`.

### Updating `.secrets.baseline`

If `detect-secrets` reports a new potential secret, review it carefully. For false positives, add an inline allowlist comment:

```python
# pragma: allowlist secret
api_key = "not-a-real-key"
```

To regenerate the baseline after approving changes:

```bash
detect-secrets scan --baseline .secrets.baseline --all-files
```

Commit the updated `.secrets.baseline`.

**Note:** The CI pipeline runs `detect-secrets scan --baseline .secrets.baseline --all-files` inside the test container. Keep the baseline file in sync with the repository.

### Rotating secrets

To rotate a secret without changing application code:

1. Update the value in **Gitea → Settings → Secrets → Actions**.
2. Trigger a redeploy (push or workflow_dispatch).
3. The pipeline rewrites `<DEPLOYMENT_PATH>/config/.env` with the new value.
4. The container is recreated and picks up the new secret on startup.

For Kraken API key rotation:

1. Generate a new key on Kraken with the same minimal permissions.
2. Update `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` in Gitea.
3. Redeploy.
4. Only then revoke the old key on Kraken.

---

## 12. Troubleshooting

### Container shows `IMAGE_NAME must be set`

Ensure `<DEPLOYMENT_PATH>/.env` exists and contains:

```
IMAGE_NAME=<REGISTRY_HOST>/<IMAGE_PATH>
IMAGE_TAG=<sha>
```

### `sudo: sorry, you are not allowed to preserve the environment`

Fixed by writing compose variables to `<DEPLOYMENT_PATH>/.env` instead of relying on `sudo -E`.

### Telegram secrets are empty in the container

Check the Gitea Actions diagnostic step. Ensure secrets are saved under **Settings → Secrets → Actions** with exact names `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then redeploy.

### `API Connection Failed`

- Verify `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` secrets.
- Verify the API key is not expired.
- Verify the key has the required permissions.

### Bot does not place orders

- Check available fiat balance. If it is 0, the bot waits.
- Check `max_price`: if current price is above it, buys are skipped.
- Check `max_monthly_amount`: if the limit is reached, buys are skipped.
- Verify `LIVE_TRADING_ENABLED` is set as expected.
- Verify `crypto_amount` meets Kraken's minimum order size.

### Manual buy does not execute

- The bot must be running (`status` not `stopped`).
- The bot must not be paused.
- The dashboard/API will return `400` with a clear reason if either is true.

### Dashboard changes are not reflected

- Most numeric/strategy settings reload automatically.
- `trading_pair` and API credential changes require a container restart.

### Need to inspect the running bot

```bash
sudo <CONTAINER_RUNTIME> exec -it crypto-agent /bin/bash
# or just run Python directly
sudo <CONTAINER_RUNTIME> exec -it crypto-agent python main.py
```

---

## 13. Emergency Stop

To immediately stop all bot activity:

```bash
ssh <DEPLOY_USER>@<PRODUCTION_HOST>
sudo <CONTAINER_RUNTIME>-compose -p crypto-agent -f <DEPLOYMENT_PATH>/compose.yaml down
sudo systemctl disable crypto-agent.service
```

To restart later:

```bash
sudo systemctl enable crypto-agent.service
sudo systemctl start crypto-agent.service
```

---

## 14. Staging & Production OIDC/SSO

Both instances can optionally use OIDC/SSO via Authentik. The bot-side code and workflow injection are identical; only the Gitea secrets/variables differ.

| Environment | Gitea variable prefix | Authentik application slug example |
|---|---|---|
| Staging | `STAGING_OIDC_*` | `dca-bot-staging` |
| Production | `OIDC_*` (no prefix) | `dca-bot` |

### Staging

A separate staging instance is defined in `compose.staging.yaml` and `.gitea/workflows/deploy-staging.yml`. It is hardcoded to `APP_ENV=staging` and `TRADING_MODE=demo`, so it never places real orders regardless of other settings.

When `STAGING_OIDC_ENABLED=true` is set as a Gitea variable, the staging pipeline injects the OIDC secrets into `<STAGING_DEPLOYMENT_PATH>/config/.env` and the login page shows a "Sign in with Authentik" button. Local password login remains available as a fallback. See `docs/WEB_DASHBOARD.md` for the Authentik provider setup.

### Production

Production OIDC uses the same bot code and `/auth/callback` route, but the production workflow (`.gitea/workflows/deploy.yml`) reads variables/secrets without the `STAGING_` prefix. To enable production OIDC:

1. Create a separate Authentik OAuth2 provider/application for production (e.g., `dca-bot`).
2. In Gitea, set production variables/secrets:
   - `OIDC_ENABLED=true`
   - `OIDC_ISSUER_URL`
   - `OIDC_CLIENT_ID`
   - `OIDC_CLIENT_SECRET`
   - `OIDC_REDIRECT_URI`
   - `OIDC_SCOPES`
3. Merge the validated `staging` branch into `main` and run the production deploy workflow.

OIDC defaults to `false` in both environments, so a production deploy with the new code will not enable OIDC until the variables are explicitly set.

---

## 15. Future Roadmap

The following items are planned or recommended but not yet fully implemented.

### Monitoring & alerting

- Container health check uses `/app/scripts/healthcheck.sh` and HTTP endpoints `/health/live` and `/health/ready`.
- Add external monitoring (e.g., Uptime Kuma, Prometheus node-exporter) against `/health/ready`.
- Alert if container is unhealthy for more than N minutes.
- Alert if no Telegram message is received for an extended period.

### Multi-pair support

- Extend `config.json` to support multiple trading pairs with independent amounts and limits.

### Enhanced dip logic

- Configurable dip buy amount (different from regular DCA amount).
- Maximum number of dip buys per cycle.

### Paper trading mode

- A mode that records simulated trades without calling Kraken’s `AddOrder` endpoint, for strategy validation.

### Secret rotation

- Document procedure for rotating Kraken API keys and Telegram bot tokens without downtime.

## 16. Related documents

- [`docs/configuration.md`](../docs/configuration.md) — full `config.json` and `.env` reference.
- [`docs/secrets.md`](../docs/secrets.md) — secret management and rotation.
- [`docs/operations/GO_LIVE_CHECKLIST.md`](operations/GO_LIVE_CHECKLIST.md) — production go-live checklist.
- [`docs/operations/RELEASE_ACCEPTANCE.md`](operations/RELEASE_ACCEPTANCE.md) — release acceptance checklist.
- [`docs/operations/INCIDENT_RUNBOOK.md`](operations/INCIDENT_RUNBOOK.md) — incident response procedures.
- [`docs/operations/BACKUP_RESTORE_RUNBOOK.md`](operations/BACKUP_RESTORE_RUNBOOK.md) — backup and restore.
- [`docs/operations/ORDER_FAILURE_RUNBOOK.md`](operations/ORDER_FAILURE_RUNBOOK.md) — order failure and hold state.
- [`docs/operations/HEALTHCHECKS.md`](operations/HEALTHCHECKS.md) — health checks and container hardening.
- [`docs/operations/MONITORING.md`](operations/MONITORING.md) — external monitoring and alerting.
- [`docs/operations/VALIDATION_RUNBOOK.md`](operations/VALIDATION_RUNBOOK.md) — live-trade validation.
- [`docs/operations/PRODUCTION_PREFLIGHT.md`](operations/PRODUCTION_PREFLIGHT.md) — production preflight and live-order gate.

---

*Last updated: 2026-07-25*
