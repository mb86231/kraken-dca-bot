# Installation & Operation — Step by Step

Complete walkthrough from a blank machine to a running bot, covering three
deployment modes. All commands target the Docker quick-start setup
(`compose.public.yaml`, prebuilt image from ghcr.io).

**Contents**

- [Part A — Common Base (all modes)](#part-a--common-base)
- [Mode 1 — Localhost-only (default, safest)](#mode-1--localhost-only-default)
- [Mode 2 — Network access via IP (plain HTTP, trusted LAN)](#mode-2--network-access-via-ip-plain-http-trusted-lan)
- [Mode 3 — Behind a reverse proxy with TLS (recommended for remote access)](#mode-3--behind-a-reverse-proxy-with-tls-recommended-for-remote-access)
- [Part B — First-time Setup in the Dashboard (all modes)](#part-b--first-time-setup-in-the-dashboard)
- [Telegram (optional)](#telegram-optional)
- [Updates](#updates)
- [Troubleshooting](#troubleshooting)

---

## Part A — Common Base

Applies to all three modes. Afterwards, pick **one** of modes 1–3.

### A1 — Prerequisites

- Linux host (or any Docker-capable machine) with Docker + the Compose plugin
- A Kraken account (for the bot itself)

### A2 — Directory and compose file

```bash
mkdir -p ~/kraken-dca-bot && cd ~/kraken-dca-bot
curl -O https://raw.githubusercontent.com/mb86231/kraken-dca-bot/main/compose.public.yaml
```

### A3 — Create `.env` for your mode

The `.env` controls the network binding. Details per mode below — for
mode 1, an empty or absent `.env` is sufficient.

### A4 — Start and get the setup token

```bash
docker compose -f compose.public.yaml up -d
docker compose -f compose.public.yaml logs -f
```

After startup, the logs show a line with the
**FIRST-RUN SETUP TOKEN** — write it down, then press `Ctrl+C`.

**Token lifetime:** the token is valid only until the admin account is
created (Part B1) — and only for the **current container run**. Every
container restart (including recreates caused by `.env` changes in
modes 2/3 below) generates a **new** token and prints it to the logs
again. If you note the token here and restart the container afterwards,
fetch the fresh token from the **most recent** logs:

```bash
docker compose -f compose.public.yaml logs | grep -A6 "FIRST-RUN SETUP"
```

### A5 — Verify

```bash
docker compose -f compose.public.yaml ps
# expected: status "healthy"

docker compose -f compose.public.yaml config | grep -A3 ports:
# expected: exactly ONE ports entry (content depends on the mode)
```

Important: **exactly one binding.** Two entries for the same host port
(e.g. `0.0.0.0:8000` *and* `127.0.0.1:8000`) make the container fail with a
misleading `address already in use` — the typical cause is a
`compose.override.yaml` (Compose merges `ports` lists instead of replacing
them). See [Troubleshooting](#troubleshooting).

---

## Mode 1 — Localhost-only (Default)

The safest mode: the dashboard is reachable **only on the machine itself**.
No `.env` entry needed — `compose.public.yaml` binds to `127.0.0.1` by
default.

```bash
# no .env needed; if the file exists, make sure
# DCA_BOT_BIND is NOT set
docker compose -f compose.public.yaml up -d
```

Expected `docker compose config | grep -A3 ports:` output:

```yaml
    ports:
      - mode: ingress
        ...
        published: "8000"
        host_ip: 127.0.0.1
```

**Access:** local only, `http://localhost:8000`.

**From outside (temporary):** use an SSH tunnel instead of opening a port:

```bash
ssh -L 8000:127.0.0.1:8000 user@bot-host
# then open http://localhost:8000 locally
```

Continue with [Part B](#part-b--first-time-setup-in-the-dashboard).

---

## Mode 2 — Network access via IP (plain HTTP, trusted LAN)

For **trusted networks** (home LAN, VLAN with trusted devices only): the
container listens on all interfaces and you reach it directly at
`http://<host-ip>:8000`. **Warning:** unencrypted HTTP — anyone on the
network can read the traffic (including the login password at first setup
and on every login). Use only if the network is trustworthy.

```bash
cd ~/kraken-dca-bot
printf 'DCA_BOT_BIND=0.0.0.0\nWEB_UI_SECURE_COOKIE=false\n' > .env
docker compose -f compose.public.yaml up -d
```

**The container is recreated now** (`.env` changed) — the setup token
from Part A4 is no longer valid. Fetch the fresh token:

```bash
docker compose -f compose.public.yaml logs | grep -A6 "FIRST-RUN SETUP"
```

Expected `docker compose config | grep -A3 ports:` output:

```yaml
    ports:
      - mode: ingress
        ...
        published: "8000"
        # no host_ip -> binds 0.0.0.0
```

**Access:** `http://<host-ip>:8000` from any device on the network.

Continue with [Part B](#part-b--first-time-setup-in-the-dashboard).

---

## Mode 3 — Behind a Reverse Proxy with TLS (recommended for remote access)

**Recommended mode for remote access** (including over the internet): a
reverse proxy (Nginx Proxy Manager, Caddy, Traefik, nginx) terminates TLS;
the container stays reachable via plain HTTP on the LAN.

### Step 1 — `.env` on the bot host

```bash
cd ~/kraken-dca-bot
printf 'DCA_BOT_BIND=0.0.0.0\nWEB_UI_SECURE_COOKIE=true\n' > .env
docker compose -f compose.public.yaml up -d
```

**The container is recreated now** (`.env` changed) — the setup token
from Part A4 is no longer valid. Fetch the fresh token:

```bash
docker compose -f compose.public.yaml logs | grep -A6 "FIRST-RUN SETUP"
```

- `DCA_BOT_BIND=0.0.0.0` — the proxy on another machine must be able to
  reach the container via its IP.
- `WEB_UI_SECURE_COOKIE=true` — session cookies are marked `Secure`.
  **Consequence:** login only works via the proxy's HTTPS address, no longer
  via `http://<ip>:8000`. This is intentional.

### Step 2 — Create the proxy host

Using **Nginx Proxy Manager** as an example (analogous for others):

1. **Hosts → Proxy Hosts → Add Proxy Host**
2. Domain: `bot.example.com`
3. Scheme: `http`, Forward Hostname: **IP of the bot host**, Forward Port: **8000**
4. **SSL** tab: pick a certificate (e.g. Let's Encrypt), enable **Force SSL**
5. **Websockets Support**: enabled (for live views in the browser)
6. Save

Generic nginx example: [`docs/WEB_DASHBOARD.md`](WEB_DASHBOARD.md)
→ *Reverse Proxy / HTTPS*.

### Step 3 — Verify

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<bot-host-ip>:8000/login
# expected: 200 (the proxy reaches the container)

curl -s -o /dev/null -w "%{http_code}\n" https://bot.example.com/login
# expected: 200 (TLS chain complete)
```

Continue with [Part B](#part-b--first-time-setup-in-the-dashboard) — always
perform the setup via the proxy's **HTTPS address** (Secure cookie), not via
the IP.

---

## Part B — First-time Setup in the Dashboard

Applies to all modes. URL depends on the mode: `http://localhost:8000`
(1), `http://<ip>:8000` (2), or `https://bot.example.com` (3).

### B1 — Create the admin account

On first visit, the login page is a **setup form**: setup token + desired
username + password (min. 8 characters). You are logged in afterwards.

The setup token is printed in the container logs — always take the token
from the **most recent** container run (it changes on every restart until
the admin account exists):

```bash
docker compose -f compose.public.yaml logs | grep -A6 "FIRST-RUN SETUP"
```

### B2 — Enter the Kraken API key

1. Kraken → **Settings → API → Create Key**: enable only the permissions
   **Query Funds** and **Create & Modify Orders** — **never** Withdraw Funds
   or Admin/Transfer.
2. Dashboard → **Settings → API Keys** → enter key + secret, Save.
3. The **Kraken API** badge in the top bar turns green.

### B3 — Configure the strategy

**Settings → Strategy:** trading pair (e.g. `XBTEUR`), amount per buy,
deposit day, buy hour. Optionally enable **Dynamic DCA** — every buy then
scales automatically with the price trend (more on dips, less or none on
rises); the tier table is freely adjustable. Details:
[`docs/configuration.md`](configuration.md).

### B4 — Run the preflight

Menu **Preflight → Run checks**. All automatic checks should be green.
Verify the two manual Kraken notes (key permissions cannot be checked via
API) once in your Kraken account, then **acknowledge** them in the
preflight view.

### B5 — Enable live trading (a deliberate step)

Top bar: click **Live: OFF** and confirm. While the switch is off, the bot
runs in **dry-run**: buys are calculated, logged, and shown on the
dashboard, but **not** placed on Kraken.

---

## Telegram (optional)

1. Message [@BotFather](https://t.me/BotFather) with `/newbot` → write down the token.
2. Find your chat ID (e.g. via [@userinfobot](https://t.me/userinfobot)).
3. Dashboard → **Settings → API Keys** → fill in the Telegram section.
4. Done — the bot sends notifications (startup, buys, errors) and answers
   commands like `/status`, `/price`, `/buy`, `/pause`, `/resume`.
   Security model (chat ID allow-list, one-time confirmation for buy/pause,
   audit log): [`docs/TELEGRAM.md`](TELEGRAM.md).

---

## Updates

```bash
cd ~/kraken-dca-bot
curl -O https://raw.githubusercontent.com/mb86231/kraken-dca-bot/main/compose.public.yaml
docker compose -f compose.public.yaml pull
docker compose -f compose.public.yaml up -d
```

Your `.env` stays in place and is picked up again. Data lives in the named
volumes `dca-bot-data`, `dca-bot-backups`, `dca-bot-logs` — an image update
does not touch them.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `failed to bind host port … address already in use`, but `ss` shows nothing | Duplicate port binding via `compose.override.yaml`. Compose merges `ports` lists → two bindings for the same host port. Fix: delete the override, set the binding via `DCA_BOT_BIND` in `.env` (mode 2/3). |
| `PermissionError: /app/data/config.json` at startup | Old image / old volumes with root ownership. `docker compose pull` (fixed in the image since October 2026), repair volumes once: `docker compose run --rm --user root --entrypoint chown dca-bot -R 1500:1500 /app/data /app/backups /app/logs` |
| `403 CSRF token invalid` at login/setup | Page loaded twice (stale copy). `docker compose pull && up -d`, then press `Ctrl+Shift+R` once. |
| Login fails, but works via the IP | `WEB_UI_SECURE_COOKIE=true` active → login only via the HTTPS proxy address. Intended behaviour (mode 3). |
| `502 Bad Gateway` at the proxy | Container still listening on `127.0.0.1` → set `DCA_BOT_BIND=0.0.0.0` in `.env` and run `up -d` (mode 3, step 1). |
| `Invalid setup token` during first-run setup | The container was restarted or recreated after the token was noted (changing `.env` in modes 2/3 does this) — a **new** token is generated on every start until the admin account exists. Fetch the current one: `docker compose -f compose.public.yaml logs \| grep -A6 "FIRST-RUN SETUP"` |
