# Order Failure Runbook

This document describes how the DCA-Bot handles failed, uncertain, and retried buy orders.

## Scope

Applies to **real** order attempts placed when `LIVE_TRADING_ENABLED=true` and the bot is running in a production environment. Simulated/dry-run buys are recorded directly and do not use this state machine.

## State machine

Every live buy is wrapped in an `OrderAttempt` that moves through the following states:

| State | Meaning |
|-------|---------|
| `CREATED` | Attempt record created, not yet submitted. |
| `SUBMITTING` | The request is being sent to Kraken. |
| `SUBMITTED` | Kraken accepted the order and returned a transaction ID. |
| `CONFIRMED` | The order is treated as filled and recorded in `transactions.json`. |
| `FAILED_TRANSIENT` | A recoverable error occurred (network, HTTP 5xx, rate limit). |
| `RETRY_SCHEDULED` | A retry has been scheduled at a future time. |
| `FAILED_PERMANENT` | A non-recoverable error occurred (insufficient funds, invalid volume, etc.). |
| `UNKNOWN` | The request may or may not have reached Kraken; outcome must be reconciled. |
| `HOLD` | Automatic processing is stopped; operator action is required. |
| `CANCELLED` | The attempt was cancelled by an operator. |

## Error classification

Errors are classified automatically:

- **Transient**: DNS failure, connection refused, HTTP 429/502/503/504, temporary Kraken service errors.
- **Permanent**: invalid pair/volume/price, insufficient funds, invalid credentials, permission denied.
- **Unknown outcome**: timeout, connection reset, broken pipe, or any error that may have been sent to Kraken before the response was lost.

Unknown outcomes are never blindly retried. The bot reconciles against Kraken with the deterministic `userref` before taking further action.

## Idempotency

Each logical cycle produces a deterministic `userref` from:

- trading pair
- strategy
- cycle time
- attempt number
- application version

The bot checks `QueryOrders` by `userref` to determine whether an unknown order was accepted. Duplicate attempts for the same logical cycle are suppressed.

## Retry policy

Configurable environment variables (with defaults):

```text
ORDER_RETRY_MAX_ATTEMPTS=4
ORDER_RETRY_BASE_SECONDS=30
ORDER_RETRY_MULTIPLIER=4
ORDER_RETRY_MAX_JITTER_SECONDS=10
ORDER_RECONCILE_WINDOW_SECONDS=300
ORDER_UNKNOWN_HOLD_SECONDS=1800
ORDER_RECONCILE_INTERVAL_SECONDS=30
```

Default schedule:

- attempt 1: immediately
- attempt 2: ~30 seconds
- attempt 3: ~2 minutes
- attempt 4: ~10 minutes

Retries use exponential backoff with jitter. After the maximum number of attempts the order moves to `HOLD`.

## Reconciliation

When an attempt is in `UNKNOWN`, the bot periodically calls `QueryOrders` with the `userref`:

- If Kraken returns the order as `closed`/`filled`/`executed`, the attempt is confirmed.
- If the status is `open`/`pending`, the attempt returns to `SUBMITTED`.
- If the status is cancelled or expired, it is treated as `FAILED_PERMANENT`.
- If the order is not found, the bot waits for `ORDER_RECONCILE_WINDOW_SECONDS`. If still not found after that window, the attempt is treated as rejected (`FAILED_PERMANENT`).

## Hold state and operator actions

An attempt enters `HOLD` when:

- the error is permanent
- retries are exhausted
- an unknown outcome cannot be resolved within the reconcile window
- a repeated authentication error occurs
- the live-order safety guard blocks a non-production order

When an attempt is on `HOLD`:

- no automatic retry of the same intended order occurs
- the dashboard shows the held attempt
- a Telegram alert and an internal alert are raised
- operator action is required

Dashboard actions (all require authentication and CSRF tokens):

- `POST /api/orders/{id}/acknowledge` — mark the hold as acknowledged.
- `POST /api/orders/{id}/resolve` with body `{"action": "confirm"}` — manually confirm the order and record a transaction.
- `POST /api/orders/{id}/resolve` with body `{"action": "retry"}` — reset the attempt and schedule a new retry cycle.
- `POST /api/orders/{id}/resolve` with body `{"action": "cancel"}` — cancel the attempt.

Future unrelated buy cycles continue according to normal scheduling unless the bot itself has been paused.

## Observability

- Attempts are persisted in `data/order_attempts.json`.
- `HOLD` is surfaced in `/health/ready` as a fatal/readiness state.
- Telegram notifications are sent for: initial failure, retry scheduled, retry success, permanent failure, unknown outcome, hold, and operator resolution.
- Notifier failure does not hide the underlying order state.

## Manual recovery checklist

1. Check the dashboard **Orders** page or `data/order_attempts.json` for held attempts.
2. Verify on Kraken whether the order was actually placed (using the pair, amount, and approximate time).
3. If the order is present on Kraken, use `resolve` → `confirm`.
4. If the order is missing and you are sure it was rejected, use `resolve` → `retry` or `cancel`.
5. Resolve the root cause (add funds, fix credentials, wait for Kraken outage to end).
6. Acknowledge the hold once the situation is understood.
7. Monitor the next scheduled cycle for normal behavior.

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Repeated `UNKNOWN` attempts | Network instability between bot and Kraken | Check host connectivity; increase `ORDER_RECONCILE_INTERVAL_SECONDS` if needed. |
| `HOLD: insufficient funds` | Account lacks fiat for the configured buy | Deposit funds, then retry or wait for next cycle. |
| `HOLD: invalid credentials` | API key revoked or permissions changed | Update `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`, restart bot. |
| `HOLD: max retry attempts exhausted` | Kraken returned repeated transient errors | Check Kraken status page; resolve then confirm/cancel manually. |
| `HOLD: live order blocked outside production` | `APP_ENV` is not `production` while live trading is enabled | Only run live trading with `APP_ENV=production`. |
