# DCA-Bot Sharing Guide

How to share the DCA-Bot repository safely without leaking secrets, private
infrastructure details, or personal information.

---

## What is safe to share

The public repository contains only:

- Application source code (`bot/`, `web/`, `main.py`).
- Container manifests (`Dockerfile`, `compose.yaml`,
  `compose.public.yaml`).
- Documentation and architecture decision records (`docs/`, `ARCHITECTURE.md`,
  `README.md`, `INSTALL.md`).
- Example configuration and environment templates (`config.json`,
  `.env.example`).
- Tests (`tests/`).
- Host-side helper scripts (`scripts/` — the `staging-*.sh` helpers and the
  `systemd/` units are **not** part of the public mirror).
- Vendored front-end libraries (`web/static/vendor/chart.js/`).

All of these are safe to share as long as the verification steps below pass.

## What must never be shared

- `.env` or any `.env.*` file except `.env.example`.
- `config.json.bak` or any backup copy that may contain real values.
- Runtime data: `data/transactions.json`, `data/state.json`,
  `data/runtime_overrides.json`, `data/audit_log.json`, `data/alerts.json`,
  `data/order_attempts.json`, `data/heartbeat.json`.
- Backups: `backups/*.tar.gz`, `backups/*.sha256`, `backups/backup_status.json`.
- Logs: `logs/app.json`, `logs/archive/*.json`.
- Local Python environment: `.venv/`.
- SSH keys, certificates, or any `*.key`, `*.pem`, `*.secret` file.
- CI/CD state such as `.current-image` or `.image-env` from the deployment host.

## Current architecture (what you are sharing)

The bot is a modular Python application, not a single file:

- `bot/core.py` — trading loop, scheduling, dip detection, deposit detection.
- `bot/api_client.py` — Kraken API client (HTTPS + HMAC-SHA512).
- `bot/config.py` — configuration loader and validator.
- `bot/store.py` — atomic JSON transaction storage.
- `bot/state.py` — shared runtime state and persistent overrides.
- `bot/order_execution.py` — order-attempt state machine, retry, reconciliation,
  and hold state.
- `bot/notifier.py` — Telegram notifications.
- `bot/logger.py` — structured JSON logging with secret redaction.
- `bot/alerts.py` — alert generation.
- `bot/demo.py` — mock exchange and synthetic demo data.
- `bot/lock.py` — advisory file locking for shared JSON persistence.
- `web/app.py` / `web/routers/api.py` — FastAPI dashboard and REST API.
- `web/auth.py` — session auth, bcrypt, CSRF.
- `web/security.py` — security headers and production validation.

## Runtime dependencies

The core trading loop uses the Python standard library plus a small set of
pinned third-party packages for the dashboard and tooling. See `requirements.txt`
and `requirements-dev.txt` for the exact versions. Major dependencies include:

- FastAPI, Uvicorn, Starlette
- Jinja2, python-multipart
- Pydantic
- itsdangerous, bcrypt
- slowapi, filelock
- pytest, httpx, ruff, mypy, detect-secrets, pre-commit

## How demo data is used

`DEMO_MODE=true` replaces the real Kraken client with `DemoKrakenAPI`, which
returns synthetic prices and balances. Demo transactions are marked
`"simulated": true` and are not real trades. Demo data is generated locally by
`scripts/generate_demo_data.py` and never leaves your machine unless you
explicitly copy it.

## How secrets are excluded

- No API keys, secrets, or password hashes are committed to Git.
- `.gitignore` excludes `.env`, `.env.*`, `data/`, `backups/`, `logs/`,
  `.venv/`, and runtime backups.
- `detect-secrets` scans every commit against `.secrets.baseline`.
- Pre-commit hooks reject `.env` files and `config.json` containing
  `api_key`/`api_secret`.
- The CI pipeline runs the same secret scan; a finding blocks deployment.

## How to replace internal infrastructure values

Documentation uses placeholders for private deployment details. Before sharing,
review the repository for any remaining hardcoded values and replace them with
these placeholders (or remove them):

| Private value | Placeholder |
|---------------|-------------|
| Production host IP or DNS | `<PRODUCTION_HOST>` |
| Staging host IP or DNS | `<STAGING_HOST>` |
| Gitea hostname | `<GITEA_HOST>` |
| Container image registry path | `<REGISTRY_HOST>` |
| Deployment directory on the host | `<DEPLOYMENT_PATH>` |
| Container runtime (Podman/Docker) | `<CONTAINER_RUNTIME>` |
| Dashboard host port | `<DASHBOARD_PORT>` |
| External Nginx HTTPS port | `<NGINX_PORT>` |

If you are sharing the actual CI workflow files (`.gitea/workflows/*.yml`), they
may still contain real hostnames or paths. Decide whether to:

1. Replace them with placeholders and re-inject them at deploy time, or
2. Keep them private and share only example workflow files.

## Verification checklist before sharing

Run these commands from the repository root and confirm the output is clean:

```bash
# 1. Secret scan must pass
detect-secrets scan --baseline .secrets.baseline --all-files

# 2. No real API keys in config.json
grep -qiE '"api_key"|"api_secret"' config.json && echo "FAIL" || echo "OK"

# 3. No .env files tracked (except .env.example)
git ls-files | grep -E '\.env' | grep -v '^\.env.example$' && echo "FAIL" || echo "OK"

# 4. No runtime data, backups, or logs tracked
git ls-files | grep -E '^(data/|backups/|logs/)' && echo "FAIL" || echo "OK"

# 5. No private keys or certificates tracked
git ls-files | grep -E '\.(key|pem|secret)$' && echo "FAIL" || echo "OK"

# 6. Tests pass in demo mode (no real orders)
pytest tests/ -q

# 7. Static analysis passes
ruff check .
mypy .
```

## Sharing with a friend or another developer

1. Complete the verification checklist above.
2. Confirm `.env.example` contains only placeholder/example values.
3. Confirm `config.json` contains only example settings and no credentials.
4. Remove or replace any remaining personal notes, donation addresses, or
   author details if you do not want to share them.
5. Share the repository archive or grant repository access.
6. Instruct the recipient to copy `.env.example` to `.env` and fill in their own
   values before running anything.

## What the recipient must do before running

1. Install with the quick start in [`README.md`](../README.md) or the
   step-by-step walkthrough in [`docs/INSTALLATION.md`](INSTALLATION.md) —
   no credentials in files are needed: the first container start prints a
   **setup token** to the log, and the admin account is created in the
   dashboard. Note: the token changes on every container restart until the
   admin account exists — always take it from the most recent logs
   (`docker compose -f compose.public.yaml logs | grep -A6 "FIRST-RUN SETUP"`).
2. Create a Kraken API key with **Query Funds** and **Create & Modify Orders**
   permissions only. Never enable **Withdraw Funds**. Enter it in the
   dashboard (Settings → API Keys).
3. Configure pair, amount and schedule (Settings → Strategy), run the
   **Preflight** checks, and only then enable live trading (top bar).
   Until then the bot runs in dry-run mode.
4. Start with `LIVE_TRADING_ENABLED` off / small amounts and validate first.
   Read [`docs/operations/VALIDATION_RUNBOOK.md`](operations/VALIDATION_RUNBOOK.md)
   before enabling live trading.
