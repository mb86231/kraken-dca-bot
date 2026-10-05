# Monitoring and Alerting

This document explains how to monitor the DCA-Bot with external tooling so failures are detected even when the bot's own Telegram notifier is unavailable.

## Endpoints

The dashboard exposes two monitoring endpoints.

### `/api/operations/status` — authenticated operational summary

Returns a JSON document with version, uptime, heartbeat, schedule, orders, backup, and notifier status. Requires a valid dashboard session cookie; use it with Uptime Kuma's JSON-query monitor or a custom script that logs in.

Example response:

```json
{
  "version": "1.1.0",
  "environment": "production",
  "demo_mode": false,
  "live_trading_enabled": true,
  "uptime_seconds": 3661,
  "heartbeat": {
    "timestamp": "2026-07-25T20:00:00+00:00",
    "age_seconds": 12,
    "status": "waiting",
    "paused": false
  },
  "schedule": {
    "last_cycle_at": "2026-07-24T08:00:00+00:00",
    "next_cycle_at": "2026-08-24T08:00:00+00:00"
  },
  "orders": {
    "last_successful_order_at": "2026-07-24T08:00:05+00:00",
    "hold_active": false,
    "hold_count": 0,
    "success_count": 12,
    "failure_count": 0,
    "retry_count": 0,
    "unknown_count": 0
  },
  "backup": {
    "last_successful_at": "2026-07-25T02:30:00+00:00",
    "age_seconds": 63000,
    "validation_status": "ok"
  },
  "notifier": {
    "enabled": true
  }
}
```

### `/api/metrics` — Prometheus-compatible metrics

Returns Prometheus exposition format. Protected by a dedicated `MONITORING_TOKEN` passed as a Bearer token.

```bash
curl -H "Authorization: Bearer $MONITORING_TOKEN" https://bot.example.com/api/metrics
```

Example output:

```text
# HELP dca_bot_uptime_seconds Time since the bot started
# TYPE dca_bot_uptime_seconds gauge
dca_bot_uptime_seconds 3661
# HELP dca_bot_heartbeat_age_seconds Age of the latest trading-loop heartbeat
# TYPE dca_bot_heartbeat_age_seconds gauge
dca_bot_heartbeat_age_seconds 12
# HELP dca_bot_backup_age_seconds Age of the last successful backup
# TYPE dca_bot_backup_age_seconds gauge
dca_bot_backup_age_seconds 63000
# HELP dca_bot_order_success_total Total confirmed order attempts
# TYPE dca_bot_order_success_total counter
dca_bot_order_success_total 12
# HELP dca_bot_order_failure_total Total failed order attempts
# TYPE dca_bot_order_failure_total counter
dca_bot_order_failure_total 0
# HELP dca_bot_order_retry_total Total order retry attempts
# TYPE dca_bot_order_retry_total counter
dca_bot_order_retry_total 0
# HELP dca_bot_order_hold_count Current number of held order attempts
# TYPE dca_bot_order_hold_count gauge
dca_bot_order_hold_count 0
# HELP dca_bot_order_unknown_count Current number of order attempts with unknown outcome
# TYPE dca_bot_order_unknown_count gauge
dca_bot_order_unknown_count 0
# HELP dca_bot_rate_limit_rejection_total Total order attempts rejected or delayed by rate limiting
# TYPE dca_bot_rate_limit_rejection_total counter
dca_bot_rate_limit_rejection_total 0
# HELP dca_bot_notifier_enabled Whether Telegram notifier is configured
# TYPE dca_bot_notifier_enabled gauge
dca_bot_notifier_enabled 1
```

## Configuration

Set a monitoring token in the environment:

```bash
# Generate a token
python -c "import secrets; print(secrets.token_hex(32))"

# Add to .env
MONITORING_TOKEN=your_token_here
```

Restart the container after changing environment variables.

## Uptime Kuma setup

### 1. Liveness check

- **Monitor type:** HTTP(s)
- **URL:** `https://bot.example.com/health/live`
- **Expected status code:** 200
- **Heartbeat interval:** 60 seconds
- **Retries:** 3

### 2. Readiness check

- **Monitor type:** HTTP(s)
- **URL:** `https://bot.example.com/health/ready`
- **Expected status code:** 200
- **Heartbeat interval:** 60 seconds
- **Retries:** 3

A non-200 response means the bot is not ready (stale heartbeat, unwritable storage, fatal state, etc.).

### 3. Heartbeat age via operations status

- **Monitor type:** HTTP(s) — JSON query
- **URL:** `https://bot.example.com/api/operations/status`
- **Authentication:** Use a valid dashboard session cookie, or call via a small wrapper script.
- **JSON path:** `heartbeat.age_seconds`
- **Max value:** 900 (or your `HEARTBEAT_MAX_AGE_SECONDS` value)

### 4. Backup age

- **Monitor type:** HTTP(s) — JSON query
- **URL:** `https://bot.example.com/api/operations/status`
- **JSON path:** `backup.age_seconds`
- **Max value:** 108000 (30 hours)

### 5. HOLD state

- **Monitor type:** HTTP(s) — JSON query
- **URL:** `https://bot.example.com/api/operations/status`
- **JSON path:** `orders.hold_active`
- **Expected value:** `false`

### 6. Prometheus scrape (if using Prometheus)

Add a job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "dca-bot"
    static_configs:
      - targets: ["bot.example.com:443"]
    scheme: https
    authorization:
      type: Bearer
      credentials: "your-monitoring-token"
    metrics_path: /api/metrics
    scrape_interval: 60s
```

## Alert thresholds

| Signal | Warning | Critical | Action |
|---|---|---|---|
| `/health/live` down | — | > 3 minutes | Bot process or host is down. Check container/systemd. |
| `/health/ready` down | — | > 5 minutes | Bot cannot trade. Check logs, heartbeat, storage. |
| `heartbeat.age_seconds` | > 50% of threshold | > threshold | Trading loop stalled. Check bot logs. |
| `backup.age_seconds` | > 26 hours | > 30 hours | Backup failed or missing. Check backup timer/logs. |
| `orders.hold_active` true | immediate | immediate | Order on hold. Check dashboard / `data/order_attempts.json`. |
| `orders.unknown_count` > 0 | > 15 minutes | > 1 hour | Reconciliation delayed. Verify Kraken connectivity. |
| `orders.failure_count` increases | > 1 per hour | > 3 per hour | Repeated order failures. Check credentials/funds/Kraken status. |
| Disk usage | > 80% | > 90% | Add disk or reduce log/backup retention. |

## Dashboard suggestions

- Uptime Kuma dashboard with the checks above.
- Optional Grafana dashboard for Prometheus metrics.
- Alertmanager route for critical alerts (email, PagerDuty, Slack).

## Security model

- `/api/operations/status` requires dashboard authentication.
- `/api/metrics` requires the `MONITORING_TOKEN` Bearer token.
- No API keys, balances, session cookies, or stack traces are exposed.
- Keep `MONITORING_TOKEN` in environment variables / container secrets only.
- Do not add the monitoring token to URLs or logs.

## Incident response

When an alert fires:

1. Check `/health/ready` for the exact failing component.
2. Inspect `logs/app.json` and `data/order_attempts.json`.
3. Follow the relevant runbook:
   - `docs/operations/ORDER_FAILURE_RUNBOOK.md`
   - `docs/operations/BACKUP_RESTORE_RUNBOOK.md`
   - `docs/operations/HEALTHCHECKS.md`
4. If a live order is stuck, check Kraken directly and use dashboard operator actions.

## Verification procedure

1. Generate and set `MONITORING_TOKEN`.
2. Restart the bot.
3. Run:
   ```bash
   curl -f https://bot.example.com/health/live
   curl -f https://bot.example.com/health/ready
   curl -H "Authorization: Bearer $MONITORING_TOKEN" https://bot.example.com/api/metrics
   ```
4. Log in to the dashboard and verify `/api/operations/status` returns 200.
5. Confirm no secrets appear in any response body.

## Test alarm procedure

1. Stop the bot container or set `HEARTBEAT_MAX_AGE_SECONDS=1` and wait.
2. Confirm Uptime Kuma marks `/health/ready` as down.
3. Restart the bot and confirm recovery.
4. Create a fake `HOLD` state in `data/order_attempts.json` (development only) and verify the alert clears after removing it.
