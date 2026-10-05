# Proposal: Isolated Development Environment

> **Status:** Proposal — not implemented.  
> This document preserves the original development-environment plan for future
> implementation. It does not describe files that currently exist.

---

## Goal

Provide a safe, isolated development environment for the DCA-Bot where developers
can work without risk of touching production data, production secrets, or placing
real orders.

## Proposed files

### 1. `compose.dev.yaml`

Overrides for local development.

```yaml
services:
  crypto-agent:
    env_file:
      - .env.dev
    volumes:
      # Mount local source so code changes are live
      - ./bot:/app/bot:ro
      - ./web:/app/web:ro
      - ./main.py:/app/main.py:ro
      # Separate dev data so prod files are never touched
      - ./dev-data/config.json:/app/config.json
      - ./dev-data:/app/data
      - ./dev-logs:/app/logs
    ports:
      - "127.0.0.1:8000:8000"
```

### 2. `.env.dev.example`

Template for dev environment variables.

```env
# Demo mode: no real exchange calls, synthetic data
DEMO_MODE=true
LIVE_TRADING_ENABLED=false

# Dummy Kraken credentials (never real ones in dev)
KRAKEN_API_KEY=dev-key
KRAKEN_API_SECRET=dev-secret

# Dev dashboard
WEB_UI_ENABLED=true
WEB_UI_HOST=0.0.0.0
WEB_UI_PORT=8000
WEB_UI_USERNAME=admin
WEB_UI_PASSWORD_HASH=$2b$12$...
SESSION_SECRET=dev-session-secret-not-for-prod
WEB_UI_SECURE_COOKIE=false

# Optional: disable Telegram in dev
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

LOG_LEVEL=DEBUG
```

### 3. `.gitignore` additions

```gitignore
.env.dev
dev-data/
dev-logs/
```

## Proposed workflow

```bash
# 1. Create dev env file
mkdir -p dev-data dev-logs
cp .env.dev.example .env.dev
cp config.json dev-data/config.json

# 2. Build and run with dev overrides
docker compose -f compose.yaml -f compose.dev.yaml up -d --build

# 3. Open dashboard
open http://127.0.0.1:8000
```

## Real-API dry-run testing

For real API connectivity testing without placing orders:

```env
DEMO_MODE=false
LIVE_TRADING_ENABLED=false
KRAKEN_API_KEY=<separate-dev-key>
KRAKEN_API_SECRET=<separate-dev-secret>
```

Use a separate Kraken key with **only “Query Funds”** permission. Dry-run never
calls `AddOrder`.

## Dev → prod workflow

1. Develop locally in `DEMO_MODE=true`.
2. Run quality checks:
   ```bash
   pytest tests/ -v
   detect-secrets scan --baseline .secrets.baseline --all-files
   pre-commit run --all-files
   ```
3. Optional real-API smoke test with the restricted dev key in dry-run.
4. Push to `main` (or merge a PR).
5. Prod Gitea workflow deploys using real prod secrets from Gitea Actions.

## Isolation checklist

- [ ] Different project name (`crypto-agent-dev`, not `crypto-agent`).
- [ ] Different data directory (not `<DEPLOYMENT_PATH>`).
- [ ] Different published port.
- [ ] Different container name.
- [ ] Separate `.env` file, never the production one.
- [ ] `LIVE_TRADING_ENABLED=false` and/or `DEMO_MODE=true`.
- [ ] No systemd unit auto-starting the dev container.
