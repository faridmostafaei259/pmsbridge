# 0001. Architecture overview

- Status: accepted
- Date: 2026-11-22

## Context
Property managers want AI assistants to work with their PMS data. Both assistants support remote MCP servers over HTTPS with OAuth. Companies want to keep PMS credentials on infrastructure they control.

## Decision
pmsbridge is a single FastAPI application that serves a web dashboard, an OAuth 2.1 authorization server, and an MCP endpoint (Streamable HTTP, stateless) at `/mcp`. Caddy terminates TLS in front of it. MCP tools are written against provider-neutral models; each PMS is an adapter behind the `PMSProvider` protocol (see 0005).

## Consequences
- One deployable unit keeps install and upgrade simple.
- The OAuth server, the dashboard and MCP share one process and one database, so they must stay well separated in code (accounts/, oauth/, mcp/, pms/).
