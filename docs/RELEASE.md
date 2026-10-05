# Release Workflow

How code moves between environments. **Production is never updated automatically —
every production release requires explicit, manual approval.**

> Deployment-specific values are written as placeholders (e.g., `<PRODUCTION_HOST>`,
> `<DEPLOYMENT_PATH>`). Replace them with your own infrastructure values.

## Environments

| | Staging | Production |
|---|---|---|
| Git branch | `staging` | `main` |
| Deploy trigger | automatic on push to `staging` | manual only: Gitea → Actions → "Production Deploy" → Run workflow |
| Deploy dir (host `<PRODUCTION_HOST>`) | `<STAGING_DEPLOYMENT_PATH>` | `<DEPLOYMENT_PATH>` |
| Image tag | `staging` | `<git-sha>` + `latest` |
| Dashboard | host port <STAGING_DASHBOARD_PORT> → nginx `:<STAGING_NGINX_PORT>` (HTTP) | container host port <DASHBOARD_PORT>, Nginx external HTTPS port `<NGINX_PORT>` |
| Trading | demo only — dummy Kraken credentials, `LIVE_TRADING_ENABLED=false` | real Kraken credentials |
| Rollback | none (redeploy by pushing) | automatic on health-check failure |

## Staging can never trade live (hard guarantees)

- `APP_ENV=staging` / `TRADING_MODE=demo` are pinned both in `compose.staging.yaml`
  (inline env overrides the env file) and in the CI-written `.env`.
- `bot/config.py`: config refuses to load with `APP_ENV=staging` unless `TRADING_MODE=demo`.
- `bot/core.py`: the bot refuses to start outside production unless demo mode is active.
- `bot/order_execution.py`: every order is simulated unless `config.live_trading_enabled` is true and `APP_ENV=production`.
- `bot/api_client.py`: `KrakenAPI.place_market_order` itself raises `LiveOrderBlockedError`
  unless the client was constructed with a production+live `Config` — no caller can bypass this.
- CI hardcodes `LIVE_TRADING_ENABLED=false` and dummy `KRAKEN_API_KEY`/`KRAKEN_API_SECRET`
  for staging; production secrets are never copied.
- `tests/test_environment.py` enforces all of the above in CI.

## Daily development

1. Branch from `staging`: `git checkout -b feature/x staging`
2. Commit, push, merge into `staging` (PR or direct).
3. Pushing to `staging` auto-deploys to the staging instance (~3–5 min).
4. Test on the staging dashboard: `http://<STAGING_HOST>:<STAGING_NGINX_PORT>`

## Releasing to production (explicit approval only)

Production runs whatever is on `main`, deployed by a manually started workflow.
Perform a release only when the owner explicitly approves it.

1. Confirm the version on staging is tested: dashboard healthy, the
   `Startup safety check: ... can_place_live_orders=false` log line present,
   simulated transactions flowing.
2. Merge the tested code into `main` (this alone deploys nothing):

   ```bash
   git checkout main && git pull
   git merge --no-ff staging -m "release: <short description>"
   git push origin main
   ```

3. In Gitea: **Actions → "Production Deploy" → Run workflow** (branch `main`).
   This is the only action that touches production.
4. The pipeline builds `:SHA` + `:latest` images, runs pytest + detect-secrets,
   deploys, health-checks, and **automatically rolls back** to the previous
   image if the health check fails.
5. Verify afterwards: dashboard on `https://<PRODUCTION_HOST>:<NGINX_PORT>`, startup log line
   shows the expected `can_place_live_orders` value, next scheduled buy time present.

### Rollback

Automatic on health-check failure. Manual: `IMAGE_TAG=<previous-sha> <DEPLOYMENT_PATH>/scripts/rollback.sh`
on the host (see `docs/operations.md`).

## Rebuilding / resetting staging

Staging is disposable; production's `<DEPLOYMENT_PATH>` is never touched by these steps.

- Data-only reset: run `scripts/staging-reset.sh` on the host (typed `staging` confirmation).
- Full rebuild: stop/disable `crypto-agent-staging.service`, `<CONTAINER_RUNTIME>-compose ... down`,
  remove `<STAGING_DEPLOYMENT_PATH>`, then push to `staging` — CI recreates the image,
  demo `.env`, directories, and systemd unit from scratch.

## Related documents

- [`docs/operations/RELEASE_ACCEPTANCE.md`](operations/RELEASE_ACCEPTANCE.md) — release acceptance checklist.
- [`docs/operations/GO_LIVE_CHECKLIST.md`](operations/GO_LIVE_CHECKLIST.md) — production go-live checklist.
- [`docs/operations.md`](../docs/operations.md) — full operations guide.
