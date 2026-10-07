# Installation Guide

A complete, self-hosted bot for automated cryptocurrency DCA on Kraken, with a built-in web dashboard. Everything after the first boot is configured in the dashboard UI — no further file editing required.

## Option A — Prebuilt image (recommended)

Prerequisites: Docker with the Compose plugin (v2.24+). A Kraken account.

### 1. Download the compose file and start the bot

```bash
mkdir kraken-dca-bot && cd kraken-dca-bot
curl -O https://raw.githubusercontent.com/mb86231/kraken-dca-bot/main/compose.public.yaml
docker compose -f compose.public.yaml up -d
docker compose -f compose.public.yaml logs -f
```

### 2. Create the admin account (first run only)

When no admin password exists yet, the container log prints a one-time
**setup token** (look for the `FIRST-RUN SETUP` banner). Open
http://localhost:8000 — the login page shows the **First-run setup** form.
Enter the token and choose your admin username and password. You are signed
in immediately, and the setup form never appears again.

The session secret is generated and persisted automatically; everything else
— Kraken keys, Telegram, strategy — is configured in the dashboard UI. If
you prefer pre-seeding credentials via environment variables instead of the
setup flow, add `WEB_UI_USERNAME`, `WEB_UI_PASSWORD_HASH` (generate with
`python scripts/generate_password_hash.py`), and optionally `SESSION_SECRET`
to an `.env` file next to the compose file — they take precedence over the
setup flow and the secrets store.

### 3. Configure the bot

1. **Settings → API Keys**: enter your Kraken API key and secret. Stored in the `dca-bot-data` volume (`data/secrets.json`, mode 0600) — never in `config.json`, never in backups.
2. **Settings → Strategy**: trading pair, buy amount, deposit day/hour, Dynamic DCA tiers.
3. **Preflight**: run the checks; acknowledge the two manual Kraken permission checks after verifying them in your Kraken account.
4. **Top bar → Live Trading**: enable when ready. Until then the bot is in dry-run mode.

### 4. Stopping and upgrading

```bash
docker compose -f compose.public.yaml down          # stop (data persists in volumes)
docker compose -f compose.public.yaml pull && \
docker compose -f compose.public.yaml up -d          # upgrade to the latest image
```

## Option B — Build from source

Prerequisites: Docker with Compose, Git.

```bash
git clone https://github.com/mb86231/kraken-dca-bot.git
cd kraken-dca-bot
cp .env.example .env
nano .env          # at minimum WEB_UI_USERNAME, WEB_UI_PASSWORD_HASH, SESSION_SECRET
docker compose up -d --build
```

`compose.yaml` builds the image locally and offers the full set of hardening and deployment options; `compose.public.yaml` runs the prebuilt registry image.

## Kraken API Key

Create at Kraken → **Settings → API** with **only**:

- ✅ Query Funds
- ✅ Create & Modify Orders
- ❌ Withdraw Funds — keep disabled

Enter the key in the dashboard (Settings → API Keys) or set `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` in `.env` (environment variables take precedence over the stored values).

## Optional: Telegram Notifications

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram to create a bot and get a token.
2. Get your chat ID (e.g. via [@userinfobot](https://t.me/userinfobot)).
3. Enter both in the dashboard (Settings → API Keys) or set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

## Optional: Reverse Proxy / HTTPS

The quick-start dashboard listens on port 8000, bound to `127.0.0.1` by
default. To serve it behind any reverse proxy (nginx, NPM, Traefik, Caddy),
set the bind address and the secure-cookie flag in `.env`:

```env
DCA_BOT_BIND=0.0.0.0
WEB_UI_SECURE_COOKIE=true
```

then `docker compose -f compose.public.yaml up -d`. Do **not** widen the
port via `compose.override.yaml` — compose merges `ports` lists, producing
two bindings for the same host port and a misleading
"address already in use" at startup. Example nginx config and background:
[`docs/WEB_DASHBOARD.md`](docs/WEB_DASHBOARD.md).

## Optional: Single Sign-On (Authentik OIDC)

Settings → Authentication → Authentik (OIDC): enable, then fill in issuer URL, client ID, client secret, and redirect URI. Local password login remains available as a fallback.

## Demo Mode (no credentials, synthetic data)

```bash
git clone https://github.com/mb86231/kraken-dca-bot.git
cd kraken-dca-bot
pip install -r requirements.txt

export DEMO_MODE=true
export WEB_UI_PASSWORD_HASH=$(python scripts/generate_password_hash.py "demo-password")
export SESSION_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
python scripts/generate_demo_data.py
python scripts/run_web_demo.py
```

Open http://127.0.0.1:8000 and sign in with `demo-password`. Demo mode cannot place real orders.

## Data and Backups

| What | Where |
|------|-------|
| Configuration (no secrets) | `dca-bot-data` volume → `/app/data/config.json` |
| Secrets (Kraken, Telegram, admin, OIDC) | `dca-bot-data` volume → `/app/data/secrets.json` (0600, excluded from backups) |
| Transactions, order attempts, bot state | `dca-bot-data` volume |
| Backups | `dca-bot-backups` volume — use **Settings → Backups** in the dashboard |
| Logs | `dca-bot-logs` volume |

## Troubleshooting

- **Dashboard does not start** — check `docker compose logs`; in production `WEB_UI_PASSWORD_HASH` and `SESSION_SECRET` are required (Option A step 1).
- **API connection failed** — re-enter Kraken credentials in Settings → API Keys; verify key permissions on Kraken.
- **Orders rejected: volume minimum not met** — raise the buy amount so `amount × price` exceeds Kraken's minimum (typically ~10 in your quote currency).
- **Pair not found** — use Kraken's exact pair symbols (`XXBTZUSD`, not `BTCUSD`); list: https://api.kraken.com/0/public/AssetPairs

## Further Reading

- Environment variable reference: [`docs/configuration.md`](docs/configuration.md)
- Dashboard & reverse proxy: [`docs/WEB_DASHBOARD.md`](docs/WEB_DASHBOARD.md)
- Secret management: [`docs/secrets.md`](docs/secrets.md)
- Preflight explained: [`docs/operations/PRODUCTION_PREFLIGHT.md`](docs/operations/PRODUCTION_PREFLIGHT.md)
- Operations runbooks: [`docs/operations/`](docs/operations/)
- Development & quality gates: [`docs/development.md`](docs/development.md)

## Disclaimer

Test with small amounts first. Never invest more than you can afford to lose. This is not financial advice. Use at your own risk.
