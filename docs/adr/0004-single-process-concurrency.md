# 0004. Single application process

- Status: accepted
- Date: 2026-11-22

## Context
The real throughput limit is the PMS API (Guesty: 15 requests per second per account), not the gateway. Token refresh must happen exactly once per connection, and rate limits must be shared by all requests.

## Decision
Run exactly one async Uvicorn process. Token refresh is serialised by a per-connection asyncio lock, which is enough because there is only one process. The refresh re-reads the stored token in a short read, calls the PMS token endpoint with no database transaction open, and then stores the result in a short write transaction. No database transaction is ever held across a network call, so a slow token request cannot block other writes such as sign-ins and audit entries. Rate limiting and concurrency caps are in-process. The container entrypoint enforces a single worker.

## Consequences
- Simple, correct coordination without Redis or distributed locks.
- The SQLite write lock is only held for short local writes.
- Code must never assume several workers. Scaling is vertical only.

## Amendments
- 2026-11-23: the token refresh no longer holds a `BEGIN IMMEDIATE` transaction across the call to the PMS token endpoint. Holding the SQLite write lock for up to the token request timeout blocked every other writer and could make their writes fail. The per-connection asyncio lock gives the same exclusivity in a single process.
