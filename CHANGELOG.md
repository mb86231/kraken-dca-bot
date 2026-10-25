# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are tagged `v*` and published as container images to
`ghcr.io/mb86231/kraken-dca-bot:<tag>` (plus `:latest`). To update a running
installation, see **Updates** in [`docs/INSTALLATION.md`](docs/INSTALLATION.md).

## [1.4.1] — 2026-10-25

### Fixed

- **Morning briefing no longer requires the AI advisor master toggle.** The
  deterministic briefing (bot-computed values, news blocks) runs on its own
  feature switch in Settings → Reporting; only the optional AI narrative and
  the alert explainer still require the master switch. The global kill
  switch overrides everything.
- **Clearer test-briefing errors.** The dashboard test button now reports
  actionable reasons instead of a bare "disabled in config": which switch is
  missing, or why the AI layer is unavailable.
- Settings → Reporting help texts updated to match the new behaviour.

## [1.4.0] — 2026-10-25

The optional **AI advisor** layer. Everything is off by default, every
function has its own switch plus a global kill switch, and the advisor is
strictly advisory — it can never place orders, move budgets, or change
strategy parameters. Bring your own OpenAI-compatible API endpoint (or
Ollama); the API key is stored in the local secrets file (0600), never in
`config.json`.

### Added

- **AI advisor foundation**: provider abstraction (OpenAI-compatible APIs
  and Ollama) with local schema validation, timeouts, retries, circuit
  breaker, and request/token/cost limits. If the AI is unreachable or
  returns garbage, the bot keeps running unchanged and says so — a compact
  deterministic template is used instead.
- **Morning briefing via Telegram**: scheduled report (daily/weekly/monthly,
  configurable time) with bot-computed numbers only — price, invested
  amount, P/L in percent, budget, next cycle. The AI optionally writes a
  short narrative; without AI the template briefing is sent. Content
  sections are selectable (Current View / Bot View / News).
- **Alert explainer**: AI-written plain-language explanations for bot
  alerts, delivered asynchronously to Telegram. Advisory only.
- **Dashboard integration**: advisor status dot in the navigation, advisor
  and reporting settings tabs (grouped cards), test-briefing button with
  provider error surfacing, briefing card on the dashboard.
- **AI narrative customization**: `narrative_focus` (market / portfolio /
  plan / news) selects what the AI covers, `narrative_max_words` bounds the
  length (40–400, default 120), and `custom_prompt` replaces the default
  task text with a user template (`{pair}`, `{price}`, `{invested}`,
  `{pnl_pct}`, `{next_run}`, …). Safety rules are unchanged: the bot
  supplies every number, the AI only writes prose, no financial advice.
- **News blocks** (deterministic, no AI tokens): `news_general` and
  `news_pair` briefing sections. The bot fetches the configured RSS/Atom
  `news_feeds` itself (default: CoinDesk, Cointelegraph; short cache,
  failing feeds skipped) and filters pair news by base asset. With the
  `news` focus topic, the AI narrative may comment on the headlines.
- **Hot reload**: saving the advisor API key or advisor settings rebuilds
  the advisory stack in the running bot — no restart needed.

### Fixed

- **Update check with pre-release versions**: a running beta like
  `1.4.0-beta.1` no longer disables the update ticker — the final release
  of the same version (e.g. v1.4.0) is correctly offered as an update.

## [1.3.4] — 2026-10-25

### Added

- **Settings → System tab.** System health (bot process, exchange API,
  Telegram, storage, runtime info) and the update-check state now live in a
  dedicated *System* tab instead of cluttering the strategy page. The
  last-update-check time is shown as a formatted date, and a dismissed
  update banner stays dismissed across page loads.
- **Hot reload: most settings apply without a restart.** Saving exchange
  credentials, the live-trading toggle, the update-check configuration, or
  strategy settings now rebuilds the affected runtime component in the
  running bot (e.g. the Kraken API client is swapped immediately). A restart
  is only needed for the few settings that still require it — the UI says
  so where applicable.
- **Poll-interval hint.** The settings page now explains the minimum poll
  interval and why a lower value is not accepted.

### Fixed

- **Audit hardening F1–F6** (see `docs/` audit report; regression tests in
  `tests/test_audit_fixes.py`):
  - F1: a `deposit_day` of 31 no longer crashes the trading loop in
    30-day months — cycle computation clamps the day to the month's end.
  - F2: orders stuck in `SUBMITTED` are reconciled against Kraken instead
    of blocking every later buy.
  - F3: dry-run buys no longer consume real budget accounting.
  - F4: resolve endpoints guard against inconsistent state and return 409
    instead of masking errors.
  - F5: order placement respects the remaining budget precisely.
  - F6: an unexpected error inside the trading loop (API shape change,
    calendar edge case, lock timeout) no longer kills the bot — the loop
    logs the error, records it in state, raises a throttled alert, and
    backs off one poll cycle.

## [1.3.3] — 2026-10-24

### Added

- **Update ticker: the bot now tells you when a newer release exists.** A
  background check polls the public GitHub releases API at a configurable
  interval (Settings → *Check for Updates*, default every 24 h). When a
  newer release is found, the dashboard shows a dismissible banner with a
  link to the release notes, and the bot sends a one-time Telegram message
  plus an info alert. The ticker is strictly informational — it never
  downloads or installs anything, and any failure (offline, API error,
  malformed response) is silent so trading is never disturbed.
  Pre-release tags are never treated as updates.

## [1.3.2] — 2026-10-21

### Fixed

- **Performance: P/L charts follow the real market.** The "Unrealized
  Profit / Loss" and "Invested vs. Market Value" charts valued every data
  point at its own transaction's fill price, so the curve only moved on
  buy days and hid everything that happened between buys. Both charts are
  now built on Kraken OHLC closes: holdings step up at each buy and every
  candle between buys is valued at its own close, so drawdowns and
  recoveries between buys are visible. When market data is unavailable the
  charts degrade to the previous trade-price valuation instead of failing,
  with a visible note on the chart.
- **Performance: market price freshness is now visible.** The KPIs value
  the portfolio at the bot's last ticker price, but nothing showed how old
  that price was — a paused bot or a dead price feed silently showed stale
  valuations. The dashboard now shows the price timestamp under "Market
  Price" and marks it red with a "stale" note when the price is older than
  15 minutes.

## [1.3.1] — 2026-10-12

### Fixed

- **Dynamic DCA tier matching**: a dip tier (e.g. -2%) previously fired on
  any price change at or above its threshold — including rises and dips
  shallower than 2% — so the bot placed a "Dynamic -2%" buy nearly every
  cycle. Dip tiers now require the drop to actually reach the threshold
  (`change <= threshold`), a rise can never match a dip tier (and vice
  versa), and a drop beyond the deepest tier uses the deepest tier's
  amount instead of falling back to the smaller base amount. Tier labels
  ("Dynamic -2%") are now truthful.

### Fixed

- **Dashboard test briefing behind a reverse proxy**: a slow or unreachable
  AI provider made the manual test send outlive the proxy timeout, so the
  UI showed a bare `504` instead of the real error. The test path now uses
  a single provider attempt with the timeout capped at 90 s (below the
  reverse-proxy limit; reasoning models need 40–50 s for briefing
  prompts), and the UI explains gateway timeouts. The scheduled briefing
  is unaffected and keeps the configured provider timeout/retries.

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

[1.3.3]: https://github.com/mb86231/kraken-dca-bot/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/mb86231/kraken-dca-bot/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/mb86231/kraken-dca-bot/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/mb86231/kraken-dca-bot/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/mb86231/kraken-dca-bot/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/mb86231/kraken-dca-bot/compare/v1.1.5...v1.2.0
[v1.1.0]: https://github.com/mb86231/kraken-dca-bot/releases/tag/v1.1.0
[v1.1.5]: https://github.com/mb86231/kraken-dca-bot/releases/tag/v1.1.5
