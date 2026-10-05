# Health Checks and Container Hardening

This document describes the DCA-Bot health-check model, the structured heartbeat, the container-hardening settings, and how to operate them.

## Overview

The bot exposes two unauthenticated HTTP health endpoints for container and orchestrator probes:

- `GET /health/live` — liveness
- `GET /health/ready` — readiness

Both are implemented in `web/app.py`. They intentionally require **no authentication** so that Docker/Podman, Kubernetes, and external uptime monitors can use them. The responses contain only non-sensitive runtime metadata.

## Endpoints

### `/health/live`

Returns `200 OK` as long as the application process and event loop respond.

Example response:

```json
{
  "status": "alive",
  "app": "ok"
}
```

This endpoint does **not** depend on Kraken, storage, or the trading loop. It answers the question: "Is the dashboard process running?"

### `/health/ready`

Returns `200 OK` only when the bot is fully configured and the trading loop has recently reported in. A `503 Service Unavailable` is returned with a machine-readable body when any required condition is not met.

Example ready response:

```json
{
  "status": "ready",
  "app": "ok",
  "trading_loop": "ok",
  "heartbeat_age_seconds": 12,
  "storage": "ok",
  "configuration": "ok",
  "checks": {
    "configuration": "ok",
    "storage": "ok",
    "heartbeat": "ok",
    "heartbeat_age_seconds": 12,
    "bot_state": "waiting"
  }
}
```

Readiness checks:

1. **Configuration** — `app.state.config` is loaded and `config.json` is readable.
2. **Storage** — the transaction store is available and its data directory is writable.
3. **Heartbeat** — `data/heartbeat.json` exists, is valid JSON, and its `timestamp` is no older than `HEARTBEAT_MAX_AGE_SECONDS` (default `900`).
4. **Bot state** — the bot is not in a fatal state (`stopped` or `error`). `paused` is treated as a valid, ready state.

Kraken reachability is **not** a readiness gate. A temporary exchange outage should not restart the container.

## Heartbeat File

The trading loop writes `data/heartbeat.json` atomically at startup and after every successful poll cycle. The file contains:

```json
{
  "timestamp": "2026-07-25T14:35:00+00:00",
  "status": "waiting",
  "mode": "recurring",
  "paused": false,
  "last_cycle_at": null,
  "next_cycle_at": "2026-07-25T18:12:00+00:00",
  "last_price_at": "2026-07-25T14:35:00+00:00",
  "last_order_at": null,
  "runtime_started_at": "2026-07-25T14:30:00+00:00",
  "version": "1.1.0"
}
```

The heartbeat deliberately omits:

- API keys and secrets
- Session cookies or tokens
- Exact account balances
- Telegram tokens

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HEARTBEAT_FILE` | `data/heartbeat.json` | Path to the heartbeat JSON file. |
| `HEARTBEAT_MAX_AGE_SECONDS` | `900` | Maximum acceptable age of the heartbeat for readiness. |

## Container Health Check Script

`scripts/healthcheck.sh` uses `curl` to probe both endpoints. It fails if either endpoint is unreachable or returns a non-2xx status.

```bash
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
```

The port is controlled by `WEB_UI_PORT` (default `8000`). `WEB_UI_ENABLED` must be `true` for the health endpoints to be available.

## Container Hardening

Both `compose.yaml` and `compose.staging.yaml` apply the following hardening:

| Setting | Value | Purpose |
|---------|-------|---------|
| `read_only: true` | — | Root filesystem is read-only. |
| `tmpfs: /tmp` | — | Temporary directory in memory, discarded on stop. |
| `cap_drop: ALL` | — | Drop all Linux capabilities. |
| `no-new-privileges:true` | — | Prevent privilege escalation. |
| `pids_limit: 100` | — | Limit processes/threads inside the container. |
| `deploy.resources.limits.memory` | `512M` | Hard memory ceiling. |
| `deploy.resources.limits.cpus` | `1.0` | CPU ceiling. |
| `deploy.resources.reservations.memory` | `128M` | Soft memory reservation. |
| `stop_signal: SIGTERM` | — | Request graceful shutdown. |
| `stop_grace_period: 60s` | — | Time allowed for graceful shutdown. |
| `logging` | `json-file`, `max-size: 10m`, `max-file: 5` | Bounded container logs. |

The Dockerfile installs `curl` for the health-check script and no longer installs `procps`.

## Graceful Shutdown

`main.py` registers a `SIGTERM` handler that calls `app.request_stop()`. This sets the internal stop event and wakes the trading loop, allowing it to exit cleanly instead of being killed when the container stops.

The systemd units include `TimeoutStopSec=60` to match the Compose `stop_grace_period`.

## Failure Scenarios

| Symptom | Likely cause | Response |
|---------|--------------|----------|
| `/health/live` fails | Dashboard not running or crash loop. | Inspect container logs and restart. |
| `/health/ready` heartbeat missing | Bot has not finished first poll cycle or startup failed. | Wait for `start_period`; check logs. |
| Heartbeat stale | Trading loop blocked, API hung, or high load. | Check logs; restart if loop does not recover. |
| Storage not writable | Volume mount missing, wrong permissions, or read-only root. | Verify host paths and `user: "1500:1500"`. |
| Bot state fatal (`stopped`/`error`) | Stop requested or unrecoverable error. | Investigate logs; restart manually. |

## Migration Notes

The previous health check wrote `/tmp/.health` and used `pgrep` to find the Python process. That has been replaced with:

- `data/heartbeat.json` (structured, persisted in the data directory)
- HTTP `/health/live` and `/health/ready` endpoints
- `curl`-based `scripts/healthcheck.sh`

No manual migration is required; the new heartbeat file is created automatically on the next poll cycle.
