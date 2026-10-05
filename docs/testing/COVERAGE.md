# Coverage

Code coverage is measured with `pytest-cov`. Coverage reports help identify
untested critical paths without becoming a vanity metric.

## Configuration

Coverage is configured in `pyproject.toml`:

```toml
[tool.coverage.run]
source = ["bot", "web"]
branch = true
omit = [
    "*/tests/*",
    "*/test_*",
    "scripts/*",
]

[tool.coverage.report]
show_missing = true
skip_covered = false
fail_under = 75
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "raise AssertionError",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
    "if TYPE_CHECKING:",
]
```

## Running Coverage Locally

### Windows

```powershell
PYTHONPATH=. .venv/Scripts/pytest tests/ -q --cov=bot --cov=web --cov-report=term --cov-report=xml
```

### Linux/macOS

```bash
PYTHONPATH=. .venv/bin/pytest tests/ -q --cov=bot --cov=web --cov-report=term --cov-report=xml
```

## Current Baseline

As of this branch, the overall coverage target is **75%**. The most recent
measurement is **75.17%**.

Coverage is uneven across modules:

| Module | Coverage | Notes |
|--------|----------|-------|
| `bot/core.py` | ~46% | Trading-loop orchestration needs more tests. |
| `bot/api_client.py` | 80% | Retry and transient-error paths need tests. |
| `bot/alerts.py` | 45% | Escalation paths are mostly untested. |
| `bot/notifier.py` | 74% | Telegram failure paths need tests. |
| `web/routers/api.py` | 67% | Order-attempt resolution UI paths need tests. |

## Threshold Policy

- The global threshold is intentionally conservative (75%) because the trading
  loop contains long-running async paths that are hard to unit test without
  extensive mocking.
- Critical security modules (`web/security.py`, `web/brute_force.py`) should
  stay above 95%.
- Coverage should be increased incrementally as new tests are added.
- Do not lower the threshold to hide missing tests.

## CI Gate

The staging workflow runs:

```bash
pytest tests/ -v --cov=bot --cov=web --cov-report=term --cov-fail-under=75
```

A pull request that drops coverage below 75% will fail CI.
