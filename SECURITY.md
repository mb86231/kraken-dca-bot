# Security Policy

## Security Features

This application has been designed with security as a primary concern and is ready for penetration testing.

### Architecture Security

1. **Minimal Attack Surface**
   - Core bot uses only the Python standard library
   - Dashboard uses pinned, well-known dependencies
   - No SQL database (eliminates SQL injection)
   - No shell command execution (eliminates command injection)
   - Direct HTTPS API communication only

2. **Input Validation**
   - All configuration values validated on startup
   - Trading pair format validation
   - Numeric range validation (e.g., `deposit_day`: 1-28)
   - Type checking on all inputs

3. **Secure API Communication**
   - HTTPS only (no HTTP fallback)
   - Proper HMAC-SHA512 signature generation
   - Nonce-based replay attack prevention
   - API keys never logged or exposed in error messages

4. **Container Security**
   - Runs as non-root user `cryptoagent` (UID 1500)
   - Minimal base image (`python:3.11-slim`)
   - All capabilities dropped (`cap_drop: ALL`)
   - No privilege escalation (`no-new-privileges:true`)
   - Limited file system access

5. **Dashboard Security**
   - Session-based authentication with bcrypt password hashing
   - Signed session cookies (`HttpOnly`, `Secure` in production, `SameSite=Lax`)
   - CSRF tokens on all state-changing requests
   - Per-IP and per-username brute-force lockout
   - Configurable rate limiting on login, API, manual buy, settings, and control endpoints
   - Secrets masked in UI and API responses

6. **Data Protection**
   - API credentials supplied via environment variables only
   - Transaction data in JSON format (no sensitive data exposure)
   - No plaintext password storage
   - No sensitive data in Docker images
   - Logs automatically redact secrets

### Threat Model

#### Protected Against

✅ **SQL Injection**: No database used
✅ **Command Injection**: No shell execution
✅ **Path Traversal**: Validates and restricts file access
✅ **XSS**: Templates escape output
✅ **CSRF**: CSRF tokens on state-changing requests
✅ **API Key Exposure**: Not logged, not in `config.json`
✅ **Privilege Escalation**: Non-root container user
✅ **Replay Attacks**: Nonce-based API signatures
✅ **Man-in-the-Middle**: HTTPS only with certificate verification
✅ **Secret Leakage in Git**: `.gitignore`, pre-commit hooks, and `detect-secrets` scan

#### Risks to Consider

⚠️ **Environment File Access**: If an attacker gains file system access, secrets in `config/.env` are readable
- Mitigation: File is owned by root and mode `600`; host is hardened
- Recommended: Limit SSH access and audit file permissions regularly

⚠️ **Docker Host Compromise**: Container isolation protects against application exploits, but not host-level attacks
- Mitigation: Keep Docker/Podman host updated, use security scanning

⚠️ **API Key Permissions**: Over-permissioned API keys could allow unauthorized actions
- Mitigation: Use minimal permissions (Query Funds + Create Orders only)
- Never enable: Withdraw Funds permission

✅ **Rate Limiting**: Configurable per-endpoint rate limits are enforced on login, API calls, manual buy, settings, and control endpoints. See `docs/configuration.md` and `docs/SECURITY_HARDENING.md` for defaults.
- Notes: `DISABLE_RATE_LIMIT` is allowed only in development/test and is rejected at startup when `APP_ENV=production`.

## Secret Management

### Environment variables

All secrets are provided via environment variables:

| Secret | Source | Usage |
|--------|--------|-------|
| `KRAKEN_API_KEY` | `.env` / Gitea secret | Kraken API public key |
| `KRAKEN_API_SECRET` | `.env` / Gitea secret | Kraken API private key |
| `TELEGRAM_BOT_TOKEN` | `.env` / Gitea secret | Telegram bot token |
| `TELEGRAM_CHAT_ID` | `.env` / Gitea secret | Telegram chat ID |
| `WEB_UI_PASSWORD_HASH` | `.env` / Gitea secret | Bcrypt hash of dashboard password |
| `SESSION_SECRET` | `.env` / Gitea secret | Session cookie signing secret |

Never commit these values. `.env` and `.env.*` are listed in `.gitignore`.

### Pre-commit hooks

Install the hooks to catch accidents before they reach Git:

```bash
pip install pre-commit detect-secrets
pre-commit install
```

The hooks:

1. Run `detect-secrets` against `.secrets.baseline`
2. Reject committed `.env` files
3. Reject `config.json` containing `api_key` or `api_secret`

### CI/CD secret handling

The production pipeline writes secrets to `<DEPLOYMENT_PATH>/config/.env` from Gitea Actions secrets. The file is:

- Created on the CI runner in memory
- Transferred via `scp`
- Installed with mode `600` and owned by root
- Never logged or printed

## Penetration Testing Guidelines

### Recommended Testing Scenarios

1. **Input Validation Testing**
   ```bash
   # Test invalid config values
   - mode: "invalid", null
   - deposit_day: 0, 29, -1, "invalid"
   - crypto_amount: 0, -0.01, "not_a_number"
   - trading_pair: "../../../etc/passwd", "<script>", "'; DROP TABLE--"
   ```

2. **File System Access Testing**
   ```bash
   # Attempt path traversal
   - Modify config paths to ../../../etc/passwd
   - Attempt to write outside /app/data directory
   - Test symbolic link following
   ```

3. **API Security Testing**
   ```bash
   # Test API interaction
   - Invalid API credentials
   - Expired/revoked API keys
   - Malformed API requests
   - Replay attack attempts
   ```

4. **Dashboard Security Testing**
   ```bash
   # Attempt unauthenticated actions
   - POST /api/bot/cycle without session
   - CSRF token bypass
   - Password brute force
   - Session fixation
   ```

5. **Container Escape Testing**
   ```bash
   # Attempt privilege escalation
   podman exec -it crypto-agent /bin/bash
   # Try to access host resources
   # Attempt capability-based exploits
   ```

6. **Denial of Service Testing**
   ```bash
   # Resource exhaustion
   - Very large transaction.json file
   - Rapid API calls (should be prevented by DCA timing)
   - Memory exhaustion attempts
   ```

### Known Limitations

1. **Environment File Security**: Secrets are stored on the host filesystem
   - Mitigation: Restricted permissions (`600`, root-owned)
   - For higher security, consider a secrets manager or volume driver with encryption

2. **Web Dashboard Authentication**: Single admin user via environment variables
   - Use a strong password and a random session secret
   - Place the dashboard behind an HTTPS reverse proxy in production

3. **Rate Limiting**: Configurable rate limits are applied to login, API, manual buy, settings, and control endpoints. Review `docs/configuration.md` and `docs/SECURITY_HARDENING.md` for defaults and tuning.

4. **Transaction Log Growth**: `transactions.json` grows indefinitely
   - For long-term use, implement log rotation or migrate to SQLite (`scripts/migrate_to_sqlite.py`)

## Security Best Practices

### File Permissions

```bash
chmod 600 <DEPLOYMENT_PATH>/config/.env
chmod 600 <DEPLOYMENT_PATH>/data/config.json
chmod 600 <DEPLOYMENT_PATH>/data/transactions.json
```

### Regular Updates

```bash
# Update base image
podman pull python:3.11-slim
podman-compose -f <DEPLOYMENT_PATH>/compose.yaml build --no-cache
```

### Secret Rotation

See [`docs/secrets.md`](docs/secrets.md#secret-rotation) for rotation procedures.

## Reporting Vulnerabilities

If you discover a security vulnerability:

1. **Do NOT** open a public issue
2. Email the repository owner privately
3. Provide:
   - Vulnerability description
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

## Security Checklist for Deployment

- [ ] API key has minimal permissions (no withdrawal)
- [ ] `.env` and `config/.env` have restricted permissions (`600`)
- [ ] Docker/Podman host is updated and patched
- [ ] Container resource limits are set
- [ ] Logs are monitored for suspicious activity
- [ ] Transaction history is backed up regularly
- [ ] API keys are rotated periodically
- [ ] Dashboard is behind HTTPS reverse proxy
- [ ] Strong dashboard password and random session secret
- [ ] Test with small amounts before production use

## Related Documents

- [`docs/SECURITY_HARDENING.md`](docs/SECURITY_HARDENING.md) — detailed hardening decisions, rate limits, brute-force protection, CSP, and local asset vendoring.
- [`docs/secrets.md`](docs/secrets.md) — secret management, rotation, and pre-commit hooks.
- [`docs/configuration.md`](docs/configuration.md) — full environment-variable reference including security-related settings.
- [`docs/operations.md`](docs/operations.md) — production deployment and host hardening.
- [`docs/operations/GO_LIVE_CHECKLIST.md`](docs/operations/GO_LIVE_CHECKLIST.md) — security checks before enabling live trading.

## Compliance Notes

- **GDPR**: No personal data collected
- **PCI DSS**: No credit card data handled
- **Financial Regulations**: User responsible for compliance with local laws
- **KYC/AML**: Handled by Kraken exchange

---

Last Updated: 2026-07-25
