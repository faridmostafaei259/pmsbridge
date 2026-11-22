# Threat model

This document is updated at the end of every development milestone.

## Assets
- PMS credentials (Guesty client ID and secret, Hostaway account ID and API key) and cached PMS tokens
- Guest personal data and reservation details returned by the PMS
- The ability to change PMS data (calendar prices, guest messages, review replies, tasks)
- OAuth signing keys, refresh tokens, user passwords and session cookies
- The encryption key in the server's `.env` file
- Backup archives (they contain the database and the `.env` file, so an unencrypted backup exposes the encryption key together with the encrypted credentials)

## Entry points
- The public HTTPS endpoint: dashboard, OAuth endpoints, MCP endpoint
- The server itself (SSH, Docker), outside the application's control
- The update channel (release bundles and images)

## Trust boundaries
- Internet to Caddy (TLS)
- Caddy to the application (internal network only)
- Application to PMS APIs (outbound HTTPS to fixed hosts)
- Operator's server to the release channel (verified by checksums and image digests)

## Key abuse cases and planned controls
| Abuse case | Control |
|---|---|
| Stolen or replayed OAuth tokens | Short-lived, audience-bound access tokens; rotating refresh tokens; hashed storage; revocation |
| An AI client using a PMS connection it was not granted | Allowed connections recorded at consent and checked on every tool call |
| Unwanted changes to PMS data | Write actions off by default, enabled per connection by an admin, audited |
| Exhausting the Guesty token budget (5 per 24 hours) | Single locked refresh path, persistent token cache, budget tracking |
| Credential theft from a stolen disk | AES-GCM encryption at rest with versioned keys; the key lives in `.env`, readable only by the app user |
| Credential theft from a stolen backup | A backup holds both the database and `.env`, so encryption at rest does not protect it. Backups copied off the server must be encrypted with a passphrase (required for the extra-location copy); local backups are readable only by root |
| Brute-force login | argon2id, rate limiting, two-factor auth required for admins |
| Tampered update | Digest-pinned images and checksum verification before applying updates |
| Server-side request forgery | PMS base URLs are fixed in code and never user-controlled |
| Sensitive data in logs | Structured logging that excludes secrets, tokens and guest personal data |
| Prompt injection through PMS content (a guest message, review or note tells the AI to send messages or change prices) | Write actions off by default and enabled per connection; write tools marked destructive so AI clients ask for confirmation; bounded calendar updates (positive prices, limited ranges and counts); every write audited |
| Malicious client registration or consent phishing (lookalike app names, attacker redirect URIs, flooding the registration endpoint) | Consent screen shows the client name and redirect host; exact redirect URI matching; registration rate limits; unused registrations expire |
| Tampered pagination cursors | Cursors are opaque, never contain URLs or hosts, are parsed strictly and rejected when malformed |
| Duplicate actions after timeouts (message sent twice) | Write operations are never retried automatically; unknown outcomes are reported to the client |
