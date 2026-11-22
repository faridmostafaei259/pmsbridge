# 0011. Opaque OAuth tokens and dynamic client registration

- Status: accepted
- Date: 2026-11-22

## Context
AI assistants connect to pmsbridge as OAuth 2.1 clients and call the MCP endpoint with access tokens. The authorization
server and the MCP endpoint run in the same process and share one database (decisions 0001, 0003, 0004).

## Decision
- Access and refresh tokens are opaque random values (at least 256 bits). Only their SHA-256 hashes are stored. The MCP
  endpoint validates a token by hash lookup, which also checks expiry, revocation, the user being active and the
  audience. There are no signing keys, so nothing needs to be rotated and revocation takes effect immediately.
- Access tokens live at most one hour; refresh tokens rotate on every use, and reuse of an old refresh token revokes the
  whole grant.
- Clients register with Dynamic Client Registration (RFC 7591), rate limited, and registrations that are never used
  expire. Client ID Metadata Documents (fetching a client's metadata from a URL) are not supported yet, because they
  require outbound requests to arbitrary hosts; this is revisited when assistants need it.
- Authorization uses the code flow with mandatory PKCE (S256), exact redirect URI matching, the `resource` parameter
  bound to the MCP endpoint URL, and a consent screen where the person chooses which PMS connections the assistant may
  use.

## Consequences
- The planned `pmsbridge rotate-oauth-keys` command is not needed and is dropped.
- Every MCP request costs one indexed database lookup, which is negligible for one company's load.
