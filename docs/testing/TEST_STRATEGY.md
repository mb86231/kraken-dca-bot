# Test Strategy

This document describes how the DCA-Bot project is tested and how to maintain
the test suite.

## Test Levels

### Unit Tests

- Located in `tests/`.
- Run with pytest.
- Cover individual modules and functions with mocked external dependencies.
- Kraken API calls are mocked or use demo-mode code paths.

### Dashboard API Tests

- Located in `tests/test_web.py`, `tests/test_api_coverage.py`, and related files.
- Use FastAPI's `TestClient`.
- Exercise authenticated routes, CSRF protection, rate limiting, and brute-force
  protection.

### Security Tests

- Located in `tests/test_security.py`.
- Cover rate limits, brute-force lockout, security headers, CSP, and local
  Chart.js vendoring.

### Monitoring Tests

- Located in `tests/test_monitoring.py`.
- Cover `/api/operations/status`, `/api/metrics`, and health endpoints.

### Smoke Tests

- Implemented in `scripts/smoke_test.py`.
- Run against a live demo or staging deployment.
- Validate end-to-end login, bot controls, transactions, backups, and security
  headers.

## Running Tests

### Local (Windows)

```powershell
PYTHONPATH=. .venv/Scripts/pytest tests/ -q
```

### Local (Linux/macOS)

```bash
PYTHONPATH=. .venv/bin/pytest tests/ -q
```

### With Coverage

```bash
PYTHONPATH=. .venv/bin/pytest tests/ -q --cov=bot --cov=web --cov-report=term --cov-report=xml
```

## Adding Tests

1. Place new tests in `tests/`. Name files `test_<module>.py`.
2. Use the shared fixtures in `tests/conftest.py`.
3. For dashboard tests, use the `auth_client` pattern from
   `tests/test_api_coverage.py`.
4. Mock external APIs; never call the live Kraken order endpoint from automated
   tests.
5. Run the full quality gate before committing:

   ```bash
   ruff check .
   mypy .
   pytest tests/ -q --cov=bot --cov=web --cov-fail-under=75
   detect-secrets scan --baseline .secrets.baseline --all-files
   ```

## Test Data

- Tests create temporary directories via the `temp_dir` fixture.
- Never commit real API keys, passwords, or session secrets in tests.
- Use `demo-key` / `demo-secret` placeholders for Kraken credentials in tests.

## Continuous Integration

The staging workflow (`.gitea/workflows/deploy-staging.yml`) runs:

1. `ruff check .`
2. `mypy .`
3. `pytest tests/ -v --cov=bot --cov=web --cov-report=term --cov-fail-under=75`
4. `detect-secrets scan --baseline .secrets.baseline --all-files`

If any step fails, the image is not pushed and staging is not deployed.

## Known Gaps

The following areas still need better test coverage:

- `bot/core.py` trading-loop orchestration (currently 46%).
- `bot/api_client.py` retry and transient-error branches.
- `bot/alerts.py` alert escalation paths.
- `bot/notifier.py` Telegram send failures.
- `web/routers/api.py` order-attempt resolution UI paths.
