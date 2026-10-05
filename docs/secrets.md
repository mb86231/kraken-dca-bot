# Secrets & Git Security Reference

This document is a concise reference for managing secrets, Git hooks, and CI/CD credentials for the DCA-Bot.

> Deployment-specific paths such as `<DEPLOYMENT_PATH>` are placeholders. Replace
> them with your own infrastructure values.

---

## Gitea Actions Configuration

All CI/CD values are configured in the repository under:

- **Settings → Secrets → Actions** for sensitive values
- **Settings → Variables → Actions** for non-sensitive values

### Required Secrets

| Name | Description |
|------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional — can be set in the dashboard instead) |
| `TELEGRAM_CHAT_ID` | Telegram chat ID (optional — can be set in the dashboard instead) |
| `WEB_UI_PASSWORD_HASH` | Bcrypt hash of the dashboard admin password (optional — dashboard secrets store) |
| `SESSION_SECRET` | Strong random value for signing session cookies (optional — dashboard secrets store) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Authentik OIDC client (optional — dashboard secrets store) |
| `REGISTRY_USERNAME` | Gitea container registry username |
| `REGISTRY_PASSWORD` | Gitea container registry password or token |
| `SSH_PRIVATE_KEY` | SSH private key for the `deploy` user on the production host |

> **Note:** Exchange credentials (`KRAKEN_API_KEY` / `KRAKEN_API_SECRET`) and the
> `LIVE_TRADING_ENABLED` flag are **no longer pipeline secrets**. They are managed
> in the dashboard (Settings → Exchange Credentials / Enable Live Trading) and
> stored in `data/secrets.json` and `config.json` on the host. Environment
> variables, when set, still take precedence over dashboard-stored values.

### Required Variables

| Name | Typical value | Purpose |
|------|---------------|---------|
| `LIVE_TRADING_ENABLED` | `false` / `true` | Safety switch for real orders |
| `WEB_UI_ENABLED` | `true` | Enable the FastAPI dashboard |
| `WEB_UI_HOST` | `0.0.0.0` | Dashboard bind address inside the container |
| `WEB_UI_PORT` | `8000` | Dashboard port inside the container |
| `WEB_UI_PUBLISH` | `0.0.0.0:8003:8000` | Port published on the Docker/Podman host |
| `WEB_UI_SECURE_COOKIE` | `true` | Set `Secure` on session cookies (use only with HTTPS) |

### Where secrets end up

The deploy pipeline writes the remaining env-based secrets (Telegram, web/OIDC,
registry is CI-only) into `<DEPLOYMENT_PATH>/config/.env` on the production host:

- File owner: `root`
- File mode: `600`
- Loaded by the container via `env_file` in `compose.yaml`
- Never committed to Git

Dashboard-managed secrets (Kraken credentials, Telegram token/chat ID, web
password hash, session secret, OIDC client settings) live in
`<DEPLOYMENT_PATH>/data/secrets.json`:

- File mode: `600`, owned by the container user
- Written via Settings → Exchange Credentials / Telegram Notifications, or the
  `/api/settings/exchange` and `/api/telegram` endpoints
- Explicitly excluded from backups
- Environment variables take precedence per-field when set

---

## Dashboard-managed secrets

The dashboard stores secrets in `data/secrets.json` (0600). The Settings page
shows a masked status card for each area and tells you which source is active
(environment variables always win over stored values).

| Area | Set via | Env override |
|------|---------|--------------|
| Kraken credentials | Settings → Exchange Credentials | `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` |
| Telegram | Settings → Telegram Notifications | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| Web password hash | secrets store / env | `WEB_UI_PASSWORD_HASH` |
| Session secret | secrets store / env | `SESSION_SECRET` |
| OIDC (Authentik) | secrets store / env | `OIDC_*` |

---

## Local Development Secrets

For local development, copy `.env.example` to `.env` and fill in real values:

```bash
cp .env.example .env
```

Never commit `.env`. It is already listed in `.gitignore`.

---

## Generating Required Values

### Dashboard password hash

```bash
python scripts/generate_password_hash.py
```

Paste the output into `WEB_UI_PASSWORD_HASH`.

### Session secret

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output into `SESSION_SECRET`.

### SSH deploy key

On the production host as the `<DEPLOY_USER>` user:

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/crypto-agent-deploy -N '' -C "crypto-agent-deploy"
```

Then add the **private** key contents to Gitea as `SSH_PRIVATE_KEY` and authorize the **public** key in `~/.ssh/authorized_keys` for the `<DEPLOY_USER>` user.

---

## Git Security

### Files that must never be committed

- `.env` and `.env.*` (except `.env.example`)
- `config.json.bak`
- `*.key`, `*.pem`, `*.secret`
- Runtime files: `data/transactions.json`, `data/runtime_overrides.json`, `data/heartbeat.json`
- Local directories: `data/`, `backups/`, `logs/`, `exports/`, `.venv/`, `__pycache__/`, `.pytest_cache/`

These are listed in `.gitignore`.

### Pre-commit hooks

Install once per clone:

```bash
pip install pre-commit detect-secrets
pre-commit install
```

The hooks will then run on every commit:

1. `detect-secrets` — scans staged files against `.secrets.baseline`
2. `check-for-env-files` — rejects any committed file matching `\.env$`
3. `check-for-secrets-in-config` — rejects `config.json` containing `api_key` or `api_secret`

### Updating `.secrets.baseline`

If `detect-secrets` reports a new potential secret, review it. For false positives, add an inline allowlist comment:

```python
# pragma: allowlist secret
api_key = "not-a-real-key"
```

Regenerate the baseline after approving changes:

```bash
detect-secrets scan --baseline .secrets.baseline --all-files
```

Commit the updated `.secrets.baseline`.

The CI pipeline also runs:

```bash
python3 -m detect_secrets scan --baseline .secrets.baseline --all-files
```

inside the test container. Keep the baseline in sync.

## Related documents

- [`docs/configuration.md`](../docs/configuration.md) — full environment-variable reference.
- [`docs/operations.md`](../docs/operations.md) — deployment and daily operations.
- [`docs/development/QUALITY_GATES.md`](development/QUALITY_GATES.md) — local and CI quality gates.

---

## Secret Rotation

To rotate a dashboard-managed secret (Kraken, Telegram, web, OIDC): update it in
the dashboard (or in `data/secrets.json` on the host) and restart the bot. To
rotate an env-based secret, update it in **Gitea → Settings → Secrets →
Actions**, trigger a redeploy, and the pipeline rewrites
`<DEPLOYMENT_PATH>/config/.env`; the container is recreated and picks up the new
secret on startup.

### Kraken API key rotation

1. Generate a new key on Kraken with the same minimal permissions.
2. Enter the new key and secret in the dashboard (Settings → Exchange
   Credentials) and restart the bot.
3. Confirm the bot is healthy and placing orders (or dry-run) correctly.
4. Only then revoke the old key on Kraken.

### Telegram rotation

1. Generate a new bot token with @BotFather or obtain a new chat ID.
2. Enter the new values in the dashboard (Settings → Telegram Notifications) and
   restart the bot.
3. Send a test message from the dashboard or wait for the next notification.

### Dashboard password rotation

1. Generate a new bcrypt hash:
   ```bash
   python scripts/generate_password_hash.py
   ```
2. Update `WEB_UI_PASSWORD_HASH` in Gitea.
3. Redeploy.
4. Sign in with the new password.

---

## Auditing

- The bot writes configuration and control changes to `/opt/crypto-agent/data/audit_log.json`.
- The dashboard **Audit** page displays these changes.
- Review the audit log after any secret rotation or unexpected control change.
