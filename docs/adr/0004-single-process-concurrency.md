# 0004. Single application process

- Status: accepted
- Date: 2026-11-22

## Context
The real throughput limit is the PMS API (Guesty: 15 requests per second per account), not the gateway. Token refresh must happen exactly once per connection, and rate limits must be shared by all requests.

## Decision
Run exactly one async Uvicorn process. Token refresh uses a per-connection asyncio lock plus a `BEGIN IMMEDIATE` SQLite transaction. Rate limiting and concurrency caps are in-process. The container entrypoint enforces a single worker.

## Consequences
- Simple, correct coordination without Redis or distributed locks.
- Code must never assume several workers. Scaling is vertical only.
