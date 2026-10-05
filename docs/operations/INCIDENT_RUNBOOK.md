# Incident Runbook

How to respond to common operational incidents for the DCA-Bot.

---

## Severity levels

| Level | Meaning | Examples |
|-------|---------|----------|
| P1 — Critical | Live trading impacted or funds at risk. | Unauthorized activity, repeated duplicate orders, cannot stop bot. |
| P2 — High | Bot unhealthy or missing scheduled buys. | `/health/ready` down, heartbeat stale, Telegram silent. |
| P3 — Medium | Degraded but trades still possible. | Dashboard slow, backups stale, one failed order on hold. |
| P4 — Low | Warning or cleanup needed. | Log volume high, old backups, non-critical alert. |

## Immediate response

1. **Stop the bot** if funds are at risk:
   ```bash
   ssh deploy@<PRODUCTION_HOST>
   sudo <CONTAINER_RUNTIME>-compose -p crypto-agent -f <DEPLOYMENT_PATH>/compose.yaml down
   ```
2. **Preserve state** before making changes:
   ```bash
   sudo cp -r <DEPLOYMENT_PATH>/data <DEPLOYMENT_PATH>/data.incident-$(date +%Y%m%d-%H%M%S)
   ```
3. **Notify** the owner and any on-call operator.

---

## Scenarios

### Bot keeps placing duplicate orders

1. Check `data/order_attempts.json` for multiple attempts with the same `cycle_id`.
2. Pause the bot from the dashboard.
3. Verify `LIVE_TRADING_ENABLED` and `APP_ENV` are correct.
4. Look for concurrent bot processes or duplicate containers.
5. Restart only after the root cause is confirmed.

### Missed scheduled buy

1. Check `/health/ready` and bot logs:
   ```bash
   sudo <CONTAINER_RUNTIME> logs --tail 100 crypto-agent
   ```
2. Verify the bot was running and not paused during the buy window.
3. Check available fiat balance on Kraken.
4. Check `max_price` and `max_monthly_amount` limits.
5. Review `data/order_attempts.json` for failed or held attempts.
6. If the buy was skipped by configuration, no action is needed. If it failed,
   resolve the underlying error and use manual "Buy Now" only after confirming
   the cause.

### `/health/ready` returns 503

1. Read the response body to identify the failing check (`configuration`,
   `storage`, `heartbeat`, `bot_state`).
2. For `configuration` failures: verify `config.json` is readable and valid JSON.
3. For `storage` failures: check volume mounts, permissions (`user: "1500:1500"`),
   and disk space.
4. For `heartbeat` failures: check if the trading loop is stuck, crashed, or
   blocked on a slow Kraken call.
5. For `bot_state` fatal: the bot is stopped or in `error`/`hold`; inspect logs.

### Stale heartbeat

1. Confirm `data/heartbeat.json` timestamp is old.
2. Check if the trading loop is blocked on a network call.
3. Verify CPU/memory limits are not exhausted:
   ```bash
   sudo <CONTAINER_RUNTIME> stats --no-stream crypto-agent
   ```
4. If the loop is hung, restart the container.

### Telegram notifications stopped

1. Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are still valid.
2. Send a test message from the dashboard (`/telegram/test`).
3. Check network connectivity to `api.telegram.org`.
4. Confirm external monitoring is still alerting even if Telegram is down.

### Order on HOLD

See `docs/operations/ORDER_FAILURE_RUNBOOK.md`.

### JSON store lock contention

Symptoms:

- Dashboard or API returns `503 Server configuration error` or `LockAcquisitionError`.
- Logs show `Could not acquire ... lock for <file> within 10.0s`.

Response:

1. Identify which file is contended (`transactions.json`, `order_attempts.json`,
   `state.json`, etc.).
2. Check for multiple bot/dashboard processes or containers accessing the same
   data directory.
3. Verify that no process is hung while holding a lock (high CPU or blocked I/O).
4. If a lock file is stale and no process is running, the OS releases it when
   the holder exits. Manually removing a `.lock` file is safe only when no
   process is using it.
5. Resolve the source of contention before resuming trading.

### Unknown order reconciliation vs. validation rejection

Two distinct failure paths must not be confused:

- **Validation rejection** (`HOLD` with `final_outcome` like `PREFLIGHT_EXPIRED`,
  `INSUFFICIENT_BALANCE`, `BELOW_MINIMUM`, `DUPLICATE_ATTEMPT`): the bot decided
  not to submit the order. The cause is local configuration or state. Fix the
  root cause and acknowledge the attempt; no reconciliation with Kraken is needed.

- **Unknown outcome** (`state == UNKNOWN`): the request was sent to Kraken but
  the response was lost or ambiguous. The bot must reconcile by querying Kraken
  with the stored `attempt_id`/`kraken_ref` before any retry. Do not manually
  retry or place a duplicate buy until reconciliation completes.

See `docs/operations/ORDER_FAILURE_RUNBOOK.md` for the full state machine.

### Backup failure or stale backup

See `docs/operations/BACKUP_RESTORE_RUNBOOK.md`.

### Suspected compromised API key

1. **Stop the bot immediately**.
2. Revoke the Kraken API key.
3. Rotate `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` in Gitea secrets.
4. Review `data/audit_log.json` and container logs for unauthorized actions.
5. Restart only after the new key is deployed.

### Container crash loop

1. Check container logs:
   ```bash
   sudo <CONTAINER_RUNTIME> logs --tail 200 crypto-agent
   ```
2. Look for startup validation errors (missing `SESSION_SECRET`, invalid
   `config.json`, etc.).
3. Fix the configuration or secret, then restart.
4. If the issue started after a deploy, roll back to the previous image.

---

## Rollback

### Roll back the application image

```bash
ssh deploy@<PRODUCTION_HOST>
cd <DEPLOYMENT_PATH>
# Replace <PREVIOUS_SHA> with the last known-good tag
echo '<PREVIOUS_SHA>' > .current-image
echo "IMAGE_NAME=<REGISTRY_HOST>/<IMAGE_PATH>" > .env
echo "IMAGE_TAG=<PREVIOUS_SHA>" >> .env
sudo <CONTAINER_RUNTIME>-compose -p crypto-agent -f compose.yaml up -d --force-recreate
```

### Roll back `config.json`

```bash
sudo <DEPLOYMENT_PATH>/scripts/restore_backup.sh \
  <DEPLOYMENT_PATH>/backups/crypto-agent-data-YYYYMMDD-HHMMSS.tar.gz
```

Stop the container first unless using `--force` in an emergency.

---

## Post-incident

1. Write a short incident note:
   - Time, symptom, impact.
   - Root cause (if known).
   - Actions taken.
   - Follow-up items.
2. Review whether monitoring thresholds need adjustment.
3. Update this runbook if a new scenario is identified.
