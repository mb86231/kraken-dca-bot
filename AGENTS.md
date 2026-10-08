# AGENTS.md

Guidelines for anyone (human or coding agent) working in this repository.

## Language

- **All code, comments, commit messages, and documentation are written in
  English.** This includes UI-facing documentation (`docs/`, `README.md`,
  `CHANGELOG.md`, `INSTALL.md`) and user-visible strings added to the
  dashboard. Commit message style: imperative summary line
  (`fix(web): …`, `feat(bot): …`), body wrapped at ~72 characters,
  signed off with `Co-authored-by: Kimi <noreply@moonshot.ai>` for
  agent-authored commits.

## Repository layout

- `bot/` — trading core: config, strategies, order execution, Kraken API
  client, preflight checks, Telegram command bot.
- `web/` — FastAPI dashboard (routers in `web/routers/`, Jinja templates in
  `web/templates/`).
- `tests/` — pytest suite; run `python -m pytest tests/ -q`.
- `docs/` — user-facing documentation (English).
- `.gitea/` — private infrastructure (workflows, mirror sync script); never
  published to the public GitHub mirror.
- `compose.public.yaml` — the public quick-start stack (ghcr image).

## Quality gates (all must pass before pushing)

```bash
python -m ruff check .
python -m mypy .
python -m pytest tests/ -q
python scripts/check_docs.py
```

The detect-secrets baseline (`.secrets.baseline`) is line-number sensitive:
if you insert lines into a file that has baseline entries, refresh the
baseline in the same commit (`detect-secrets scan --baseline
.secrets.baseline --all-files --exclude-files '^\.git/'`, then drop entries
for files that were never tracked, e.g. `.gitea/`), and verify against the
sanitised public export tree.

## Public mirror

This repository is mirrored one-directionally to
`github.com/mb86231/kraken-dca-bot` by `.gitea/scripts/sync_public_mirror.py`
(tree export with internal facts replaced by placeholders; `.gitea/` and
`systemd/` excluded). Never commit real hostnames, internal IPs, or secrets:
the mirror verifies the export and fails closed.

See `.gitea/PUBLISHING.md` (private infrastructure doc, never published)
for the full release flow: what gets published, how tags and container
images reach GitHub, and how GitHub releases are created from
`CHANGELOG.md`.
