# 0011. Opaque OAuth tokens and dynamic client registration

- Status: accepted (amended 2026-11-22: metadata documents added)
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
- Clients can identify themselves either way the MCP authorization specification allows: Dynamic Client Registration
  (RFC 7591), rate limited, with unused registrations expiring; and Client ID Metadata Documents (an HTTPS URL used as
  the client id, which pmsbridge fetches). The specification (2026-07-28) recommends metadata documents and keeps
  registration for compatibility, so both are supported. Fetching a metadata document is the only outbound request an
  assistant can trigger; it is limited to HTTPS on port 443, refuses private, loopback and link-local addresses
  (checked after DNS resolution, no redirects), caps size and time, and caches results. A metadata document is fetched
  only after the person has signed in (a stored copy may be used before that to check the return address), with a
  per-person, per-host and installation-wide allowance. A failed fetch is remembered for 5 minutes only to suppress
  retries: nothing from the failed response is kept or used as client metadata, which is how this implementation reads
  the draft's rule against caching error responses.
- Authorization uses the code flow with mandatory PKCE (S256), exact redirect URI matching (the only exception: for
  http loopback IP addresses, 127.0.0.1 and [::1], the port may differ, as RFC 8252 requires), the `resource` parameter
  bound to the MCP endpoint URL, and a consent screen where the person chooses which PMS connections the assistant may
  use. An approval given before any connection exists covers no connections; the person chooses them later.
- Before the person has signed in and acted on the consent screen, the server never redirects automatically to a
  client's address: request errors show a page naming the return host with a link back (RFC 9700 4.11.2).

## Consequences
- The planned `pmsbridge rotate-oauth-keys` command is not needed and is dropped.
- Every MCP request costs one indexed database lookup, which is negligible for one company's load.
