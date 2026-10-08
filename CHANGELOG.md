# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are tagged `v*` and published as container images to
`ghcr.io/mb86231/kraken-dca-bot:<tag>` (plus `:latest`). To update a running
installation, see **Updates** in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

## [1.2.1] — 2026-10-08

### Added

- **Fiat value display in Settings → Strategy**: every dynamic DCA tier shows
  an "≈ Value" column (amount × current price, live-updating while you type),
  and the base crypto amount shows its current value underneath the input.
  The page also states Kraken's minimum order size for the configured pair.
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

[1.2.1]: https://github.com/mb86231/kraken-dca-bot/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/mb86231/kraken-dca-bot/compare/v1.1.5...v1.2.0
[v1.1.0]: https://github.com/mb86231/kraken-dca-bot/releases/tag/v1.1.0
[v1.1.5]: https://github.com/mb86231/kraken-dca-bot/releases/tag/v1.1.5
