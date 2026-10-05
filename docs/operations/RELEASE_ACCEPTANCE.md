# Release Acceptance

Checklist performed before accepting a release into production.

---

## 1. Code review and quality gates

- [ ] Branch has been reviewed and approved.
- [ ] All CI quality gates pass:
  - `pytest tests/ -q`
  - `ruff check .`
  - `mypy .`
  - `detect-secrets scan --baseline .secrets.baseline --all-files`
  - `python scripts/check_docs.py`
- [ ] No functional code changes are hidden in documentation-only commits.
- [ ] No secrets or real credentials are committed.

## 2. Staging acceptance

- [ ] Code is deployed to the staging instance (`staging` branch).
- [ ] Staging dashboard is healthy (`/health/live` and `/health/ready` return 200).
- [ ] Staging logs show demo mode and no real exchange calls.
- [ ] Manual "Buy Now" in staging creates a simulated transaction.
- [ ] Backup creation and listing work from the staging dashboard.
- [ ] Telegram test notification succeeds (if configured for staging).
- [ ] Security headers and CSP are present on responses.
- [ ] Rate limiting and brute-force protection behave as expected.

## 3. Documentation review

- [ ] `docs/roadmap.md` reflects the scope of the release.
- [ ] `docs/RELEASE.md` procedure is still accurate.
- [ ] Any new environment variables are documented in `docs/configuration.md`.
- [ ] Any new secrets are documented in `docs/secrets.md`.
- [ ] ADRs are created or updated for architectural decisions.

## 4. Production readiness

- [ ] `main` branch contains the exact code to release.
- [ ] A rollback tag/image is known from the previous production deployment.
- [ ] The production deploy workflow is manual-only and ready to run.
- [ ] Gitea Actions secrets and variables are current.
- [ ] The `deploy` user can reach `<PRODUCTION_HOST>` via SSH.

## 5. Deploy and verify

- [ ] Production deploy workflow runs without errors.
- [ ] Container starts and `/health/live` returns 200.
- [ ] `/health/ready` returns 200 after the first poll cycle.
- [ ] Telegram startup message received.
- [ ] Dashboard login works.
- [ ] If live trading is enabled, the first cycle is monitored manually.
- [ ] Rollback image is recorded in `<DEPLOYMENT_PATH>/.current-image`.

## 6. Rollback criteria

Roll back immediately if any of the following occur:

- Container fails to become healthy within the timeout.
- `/health/ready` remains non-200 after two poll intervals.
- Unexpected real order is placed.
- Dashboard cannot authenticate or shows security warnings.
- Logs contain unredacted secrets or errors.

## Sign-off

| Role | Name | Date | Approved |
|------|------|------|----------|
| Developer | | | |
| QA / Operator | | | |
| Owner | | | |

*Keep this checklist with the release notes in a location accessible to the
operations team but outside the public repository.*
