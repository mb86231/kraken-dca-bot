# Go-Live Checklist

Checklist to complete before enabling unattended live trading in production.

---

## 1. Infrastructure

- [ ] Production host is provisioned and reachable.
- [ ] `<CONTAINER_RUNTIME>` (Docker or Podman) and Compose plugin are installed.
- [ ] Deployment directory `<DEPLOYMENT_PATH>` is created with correct ownership.
- [ ] Reverse proxy (e.g., Nginx) terminates HTTPS and forwards to the dashboard.
- [ ] Firewall allows outbound HTTPS to `api.kraken.com` and Telegram API.
- [ ] systemd is available if using the provided unit files.

## 2. Secrets and access control

- [ ] Kraken API key created with **only**:
  - Query Funds
  - Create & Modify Orders
- [ ] **Withdraw Funds** permission is disabled.
- [ ] API key IP allowlist configured (recommended).
- [ ] Telegram bot token and chat ID configured (strongly recommended).
- [ ] Dashboard password hash generated with `scripts/generate_password_hash.py`.
- [ ] Stable `SESSION_SECRET` generated (`secrets.token_hex(32)`).
- [ ] `WEB_UI_SECURE_COOKIE=true` if serving over HTTPS.
- [ ] OIDC configured and tested, or disabled (optional).
- [ ] SSH deploy key installed for the `deploy` user.
- [ ] Gitea Actions secrets and variables populated.

## 3. Configuration

- [ ] `config.json` contains valid trading parameters.
- [ ] `mode` is `recurring` or `lump_sum`.
- [ ] `trading_pair` uses Kraken's exact pair string.
- [ ] `crypto_amount` produces an order value above Kraken's minimum for the pair.
- [ ] `dip_threshold_percent`, `max_price`, and `max_monthly_amount` reflect risk tolerance.
- [ ] `config.json` contains no API keys or secrets.

## 4. CI/CD and deployment

- [ ] Gitea Actions workflows are configured.
- [ ] Production deploy workflow is manual-only (`workflow_dispatch`).
- [ ] Staging deploy workflow is configured and triggers on `staging` branch pushes.
- [ ] Quality gates in CI match local gates:
  - `pytest tests/ -q`
  - `ruff check .`
  - `mypy .`
  - `detect-secrets scan --baseline .secrets.baseline --all-files`
  - `python scripts/check_docs.py`

## 5. Backup and monitoring

- [ ] `dca-bot-backup.service` and `dca-bot-backup.timer` installed and enabled.
- [ ] Backup destination directory exists and is writable.
- [ ] At least one manual backup succeeded and produced a `.tar.gz` + `.sha256`.
- [ ] External monitoring checks `/health/live` and `/health/ready`.
- [ ] Heartbeat-age and backup-age alerts configured (e.g., Uptime Kuma).
- [ ] `MONITORING_TOKEN` set if using Prometheus metrics (`/api/metrics`).

## 6. Validation in staging

- [ ] Staging container runs with `APP_ENV=staging` and `DEMO_MODE=true`.
- [ ] Staging dashboard shows demo mode and synthetic balance.
- [ ] Manual "Buy Now" in staging creates a simulated transaction.
- [ ] Health endpoints respond correctly.
- [ ] Telegram test notification succeeds.
- [ ] Backup create/list works from the dashboard.

## 7. Production preflight (optional)

- [ ] Read `docs/operations/PRODUCTION_PREFLIGHT.md`.
- [ ] Optionally run `python scripts/preflight_production.py` to verify the
      production environment.
- [ ] The preflight is advisory: live trading is controlled by
      `LIVE_TRADING_ENABLED` and the order executor's placement-time checks.

## 8. Live-trade validation

- [ ] Read `docs/operations/FIRST_LIVE_TRADE_TEMPLATE.md`.
- [ ] Deploy to production with `LIVE_TRADING_ENABLED=false` first.
- [ ] Confirm dry-run logs show `DRY RUN: skipped placing order`.
- [ ] Confirm Telegram startup and dry-run messages arrive.
- [ ] Set `LIVE_TRADING_ENABLED=true` and redeploy.
- [ ] Create a fresh backup and record the checksum.
- [ ] Confirm `OrderExecutor` performs final placement-time validation
      immediately before the real Kraken call (balance, pair metadata, minimum
      volume, fee buffer, HOLD/UNKNOWN state, duplicate idempotency).
- [ ] Wait for the first scheduled buy or trigger **Buy Now** from the dashboard.
- [ ] Confirm the order appears on Kraken and in `data/transactions.json`.
- [ ] Confirm Telegram reports `✅ Buy order placed`.
- [ ] Verify `/health/ready` remains `200`.
- [ ] Do **not** enable unattended operation until at least one scheduled buy has
      completed in live mode.
- [ ] Complete `docs/operations/FIRST_LIVE_TRADE_TEMPLATE.md`.

## 9. Documentation review

- [ ] Operations team has read `docs/operations.md`.
- [ ] Incident response contacts and escalation path are documented locally.
- [ ] `docs/operations/ORDER_FAILURE_RUNBOOK.md` is bookmarked.
- [ ] `docs/operations/BACKUP_RESTORE_RUNBOOK.md` is bookmarked.
- [ ] `docs/operations/PRODUCTION_PREFLIGHT.md` is bookmarked.

## Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Owner | | | |
| Operator | | | |
| Security review | | | |

*Keep this signed checklist in a location accessible to the operations team but
outside the public repository.*
