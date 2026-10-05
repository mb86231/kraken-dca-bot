# Dashboard Security Hardening

This document describes the security controls applied to the DCA-Bot web dashboard.

## Threat model

| Threat | Mitigation |
|--------|------------|
| Brute-force password guessing | Per-IP and per-username lockout after `LOGIN_MAX_FAILURES` failures |
| Credential stuffing / enumeration | Generic login error message; no hint whether username or password was wrong |
| Abuse of state-changing actions | Rate limits on login, API, manual buy, settings, backup, and restore endpoints |
| Supply-chain / CDN compromise | Chart.js and its date adapter are vendored locally and pinned |
| XSS / data exfiltration | Content Security Policy with `default-src 'self'` |
| Session hijacking | Required stable `SESSION_SECRET`; secure-cookie enforcement in production |
| Downgrade / MITM | HSTS from the reverse proxy; `WEB_UI_SECURE_COOKIE=true` required in production |
| Information leakage | Generic errors; no passwords, tokens, or session cookies logged |

## Rate limiting

Rate limits are applied by FastAPI dependencies. They are configurable through environment variables:

| Variable | Default | Used by |
|----------|---------|---------|
| `RATE_LIMIT_ENABLED` | `true` | Master switch |
| `RATE_LIMIT_LOGIN` | `5/minute` | `POST /login`, `/login/oidc`, `/auth/callback` |
| `RATE_LIMIT_API` | `60/minute` | Authenticated read-only API routes |
| `RATE_LIMIT_MANUAL_BUY` | `3/minute` | `POST /api/bot/cycle` |
| `RATE_LIMIT_SETTINGS` | `10/minute` | Settings, bot controls, backup, restore, Telegram, alert ack, log archive |
| `DISABLE_RATE_LIMIT` | `false` | Allowed in development/test only |

### Production invariants

In `APP_ENV=production` (and `staging`) the application refuses to start if rate
limiting is disabled through **any** supported configuration variable. The
following are rejected:

- `DISABLE_RATE_LIMIT=true`
- `RATE_LIMIT_ENABLED=false`
- any alias that resolves to disabling rate limits

`web/rate_limit.py` contains one authoritative helper, `rate_limit_enabled()`,
that parses these variables with strict Boolean logic. All other code checks this
helper rather than reading the raw environment variable directly.

In `development` and `test` environments rate limits may be disabled deliberately
for local debugging.

## Brute-force protection

Failed login attempts are tracked both by source IP and by username. After `LOGIN_MAX_FAILURES` failures (default `5`), the IP/username is locked out for `LOGIN_LOCKOUT_SECONDS` (default `900`). A successful login clears the failure counter for both keys.

The implementation is in-memory and suitable for the current single-process deployment. A multi-node deployment would need a shared store such as Redis.

## Login CSRF protection

The local login form (`POST /login`) requires a CSRF token:

1. `GET /login` sets a `csrf_token` cookie and renders the token in a hidden form field.
2. `POST /login` validates the submitted token against the cookie using constant-time comparison.
3. Missing, malformed, expired, or mismatched tokens are rejected with `403 Forbidden`.
4. The token is rotated after a successful login.

This protects against login CSRF attacks that could force an operator's session
to be authenticated under an attacker-controlled account. The brute-force rate
limit is applied before the CSRF check, so the protection cannot be used to
bypass lockout.

## OIDC JWKS key selection

When validating an OIDC ID token, the application selects the verification key by
matching the token header `kid` against the JWKS keys returned by the identity
provider. The behavior is fail-closed:

- If the token supplies a `kid`, the key must exist in the JWKS response.
- If the token does not supply a `kid`, the key set must contain exactly one
  compatible verification key.
- Ambiguous or missing key selection rejects the token.

This prevents a silent downgrade to `keys[0]` if the provider rotates signing
keys. Existing issuer, audience, nonce, PKCE, expiry, and signature validation are
preserved.

## Content Security Policy and security headers

FastAPI is the source of truth for the following headers via `SecurityHeadersMiddleware`:

- `Content-Security-Policy`
- `X-Content-Type-Options`
- `X-Frame-Options`
- `Referrer-Policy`
- `Permissions-Policy`
- `Cache-Control` for sensitive paths
- `Strict-Transport-Security` when the request is served over HTTPS (`X-Forwarded-Proto: https`)

The current CSP is:

```
default-src 'self';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
connect-src 'self';
font-src 'self';
frame-ancestors 'none';
base-uri 'self';
form-action 'self'
```

`'unsafe-inline'` is required because the dashboard templates contain inline scripts and styles. Future hardening could move these to external files and remove the directive.

Nginx should add only `Strict-Transport-Security` and must not duplicate the headers above. See `nginx/crypto-agent.conf`.

## Production startup validation

When `APP_ENV=production`, the application refuses to start unless:

- `SESSION_SECRET` is set and at least 16 characters long
- `WEB_UI_PASSWORD_HASH` is set
- `WEB_UI_SECURE_COOKIE=true`
- `DISABLE_RATE_LIMIT` is not `true`
- `RATE_LIMIT_ENABLED` is not `false`

These checks run in the application lifespan, before any traffic is handled.

## Local Chart.js and vendored dependency verification

Chart.js and the date-fns adapter are vendored in `web/static/vendor/chart.js/`.
The exact version, source URL, SHA-256 checksums, and update instructions are
documented in `web/static/vendor/chart.js/VENDOR.md` and a machine-readable
`web/static/vendor/chart.js/vendor-manifest.json`.

Verify the vendored files at any time:

```bash
python scripts/verify_vendor_checksums.py
```

CI runs the same script; any modification, mismatch, missing documented file,
or undocumented JavaScript file in the vendor directory fails the build. Do not
load Chart.js from a public CDN.

## Recommended next steps

1. Move inline scripts/styles to external files and tighten `script-src` to remove `'unsafe-inline'`.
2. Add a persistent failed-login store (Redis/database) if the dashboard is ever scaled beyond one process.
3. Add CAPTCHA or WebAuthn passkey support for the login form.
4. Periodically review and update the CSP and `Permissions-Policy` directives.
