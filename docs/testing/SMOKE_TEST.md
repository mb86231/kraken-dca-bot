# Smoke Test

The smoke test performs a safe end-to-end validation of a running dashboard.
It refuses to run against production unless explicitly allowed.

## Requirements

- Python 3.11+
- Project dependencies installed (`requirements.txt`)
- A running dashboard in `demo` or `staging` mode

## Usage

```bash
python scripts/smoke_test.py \
  --url http://localhost:8000 \
  --username admin \
  --password <password> \
  --env demo
```

### Optional Arguments

| Argument | Description |
|----------|-------------|
| `--allow-production` | Skip the production environment block. Use with extreme care. |
| `--timeout 30` | Request timeout in seconds (default 30). |
| `--output report.json` | Write the machine-readable report to a file. |

## Environment Safety

- `--env demo` and `--env staging` are allowed by default.
- `--env production` exits immediately unless `--allow-production` is also passed.
- The smoke test never calls the live Kraken order endpoint.
- Manual cycle in demo/staging mode executes a simulated purchase.

## Steps

1. Verify target environment.
2. Call `/health/live` and `/health/ready`.
3. Log in and capture session + CSRF cookies.
4. Load `/dashboard`.
5. Call `/api/operations/status`.
6. Pause the bot.
7. Verify paused state via `/api/status`.
8. Resume the bot.
9. Trigger a manual cycle via `/api/bot/cycle`.
10. Verify a transaction appears in `/api/transactions`.
11. Create a backup via `/api/backups/create`.
12. Verify security headers on `/login`.
13. Log out.

## Output

Console output shows each step as `PASS` or `FAIL`. A machine-readable JSON
report is also printed (or written to `--output`). The script exits with a
non-zero status if any step fails.

## CI Integration

The staging workflow runs the smoke test after a successful staging deployment
when `SMOKETEST_BASE_URL` is configured. Required secrets (by name only):

- `SMOKETEST_USERNAME`
- `SMOKETEST_PASSWORD`

Required variables:

- `SMOKETEST_BASE_URL`

The workflow skips smoke testing if `SMOKETEST_BASE_URL` is unset.
