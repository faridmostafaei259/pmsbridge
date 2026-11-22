# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Project skeleton, provider-neutral PMS contract and canonical data models.
- Architecture overview, threat model and design decisions 0001-0008 (including the Elastic License 2.0).
- In-memory fake provider for tests that proves the provider contract is implementable.
- Application factory with `/healthz` liveness and `/readyz` readiness endpoints, default security headers, and discovery-based feature registration.
- Single-worker application entry point that trusts forwarded headers only from the configured proxy address.
- Multi-stage, non-root (UID 10001), read-only-root-compatible container image with a health check.
- Docker Compose stack (Caddy and app) with persistent certificate storage, health checks, log rotation, and an optional systemd unit.
- Caddy TLS modes `auto` (Let's Encrypt with ZeroSSL fallback, HSTS) and `internal` (testing only), with HTTP to HTTPS redirect.
- Continuous integration: lint, type check, tests, shellcheck, emoji check, generated-docs freshness, dependency audit, image build and scan, compose validation and an edge smoke test. Dependabot updates for Actions, Python and container images.
- Operator guide for TLS and domains.
- Settings are read from environment variables only and validated at start-up, with clear errors that name the variable. In production the public address must be HTTPS and match `DOMAIN`.
- Structured JSON logging to standard output that masks passwords, tokens, cookies, e-mail addresses and phone numbers.
- SQLite storage in WAL mode with private file permissions. Database migrations run automatically at start-up after an online backup (newest 5 kept). A failed or newer-version start leaves the database unchanged and explains how to recover.
- Database readiness check on `/readyz`.
- Generated configuration reference, including what to do if the application stops after an update.
- Stored PMS credentials and tokens are encrypted with AES-256-GCM using versioned keys from the new `ENCRYPTION_KEYS` setting. Each value is bound to its own row and key header.
- `python -m pmsbridge.admin keys status|reencrypt` shows which key each stored secret uses and re-encrypts everything under the active key. `status` refuses to confirm a rotation when no encrypted column is registered.
- Dashboard sign-in and sign-out with local accounts. Passwords are stored as argon2id hashes (12 to 256 characters, no composition rules).
- Server-side sessions that end after 12 hours without activity or 7 days in total (`SESSION_IDLE_HOURS`, `SESSION_MAX_DAYS`). Only a hash of the session cookie is stored, and every sign-in starts a new session.
- Administrator guide covering signing in, session lifetime, sign-in lockouts and troubleshooting.
- One-time setup link for the first administrator (`users create-admin`) and a password-reset link for administrators (`users reset-admin-password`). Links work once, expire after 24 hours and are stored only as hashes.
- Two-factor sign-in with authenticator apps (TOTP), required for administrators and optional for staff on the new Account security page, with single-use recovery codes.
- Invite staff and administrators from the dashboard with a one-time link that works once and ends after 72 hours (no e-mail is sent).
- Users page for administrators: list people, invite, cancel invitations, disable and enable accounts, reset two-factor sign-in and change roles. Sensitive actions ask for the administrator's password and a current code, valid for 10 minutes.
- Admin commands `users list`, `users invite`, `users disable`, `users enable` and `users reset-2fa`; the last one is also the recovery path for an administrator who lost their phone and recovery codes.
- OAuth 2.1 authorization server, first part: discovery metadata (RFC 8414 and RFC 9728), Dynamic Client Registration (rate limited; unused registrations expire after 24 hours, idle ones after 90 days), and an authorization endpoint with mandatory PKCE (S256), exact redirect matching and RFC 9207 `iss` responses.
- A consent screen shows the assistant's name and where the browser returns, and lets the person choose PMS connections. Authorization codes are single use, last 5 minutes and are stored only as hashes.
- OAuth token endpoint with PKCE verification and client authentication (public, client_secret_post, client_secret_basic): one-hour access tokens and rotating refresh tokens (30 days idle, 90 days at most) with reuse detection that revokes the whole approval, and a cap of 10 refreshes per approval per minute.
- OAuth token revocation endpoint (RFC 7009), advertised in the authorization server metadata.
- A Connected apps page where people see, revoke and choose connections for the assistants they approved, and an Assistants page for administrators (re-authentication required to revoke).
- `oauth revoke-ai-access` administrator command (by person, by assistant, or everything with `--yes`); prints counts only.

### Security
- Strict default Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and `no-referrer` headers; error responses never include stack traces.
- Forwarded headers are trusted only from Caddy's fixed address on a private network.
- Caddy and the application run as unprivileged users with all capabilities dropped and read-only root filesystems. The internal `/readyz` endpoint is blocked at the public edge, request bodies are capped at 2 MB, and only Caddy publishes ports.
- Image scans in CI fail on fixable HIGH or CRITICAL findings. One known finding in the upstream Caddy binary is excepted until 2026-12-22.
- Log redaction masks every `ENCRYPTION_KEYS` entry wherever it appears.
- Log redaction also masks session ids, CSRF tokens, OAuth authorization codes and verifiers, one-time, backup and recovery codes, signatures, assertions and JWTs, including inside escaped or JSON-encoded messages, quoted values, URL-encoded pairs, multi-word names such as "Session ID" and raw header lists. Short names match only as whole words, and the matching stays fast on hostile input.
- A log call whose argument fails while being rendered no longer raises into the caller, and the error text is not written.
- Threat model updated with concrete assets, trust boundaries, implemented versus planned controls, accepted risks and known gaps.
- Security regression tests covering hostile ciphertext, secrets in logs, forged forwarded headers, hostile database files and deployment invariants, with strict expected-failure tests that track each known gap until it is fixed.
- Dashboard forms are protected against cross-site request forgery with per-response masked tokens and an Origin/Referer check, including the sign-in form.
- Failed sign-ins are rate limited per e-mail address (5 failures) and per network address (20, or 200 for private and shared addresses) in 15 minutes, with a 5-minute pause. Attempts are counted before the password is checked, so simultaneous guesses cannot exceed a limit. An unknown e-mail address takes as long as a wrong password and gets the same message.
- Dashboard pages send a strict Content-Security-Policy (same-origin styles, no scripts) and `Referrer-Policy: same-origin`.
- Wrong two-factor codes are limited to 5 per 15 minutes and 20 per 24 hours per person; each code works once; authenticator secrets are encrypted at rest and covered by key rotation. An administrator session without confirmed two-factor sign-in is ended on the next request, and a password reset ends sign-ins that began with the old password.
- The last active administrator cannot be disabled or demoted, and administrators cannot lock themselves out. Disabling an account, resetting two-factor sign-in or promoting staff to administrator signs the person out everywhere. User ids are never reused after a person or invitation is deleted.
- Authorization request errors show a page with a "Return to <host>" link and never redirect automatically, so the server cannot be used as an open redirector. Disabling a person, resetting their password or two-factor sign-in, or changing their role revokes the assistant approvals they gave.
- Assistant access ends immediately when a person is disabled, their password or two-factor sign-in is reset, or their role changes; an administrator without two-factor sign-in cannot use refresh tokens. Tokens are stored only as SHA-256 hashes and never logged. Failed token requests are limited per address, and wrong secrets per assistant that has a secret; a public assistant's id never blocks anyone.
- The application refuses to start in production when `TRUSTED_PROXIES` trusts every address (`*`, `0.0.0.0/0`, `::/0`) or a very large network (IPv4 shorter than /8, IPv6 shorter than /16).
- The application rejects request bodies over 2 MiB with 413, by declared length and by counting streamed bodies, so traffic that reaches it without Caddy is also capped.
- Caddy's log no longer records query strings or request headers in either TLS mode.
- Database error messages and logs never include bound parameter values. A start-up failure is logged once as redacted JSON, never as a raw traceback.
