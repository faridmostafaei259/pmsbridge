# Contributing to pmsbridge

Thank you for helping. This guide covers the development setup and the rules every change must follow.

## Development setup

Requirements: Linux or macOS for development, [uv](https://docs.astral.sh/uv/), Docker with Compose v2.
Production deployments are Linux only.

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run python scripts/check_no_emoji.py
```

## Rules for every change

- Tests accompany every change. Tests never call real PMS APIs; use recorded-style fake fixtures.
- Type hints everywhere; `pyright` runs in strict mode on `src/`.
- MCP tools only use the canonical models in `pmsbridge.pms.models`. Provider payloads stay in adapters.
- Never commit secrets, `.env` files, databases, backups, certificates, or real customer data.
- No emojis anywhere in the repository, including commit messages.
- Shell scripts are Bash, start with `set -euo pipefail`, and pass `shellcheck`.
- Changes that affect configuration, migrations or deployment must keep install, update, backup and restore
  working, and must update the docs.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/), for example
  `feat(reservations): add check-in date filter`.

## Adding a PMS

Read design decision [0005](docs/adr/0005-pms-abstraction.md). A step-by-step guide will be added once the
second provider is complete.

## Reporting security issues

Do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
