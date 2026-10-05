#!/usr/bin/env bash
set -eu

# CI test script intended to run inside a Python 3.12 container.

apt-get update >/dev/null 2>&1
apt-get install -y gcc libffi-dev >/dev/null 2>&1

python3 -m pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

# Validate repository contents.
test -f main.py
test -f config.json
test -f Dockerfile
test -f compose.yaml
test -f requirements.txt
test -f systemd/crypto-agent.service
test -f scripts/entrypoint.sh
test -f scripts/healthcheck.sh

# Verify config.json does not contain credentials.
if grep -qiE '"api_key"|"api_secret"' config.json; then
  echo "ERROR: config.json must not contain api_key or api_secret"
  exit 1
fi

# Basic Python syntax check.
python3 -m py_compile main.py

# Validate Markdown links.
python3 scripts/check_docs.py

# Static analysis.
python3 -m ruff check .
python3 -m mypy .

# Run pytest with a generated test password hash.
WEB_UI_PASSWORD_HASH=$(python3 -c "import bcrypt; print(bcrypt.hashpw('test'.encode(), bcrypt.gensalt()).decode())")
WEB_UI_PASSWORD_HASH="$WEB_UI_PASSWORD_HASH" \
SESSION_SECRET="test-secret-32-bytes-long-value" \
KRAKEN_API_KEY="demo-key" \
KRAKEN_API_SECRET="demo-secret" \
DEMO_MODE="true" \
RATE_LIMIT_ENABLED="true" \
python3 -m pytest tests/ -v

# Scan for secrets.
python3 -m detect_secrets scan --baseline .secrets.baseline --all-files
