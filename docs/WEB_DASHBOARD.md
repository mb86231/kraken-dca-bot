# DCA-Bot Web Dashboard

The DCA-Bot includes an optional web dashboard for monitoring and managing the bot without using the console.

## Architecture

- The web dashboard is built with **FastAPI** and runs inside the same container as the bot.
- It is started in a **daemon thread** so it cannot block the trading loop.
- It is **disabled by default**; set `WEB_UI_ENABLED=true` to enable it.
- All secrets (Kraken API, Telegram, dashboard password) are supplied via environment variables only.

## Security

- Session-based authentication with bcrypt password hashing.
- Signed session cookies (`HttpOnly`, `Secure` in production, `SameSite=Lax`).
- CSRF tokens on all state-changing requests.
- Secrets are masked in the UI and API responses.
- API endpoints never return full secrets after they have been stored.
- Logs automatically redact API keys, secrets, tokens, and passwords.

## Header (on every page)

The top bar shows:

- **Page title**
- **Quick controls**: Buy Now, Pause/Resume, Stop
- **Status badges**: bot status, exchange connection, Telegram connection, warning count
- **Mode banner**: "LIVE TRADING" (orange/red) or "Dry Run" (blue)

## Pages

1. **Dashboard** — status, portfolio summary, quick controls, connection status, warnings.
   - KPI cards: Total Invested, Current Value, Unrealized P/L, Available Funds, Planned Buys.
   - Monthly Budget card with progress bar.
   - Next Cycle card with countdown and estimated buys.
   - Recent Transactions card (last 10).
   - Market Price chart with time-range selector and bot buy markers.
2. **Performance** — portfolio P/L, charts, and metrics.
3. **Transactions** — search, filter, sort, paginate, and export trade history.
4. **Settings** — edit `config.json` with validation, backup/restore, runtime overrides.
5. **Logs** — view, filter, archive, and download JSON-lines logs.
6. **Alerts** — active alerts and acknowledgement.
7. **Audit** — configuration and control changes.
8. **Backups** — create and list host-side backups.

The **Holdings**, **Bot Controls**, **Telegram**, and **System Health** standalone pages have been folded into the header or integrated into other pages; the underlying API endpoints remain available.

## Enabling the Dashboard

The dashboard is enabled with `WEB_UI_ENABLED=true` (the default in the
quick-start compose file).

### First run (no configuration needed)

When no admin password is configured anywhere, the bot starts in
**first-run setup mode**:

1. The container log prints a one-time **setup token**
   (`docker compose logs` — look for the `FIRST-RUN SETUP` banner).
   The token changes on every container restart until the admin account
   exists — always take it from the most recent logs.
2. Open the dashboard: the login page shows a **First-run setup** form
   instead of the sign-in form.
3. Enter the setup token and choose your admin username and password.
   The credentials are stored in the secrets store (`data/secrets.json`,
   mode 0600); the session secret is generated and persisted automatically.
4. You are signed in immediately; the setup form never appears again.

### Pre-seeding credentials (optional)

Instead of the setup flow you can provide credentials via environment
variables (they take precedence over the secrets store):

1. Generate a password hash:
   ```bash
   python scripts/generate_password_hash.py
   ```
2. Add to your `.env` file:
   ```env
   WEB_UI_ENABLED=true
   WEB_UI_HOST=127.0.0.1
   WEB_UI_PORT=8000
   WEB_UI_USERNAME=admin
   WEB_UI_PASSWORD_HASH=<hash-from-step-1>
   SESSION_SECRET=<generate-a-strong-random-value>
   WEB_UI_SECURE_COOKIE=false
   ```
3. Bind the dashboard port in `compose.yaml` or via `WEB_UI_PUBLISH`:
   ```env
   WEB_UI_PUBLISH=127.0.0.1:8000:8000
   ```
4. Restart the container:
   ```bash
   docker compose up -d --force-recreate
   ```
5. Open `http://127.0.0.1:8000` and sign in.

## OIDC / SSO Authentication (Authentik)

The dashboard supports OpenID Connect via the standard authorization-code flow with PKCE. When configured, the login page shows a "Sign in with Authentik" button. Local password login remains available as a fallback.

### Authentik application setup

In your Authentik admin panel:

1. Create a new **OAuth2/OpenID Provider**:
   - Name: `DCA-Bot Staging OIDC`
   - Client type: `Confidential`
   - Client ID: `dca-bot-staging`
   - Client secret: a strong random value
   - Redirect URIs: `https://staging-bot.example.com/auth/callback`
   - Scopes: `openid`, `email`, `profile`
2. Create an **Application** and bind it to the provider.
3. Assign users/groups that should access the dashboard.
4. Copy the issuer URL (e.g. `https://authentik.example.com/application/o/dca-bot-staging/`).

### Environment variables

```env
OIDC_ENABLED=true
OIDC_ISSUER_URL=https://authentik.example.com/application/o/dca-bot-staging/
OIDC_CLIENT_ID=dca-bot-staging
OIDC_CLIENT_SECRET=<secret-from-authentik>
OIDC_REDIRECT_URI=https://staging-bot.example.com/auth/callback
OIDC_SCOPES=openid email profile
OIDC_ALLOWED_SUBJECTS=alice,bob@example.com
```

`OIDC_REDIRECT_URI` can be omitted; the dashboard will infer it from the incoming `Host`/`X-Forwarded-*` headers as `/auth/callback`.

`OIDC_ALLOWED_SUBJECTS` is **required in practice**: it is the
comma-separated allow-list of identities (matched against the token's
`sub`, `preferred_username` and `email`) that may log in. The login is
**fail closed** — when OIDC is enabled and the allow-list is empty, *every*
OIDC login is denied. Set it in the environment or in the dashboard
(Settings → Authentication).

## Demo Mode

To try the dashboard without real credentials:

```bash
export DEMO_MODE=true
python scripts/generate_demo_data.py
python main.py
```

Then open `http://127.0.0.1:8000`.

## Reverse Proxy / HTTPS

If you already have Nginx (or another proxy) running, configure it to forward to the host's IP on port `8003`. An example config is in `nginx/crypto-agent.conf`.

> **Quick-start users** (`compose.public.yaml`, the prebuilt image): the
> dashboard is published on port **8000**, not 8003, and loopback-only by
> default — see *Public quick-start: exposing via reverse proxy* below.

### Required environment variables

```env
WEB_UI_ENABLED=true
WEB_UI_HOST=0.0.0.0
WEB_UI_PORT=8000
WEB_UI_PUBLISH=0.0.0.0:8003:8000
WEB_UI_SECURE_COOKIE=true
```

### Nginx upstream

Point your existing Nginx `proxy_pass` to the bot host's IP:

```nginx
location / {
    proxy_pass http://<bot-host-ip>:8003;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Then recreate the container:

```bash
docker compose up -d --force-recreate
```

### Public quick-start: exposing via reverse proxy

`compose.public.yaml` publishes the dashboard on **loopback only** by
default (`${DCA_BOT_BIND:-127.0.0.1}:8000:8000`) so an out-of-the-box
install is never exposed to the network unencrypted. To put it behind your
own reverse proxy (Nginx, Nginx Proxy Manager, Caddy, Traefik, …), set the
bind address in a local `.env` next to the compose file:

```env
DCA_BOT_BIND=0.0.0.0
WEB_UI_SECURE_COOKIE=true
```

```bash
docker compose -f compose.public.yaml up -d
```

Point the proxy at `http://<bot-host-ip>:8000` and terminate TLS there.
With `WEB_UI_SECURE_COOKIE=true` logins only work through the HTTPS proxy
address — direct `http://<ip>:8000` access no longer authenticates, which
is what you want. If you intentionally run plain HTTP on a trusted network
*without* a proxy, set `WEB_UI_SECURE_COOKIE=false` (the default) instead.

> **Do not use `compose.override.yaml` to widen the port.** Docker compose
> *merges* `ports` lists instead of replacing them, so an override adding
> `8000:8000` results in **two bindings for the same host port**
> (`0.0.0.0:8000` *and* `127.0.0.1:8000`). The container then fails to
> start with a misleading `failed to bind host port … address already in
> use`, even though nothing else is listening. `DCA_BOT_BIND` exists
> precisely to avoid this: there is always exactly one binding, and the
> default stays loopback-only. Verify with
> `docker compose -f compose.public.yaml config | grep -A3 ports:`.

### Localhost-only / SSH tunnel

If you do not need remote access, keep:

```env
WEB_UI_HOST=127.0.0.1
WEB_UI_PUBLISH=127.0.0.1:8000:8000
```

and tunnel when needed:

```bash
ssh -L 8000:127.0.0.1:8000 user@bot-host
```

## Important Notes

- The dashboard shares the bot process. Web failures are isolated, but the dashboard is optional for this reason.
- Most configuration changes saved in the dashboard call `config.reload()` and take effect immediately.
- Changes to `trading_pair` or API credentials require a container restart to take full effect.
- Telegram credentials can be entered in the dashboard (Settings → API Keys) and are stored in the secrets store (`data/secrets.json`, mode 0600, excluded from backups); environment variables take precedence when set.
- Pause/resume and manual "Buy Now" react within ~1 second because the trading loop waits on `state.wake()`.
