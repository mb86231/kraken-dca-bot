# DCA-Bot Architecture

## Overview

The DCA-Bot is a single-container Python application that performs dollar-cost averaging on Kraken and exposes an optional web dashboard.

```
┌─────────────────────────────────────────┐
│           Docker Container              │
│  ┌─────────────┐   ┌─────────────────┐  │
│  │  DCA Bot    │   │  FastAPI Web UI │  │
│  │  (thread)   │   │  (daemon thread)│  │
│  └──────┬──────┘   └────────┬────────┘  │
│         │                   │           │
│         └───────┬───────────┘           │
│                 │                       │
│         ┌───────┴───────┐               │
│         │  Shared JSON  │               │
│         │  config.json  │               │
│         │  transactions │               │
│         │  state files  │               │
│         └───────────────┘               │
└─────────────────────────────────────────┘
```

## Modules

- `main.py` — Entrypoint. Starts the web server (if enabled) and the bot.
- `bot/core.py` — Trading loop, scheduling, dip detection, deposit detection.
- `bot/api_client.py` — Kraken API client (urllib + HMAC).
- `bot/config.py` — Configuration loader/validator.
- `bot/store.py` — Atomic JSON transaction storage with schema migration.
- `bot/state.py` — Shared runtime state and persistent runtime overrides.
- `bot/order_execution.py` — Order-attempt state machine, retry, reconciliation, and hold state.
- `bot/notifier.py` — Telegram notifier.
- `bot/logger.py` — Structured JSON logging with secret redaction.
- `bot/alerts.py` — Alert generation and storage.
- `bot/demo.py` — Mock exchange and synthetic demo data.
- `web/app.py` — FastAPI application, templates, auth.
- `web/routers/api.py` — REST API endpoints.
- `web/auth.py` — Session auth, CSRF protection.
- `web/templates/` — Jinja2 HTML templates.
- `web/static/` — CSS and JavaScript.

## Data Flow

1. Bot reads `config.json` and environment variables on startup.
2. Bot runs an infinite loop: immediate buy, then scheduled/dip/deposit buys.
3. Live buys are wrapped in an `OrderAttempt`, persisted to `data/order_attempts.json`, retried on transient failures, and reconciled when the outcome is unknown.
4. Each buy is recorded in `transactions.json` via atomic writes.
5. Runtime state is updated in memory and optionally persisted.
6. Web dashboard reads the same `config.json`, `transactions.json`, and `order_attempts.json`.
6. Bot controls (pause/resume/manual cycle) write to `data/runtime_overrides.json`.

## Security

- Secrets only via environment variables.
- Bcrypt password hashing for dashboard.
- Signed session cookies + CSRF tokens.
- Atomic file writes with backups.
- Secret redaction in logs.
- Demo mode disables real trading.

## Related Documentation

- [`docs/configuration.md`](docs/configuration.md) — `config.json` and environment-variable reference.
- [`docs/operations.md`](docs/operations.md) — deployment, health checks, backups, and incident response.
- [`docs/SECURITY_HARDENING.md`](docs/SECURITY_HARDENING.md) — rate limiting, brute-force protection, CSP, and container hardening.
- [`docs/adr/ADR-001-strategy-framework.md`](docs/adr/ADR-001-strategy-framework.md) — why the pluggable strategy framework is deferred to post-v1.0.
- `docs/adr/ADR-002-persistence-storage.md` — JSON-plus-locking persistence decision for v1.0 (created in a future hardening task).

## Future Migrations

- `scripts/migrate_to_sqlite.py` provides a path from JSON to SQLite if storage reliability becomes a concern.
