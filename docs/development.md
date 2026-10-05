# Development Environment

How to set up a local development environment for the DCA-Bot and run the same
quality gates that CI enforces.

---

## Goal

Develop dashboard, trading logic, and configuration changes without touching
production data, production secrets, or placing real orders.

## Supported platforms

- Python 3.11 or 3.12 on Linux, macOS, or Windows.
- Docker or Podman for container-level testing.

The CI container is built on Python 3.12, but the code must remain compatible
with Python 3.11 because `pyproject.toml` declares `requires-python = ">=3.11,<3.13"`.

## Local setup

```bash
# 1. Create a virtual environment
python3 -m venv .venv

# 2. Activate it
# Linux / macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 4. Copy the environment template
cp .env.example .env

# 5. Run tests
PYTHONPATH=. pytest tests/ -q
```

## Demo mode (default)

The easiest and safest dev setup uses built-in demo mode. Set in `.env`:

```env
DEMO_MODE=true
LIVE_TRADING_ENABLED=false
KRAKEN_API_KEY=demo-key
KRAKEN_API_SECRET=demo-secret
WEB_UI_ENABLED=true
WEB_UI_PASSWORD_HASH=<generate-with-scripts/generate_password_hash.py>
SESSION_SECRET=<generate-with-python-secrets>
WEB_UI_SECURE_COOKIE=false
```

Then run the bot directly:

```bash
PYTHONPATH=. python main.py
```

Open `http://127.0.0.1:8000` and sign in.

Demo mode uses synthetic prices and balances, so no real exchange calls are made.

## Local quality gates

Run the exact same commands that CI runs. From the repository root:

```bash
# Linux / macOS
python3 -m pytest tests/ -q
python3 -m ruff check .
python3 -m mypy .
python3 -m detect_secrets scan --baseline .secrets.baseline --all-files

# Windows (Git Bash / PowerShell)
.venv\Scripts\python -m pytest tests/ -q
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy .
.venv\Scripts\python -m detect_secrets scan --baseline .secrets.baseline --all-files
```

A convenience wrapper is also available:

```bash
bash scripts/ci-test.sh
```

## Pre-commit hooks

Install the hooks once per clone so secret-scanning runs automatically:

```bash
pip install pre-commit detect-secrets
pre-commit install
```

The hooks run:

1. `detect-secrets` against `.secrets.baseline`.
2. `check-for-env-files` — rejects any committed `.env` file.
3. `check-for-secrets-in-config` — rejects `config.json` containing `api_key`
   or `api_secret`.

## Using real exchange credentials in dev

Kraken has no public sandbox. Real credentials can be used safely **only in
dry-run mode** (`LIVE_TRADING_ENABLED=false`).

Use a separate Kraken API key with **only “Query Funds”** permission. Never use
the production key on a dev machine. See `docs/configuration.md` for the full
environment reference.

## Project layout

```
dca-bot/
├── main.py              # Application entrypoint
├── bot/                 # Trading engine
├── web/                 # FastAPI dashboard
├── tests/               # Test suite
├── scripts/             # Runtime and helper scripts
├── systemd/             # systemd unit templates
├── nginx/               # Example reverse-proxy config
├── docs/                # Documentation
├── config.json          # Bot configuration template (no secrets)
├── compose.yaml         # Production Compose manifest
├── compose.staging.yaml # Staging Compose manifest
├── Dockerfile           # Container image definition
├── requirements.txt     # Runtime dependencies
├── requirements-dev.txt # Test/scan dependencies
├── .env.example         # Environment variable template
└── .secrets.baseline    # detect-secrets baseline
```

## Isolation checklist

If you ever run dev on shared infrastructure, verify:

- [ ] Different project/container/network name than production.
- [ ] Different data directory than production.
- [ ] Different published port.
- [ ] Separate `.env` file, never the production one.
- [ ] `LIVE_TRADING_ENABLED=false` and/or `DEMO_MODE=true`.
- [ ] No systemd unit auto-starting the dev container.

## Future improvements

A fully containerized dev setup with live source mounts is proposed in
`docs/proposals/DEVELOPMENT_ENVIRONMENT.md`. That proposal is not yet
implemented; the current workflow uses a local virtual environment.
