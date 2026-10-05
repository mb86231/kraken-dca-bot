# DCA-Bot Quality Gates

This document defines the static-analysis and test quality gates for the DCA-Bot project.
All gates must pass before code is merged and before any deployment is built.

## Supported Python versions

- **Python 3.11** and **Python 3.12** are supported.
- The CI container image is built on Python 3.12.
- Local development may use either version, but the project must remain compatible with 3.11 because `pyproject.toml` declares `requires-python = ">=3.11,<3.13"`.

## Local quality commands

Run the exact same commands that CI runs. From the repository root:

### Linux / macOS

```bash
python3 -m pytest tests/ -v
python3 -m ruff check .
python3 -m mypy .
python3 -m detect_secrets scan --baseline .secrets.baseline --all-files
```

### Windows (PowerShell / Git Bash)

```powershell
.venv\Scripts\python -m pytest tests/ -v
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy .
.venv\Scripts\python -m detect_secrets scan --baseline .secrets.baseline --all-files
```

A convenience wrapper is also available:

```bash
# Linux / macOS
bash scripts/ci-test.sh
```

## What each gate checks

| Gate | Purpose | Must pass |
|------|---------|-----------|
| `pytest tests/ -q` | Unit and integration tests | yes |
| `ruff check .` | Linting and import checks | yes |
| `mypy .` | Static type checking | yes |
| `detect-secrets scan --baseline .secrets.baseline --all-files` | Detect committed secrets | yes |
| `python scripts/check_docs.py` | Validate relative Markdown links | yes |

## Reproducing failures

1. **Ensure the virtual environment is active** and contains dependencies from `requirements.txt` and `requirements-dev.txt`.
2. **Run the failing command directly** using the exact invocation above. Do not rely on editor plugins, which may use different settings.
3. **Fix the root cause** rather than suppressing the symptom.
4. **Re-run all gates** before committing; fixing one gate can expose another.

## CI quality gates

The Gitea Actions workflows enforce the same commands inside the built container:

- `.gitea/workflows/deploy.yml` (production)
- `.gitea/workflows/deploy-staging.yml` (staging)
- `scripts/ci-test.sh` (reusable CI script)

Each workflow runs:

```bash
python3 scripts/check_docs.py
python3 -m ruff check .
python3 -m mypy .
python3 -m pytest tests/ -v --cov=bot --cov=web --cov-report=term --cov-fail-under=75
python3 -m detect_secrets scan --baseline .secrets.baseline --all-files
```

If any command fails, the job exits non-zero and the deployment is blocked. The `set -eu` flag in the CI shell blocks ensure failures propagate.

## Rules for `# type: ignore` usage

Narrow, justified type ignores are permitted only when **all** of the following are true:

1. There is no safe type-correct alternative.
2. The exact error code is specified: `# type: ignore[<error-code>]`.
3. A comment explains why the ignore is necessary.
4. A test protects the runtime behavior being ignored.

Example of an acceptable ignore:

```python
# slowapi's handler is runtime-compatible but not fully typed for Starlette's
# exception-handler protocol; covered by web dashboard tests.
app.add_exception_handler(
    429,
    cast(Callable[[StarletteRequest, Exception], Awaitable[StarletteResponse]], _rate_limit_exceeded_handler),
)
```

Broad ignores such as `# type: ignore` without an error code are not allowed. If a missing third-party stub is the only issue, add a module override in `pyproject.toml` instead:

```toml
[[tool.mypy.overrides]]
module = "jose.*"
ignore_missing_imports = true
```

## Adding tests

- Place new tests in `tests/` using the existing `test_*.py` naming convention.
- Use the fixtures in `tests/conftest.py` for common setup.
- For scheduling/date tests, use the `_make_bot` helper in `tests/test_cycle.py` to construct a bot with a fake API.
- Prefer `monkeypatch` over global state mutation.
- Run the new test in isolation first, then run the full suite:

```bash
python3 -m pytest tests/test_cycle.py::test_lump_sum_valid_future_end_date -v
python3 -m pytest tests/ -v
```

## Current status

- `pytest tests/ -q`: **259 passed, 5 warnings**
- `ruff check .`: **0 findings**
- `mypy .`: **0 errors**
- `detect-secrets`: **pass**
- `python scripts/check_docs.py`: **0 broken links**
