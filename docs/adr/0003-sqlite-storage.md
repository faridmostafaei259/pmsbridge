# 0003. SQLite for persistent state

- Status: accepted
- Date: 2026-11-22

## Context
Some state must survive restarts: users and sessions, encrypted PMS credentials, cached PMS tokens (Guesty allows only 5 token requests per 24 hours per client ID), and OAuth client registrations and refresh tokens (losing them disconnects every AI client). Operators should not have to run a database server.

## Decision
Use one SQLite database file in a Docker volume, in WAL mode with a busy timeout and foreign keys on, accessed through SQLAlchemy (async, aiosqlite). Migrations use Alembic in batch mode and run automatically on start after an automatic backup. Backups use SQLite's online backup, never a raw file copy.

## Consequences
- Nothing extra to install, operate or secure.
- Write concurrency is limited; acceptable for one company's load (see 0004).
- Horizontal scaling across several app containers is not supported.
- The database and `.env` are readable only by the application user. Losing the encryption key in `.env` makes the stored PMS credentials unrecoverable (users must re-enter them), so the installer tells operators to back it up.
