# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are tagged `v*` and published as container images to
`ghcr.io/mb86231/kraken-dca-bot:<tag>` (plus `:latest`). To update a running
installation, see **Updates** in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

## [1.3.0] — 2026-10-08

### Security

Security-hardening wave: findings F1–F9 of an independent review of the
live deployment, addressed in five stories (S1–S5).

- **No more duplicate buys after ambiguous failures** (S1/S2, finding F1):
  the HTTP transport retried every exception, so a timeout *after* Kraken
  accepted the order produced a duplicate buy. Order placement now retries
  only definitive rate-limit rejections; timeouts and 5xx (which may mean
  "accepted, reply lost") raise immediately, 502/503/504 classify as
  UNKNOWN, and UNKNOWN attempts reconcile via `userref` before any
  resubmission — else they go to HOLD for an operator decision.
- **Pause actually pauses** (S1, finding F5): pending retry submissions
  were processed before the pause check, so a retry that came due while
  paused still reached AddOrder after the dashboard said "paused".
  Retries now defer while paused (state kept, fires on resume);
  UNKNOWN reconciliation stays read-only; placement is gated as defence
  in depth.
- **Login lockout no longer renews itself indefinitely** (S3, finding
  F6): after `locked_until` expired, the stale failure count re-triggered
  a fresh lockout on every check — an attacker who knew the username
  could lock the single admin out forever. Expired lockouts now reset,
  failures decay after the tracking window, failures during a lockout no
  longer extend it, and the tracked-key map is pruned against memory
  exhaustion.
- **Stored-XSS sinks eliminated** (S4, finding F2): every dashboard
  template interpolated API/user-influenced values directly into
  innerHTML markup. All dynamic rendering now goes through textContent /
  DOM building (shared `dom-safe.js` helpers), with a static lint and
  payload-sweep regression guard. Removing the CSP `unsafe-inline`
  allowance is a tracked follow-up (it requires externalising inline
  scripts).
- **OIDC login is now fail closed** (S5, finding F3): only identities on
  the new `OIDC_ALLOWED_SUBJECTS` allowlist (matched against `sub`,
  `preferred_username` and `email`) receive a session. When OIDC is enabled
  and the allowlist is empty, every OIDC login is denied — configure the
  allowlist (env var or secrets store) before relying on SSO.
- **Telegram commands check the sender, not just the chat** (S5, finding F4): with
  `TELEGRAM_ALLOWED_USER_IDS` set, only those user IDs may issue commands or
  press confirmation buttons — even in group chats. Without it, only private
  chats are accepted, so group members can no longer drive the bot or confirm
  someone else's action. Confirmation callbacks are bound to the requesting
  user.
- **Sessions are revocable** (S5, finding F7): the dashboard keeps a server-side session
  registry (`data/sessions.json`, override with `SESSIONS_PATH`). Logout
  revokes the individual session — a copied cookie stops working immediately —
  and any admin credential change (set/change/clear, first-run setup)
  revokes **all** sessions. Existing sessions without a server-side ID are
  rejected; users simply log in again once after upgrading.
- **ID tokens must now contain `exp`, `iat`, `iss`, `aud` and `sub`** (S5, finding F8):
  PyJWT `require` replaces the weaker `require_exp`/`require_iat` options,
  which validated the claims only when present.
- **One boolean truth table for all security flags** (S5, finding F9): `web.flags`
  drives `WEB_UI_SECURE_COOKIE` validation, every cookie setter, preflight
  and rate limiting together — `1`/`yes`/`on` now consistently mean "true"
  everywhere (previously they passed validation but did not set the `Secure`
  cookie flag), and unrecognised values are rejected at startup in production.

## [1.2.1] — 2026-10-08

### Added

- **Fiat value display in Settings → Strategy**: every dynamic DCA tier shows
  an "≈ Value" column (amount × current price, live-updating while you type),
  and the base crypto amount shows its current value underneath the input.
  The page also states Kraken's minimum order size for the configured pair.
- **Version badge in the dashboard**: the navigation bar shows the running
  app version (e.g. `v1.2.1`) next to the DCA-Bot title.
- `/api/settings` now returns a `market` block (quote currency, last price,
  Kraken order minimum from the public AssetPairs endpoint, 5 s timeout,
  fail-closed).

### Fixed

- **Tier "Enabled" checkboxes rendered unchecked after reload**, no matter
  what had been saved — a stray quote produced `checked"=""` instead of a
  real `checked` attribute. Saving worked all along; the display did not.
  Regression test added.
- **Order minimum floor is now dynamic**: the bot enforces the pair's live
  `ordermin` from Kraken instead of a hardcoded guess (Kraken reports
  0.00005 XBT for BTC/EUR, not 0.001), falling back to 0.00005 only when
  metadata is unavailable. Demo mode keeps a tiny floor for toy amounts.

## [1.2.0] — 2026-10-07

### Added

- **Telegram command bot**: interactive controls with an allow-list,
  one-time confirmations, and an audit log (see `docs/TELEGRAM.md`).
- **Dynamic DCA tier labels**: orders record and display which tier
  triggered a buy (e.g. "Dynamic -5 %").
- **Budget feedback**: skipped buys explain why (visible in the dashboard
  and pushed to Telegram), and Buy Now can exceed the monthly budget once
  with explicit confirmation.
- **Configurable bind address** for the quick-start compose stack via
  `DCA_BOT_BIND` (loopback remains the default); `WEB_UI_SECURE_COOKIE`
  support for reverse-proxy TLS setups.
- Step-by-step installation walkthrough: [`docs/INSTALLATION.md`](docs/INSTALLATION.md)
  (localhost-only / LAN IP / reverse proxy).

### Fixed

- First-visit login CSRF mismatch (cookie was regenerated on every login view).
- Fresh-install volume permissions (`/app/data`, `/app/logs`).
- Health endpoint reporting "not ready" on fresh installs without Kraken
  credentials.

## Earlier releases

[v1.1.0]–[v1.1.5] shipped the initial public feature set (dashboard,
dynamic DCA tiers, Authentik OIDC + local login, preflight checks, secrets
management, staging/backup plumbing). See the git tags and commit history
for details.

[1.3.0]: https://github.com/mb86231/kraken-dca-bot/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/mb86231/kraken-dca-bot/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/mb86231/kraken-dca-bot/compare/v1.1.5...v1.2.0
[v1.1.0]: https://github.com/mb86231/kraken-dca-bot/releases/tag/v1.1.0
[v1.1.5]: https://github.com/mb86231/kraken-dca-bot/releases/tag/v1.1.5
