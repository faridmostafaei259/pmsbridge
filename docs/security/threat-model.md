# Threat model

This document is updated at the end of every development milestone. It says what is protected, who can
reach it, where the trust boundaries are, and which controls exist. Every control is marked
**Implemented** (present in the code today and tested) or **Planned** (a later milestone owns it).

Status: foundation milestone (container image, Docker Compose stack with Caddy, settings, logging,
SQLite storage and migrations, encryption vault, key rotation commands) plus the accounts and OAuth
milestone (dashboard sign-in, two-factor, invitations, user management, the OAuth authorization server
with the token endpoint, connected-apps pages and the assistant web-address fetch). PMS connections
and the MCP endpoint with its tool framework are built; controls that depend on the PMS data tools are
marked Planned.

## Assets

| Asset | Where it lives today | Notes |
|---|---|---|
| `ENCRYPTION_KEYS` (the keys that protect stored credentials) | The operator's `.env` file; passed to the app container as an environment variable | Never written to logs, error messages, settings printouts or process arguments. Anyone who can run Docker commands on the server can read it (`docker inspect`, `docker compose config`); that is root-equivalent access and is out of scope for the application. |
| The SQLite database (`/data/pmsbridge.db`, plus the `-wal` and `-shm` files) | The `app_data` Docker volume | Folder `0700`, files `0600`, owned by the container user (uid 10001). Secrets are encrypted per field (AES-256-GCM); the rest of the file is not encrypted. |
| Pre-upgrade database backups (`/data/backups/pre-migration-*.db`) | Same volume | Complete copies of the database taken before each schema change, files `0600`, only the newest few kept (`PRE_MIGRATION_BACKUPS_KEEP`, default 5). They hold the same encrypted values as the live file, so they are only as safe as the key. |
| Caddy's certificate private keys, ACME account and (in `internal` mode) the private certificate authority | The `caddy_data` and `caddy_config` volumes | Folders `0700`, files `0600`, owned by the Caddy user (uid 10001). |
| The container images | Built locally from the repository; base images pinned by tag and digest | A tampered base image would run with the privileges of the container. |
| The CI pipeline and the lock file | GitHub Actions workflow; `uv.lock` with hashes | Actions are pinned by commit hash, the token is read-only, and no secrets are used. |
| PMS credentials (Guesty client ID and secret, Hostaway account ID and API key) and cached PMS tokens | Stored in encrypted database columns (AES-256-GCM, bound to the connection id); cached tokens likewise | Implemented |
| Guest personal data and reservation details returned by the PMS | Not handled yet | Planned: never logged, never stored beyond short-lived caches. |
| The ability to change PMS data (prices, guest messages, review replies, tasks) | Not available yet | Planned: write tools off by default. |
| User passwords, session cookies, one-time links, authenticator secrets and recovery codes, OAuth authorization codes, access and refresh tokens, client secrets | The SQLite database | Passwords are argon2id hashes; sessions, links, codes, tokens and client secrets are stored only as SHA-256 hashes of 256-bit random values (96 bits for recovery codes); authenticator secrets are encrypted with the vault. OAuth tokens are opaque, so there are no signing keys (ADR 0011). |
| Full backup archives made by `pmsbridge backup` | Not available yet | Planned: packaging milestone. They will contain both the database and `.env`, so an unencrypted archive exposes the key together with the encrypted data. |

## Entry points

- **Public HTTPS (port 443) and the HTTP redirect (port 80)**, served by Caddy. Only these two ports are
  published. The application answers `/healthz` (a fixed `{"status":"ok"}`), the dashboard (sign-in,
  one-time link pages, two-factor, administrator and connected-apps pages, all behind a session except
  sign-in and the link pages), the OAuth endpoints (`/.well-known/oauth-*` discovery documents,
  `/oauth/register`, `/oauth/authorize`, `/oauth/token`, `/oauth/revoke`; the registration, token and
  revocation endpoints are reachable by anyone and are rate limited) and 404 for everything else.
  `/readyz` is answered with 404 by Caddy and never reaches the application. The MCP endpoint `/mcp`
  accepts only POST requests with a bearer access token (no cookies, so no CSRF; no CORS headers) and
  refuses everything else.
- **The private backend network** shared by Caddy and the app. Nothing else is attached to it. The app
  listens on port 8000 on this network. That port is not published on the server's network cards, but on
  a Linux server the Docker host itself can reach the app's bridge address directly, so anyone with a shell
  on the server can talk to the app without going through Caddy. Forwarded headers sent that way are
  ignored.
- **The server itself** (SSH, the Docker daemon, the `.env` file, Docker volumes). Outside the
  application's control: whoever controls Docker controls everything.
- **The container image build and the dependency supply chain** (base images, Python packages, the Caddy
  binary, GitHub Actions).
- **The update channel.** Planned: release bundles and image digests, verified before applying.

## Trust boundaries

```
Internet --(TLS 1.2+)--> Caddy --(private network)--> app --> /data volume
                                                       |
                                                       +--> PMS APIs (outbound HTTPS, fixed hosts)
                                                       +--> assistant metadata hosts (outbound HTTPS 443, guarded, after sign-in)
Operator / Docker host  ------------------------------> everything above (full trust)
```

- **Internet to Caddy.** All visitor input is untrusted. Caddy terminates TLS, limits request bodies and
  decides which paths reach the app.
- **Caddy to the app.** The app trusts forwarded headers (`X-Forwarded-For`, `X-Forwarded-Proto`) from
  exactly one address: Caddy's fixed address on the private network. Every other sender, including other
  containers and the host's gateway address, is treated as an ordinary visitor.
- **App to its volume.** The app is the only writer of `/data`. It runs as an unprivileged user on a
  read-only root filesystem.
- **App to PMS APIs.** Implemented: outbound HTTPS to fixed, hard-coded hosts, with timeouts, request path checks, a circuit breaker and per-account rate limits.
- **App to assistant metadata hosts.** Implemented: outbound HTTPS on port 443 to the public host named in an assistant's
  web-address identity, only after a person signs in, under the server-side request forgery guard.
- **Operator's server to the release channel.** Planned: checksums and image digests.

## Controls

| Area | Control | Status |
|---|---|---|
| Transport | TLS 1.2 and 1.3 only (older versions are refused), HTTP redirected to HTTPS, no HTTP/3 | Implemented |
| Transport | HSTS only with a publicly trusted certificate (`auto` mode), without subdomains or preload; never in `internal` mode | Implemented |
| Edge | Request bodies capped at 2 MB; `/readyz` hidden; `Server` header removed on proxied responses | Implemented |
| Edge | Only the configured `DOMAIN` is routed to the app, so a forged `Host` header does not reach it | Implemented |
| Edge | Caddy overwrites any `X-Forwarded-For` and `X-Forwarded-Proto` sent by a visitor; the app ignores forwarded headers from anyone but Caddy | Implemented |
| Edge | Access logs are off (request URLs can carry one-time OAuth values). Caddy's own log is filtered in both TLS modes: query strings are cut from the URI and request headers are not recorded. The request path is kept, so one-time values (invite, reset or OAuth codes) must travel in query strings or request bodies, never in path segments | Implemented (filter); design rule for future routes |
| Responses | Every response carries `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, a restrictive `Content-Security-Policy`, `Permissions-Policy` and `Cache-Control: no-store` | Implemented |
| Responses | Unhandled errors return a fixed message with no traceback, path or exception text; details go to the server log only. API documentation pages are off | Implemented |
| Containers | Non-root user (uid 10001), read-only root filesystem, all capabilities dropped, `no-new-privileges`, default seccomp profile, small `noexec` tmpfs for `/tmp`, no privileged mode, no Docker socket, no host paths mounted | Implemented |
| Containers | Only Caddy publishes ports (80 and 443); the app publishes none, so it cannot be reached from outside the server. The app port is reachable from the Docker host and from containers on the backend network (see accepted risks). Caddy's admin interface listens inside its container only | Implemented |
| Application | Request bodies over 2 MiB are rejected with 413 by the application itself (declared length and streamed bodies), so traffic that skips Caddy is capped too. In production the application refuses to start if `TRUSTED_PROXIES` trusts every address | Implemented |
| Dashboard accounts | Passwords stored as argon2id hashes (12 to 256 characters), rehashed when parameters change; unknown e-mail addresses take as long as wrong passwords and get the same message. Server-side sessions: the cookie (`__Host-`, Secure, HttpOnly, SameSite=Lax) holds a random value and only its hash is stored; every sign-in starts a new session; idle and absolute lifetimes are enforced by the server. All cookies use the `__Host-` prefix (Secure, HttpOnly, Path=/, no Domain): the session and the setup/reset flow cookies are SameSite=Lax (a setup link opened from a mail program is a cross-site navigation), the sign-in page, second-factor and enrollment cookies are SameSite=Strict; every form also needs its CSRF token and a matching Origin. An administrator session without confirmed two-factor sign-in is ended on the next request | Implemented |
| Cross-site request forgery | A masked synchronizer token (a fresh mask per response) on every state-changing dashboard request, plus an Origin or Referer check against `PUBLIC_BASE_URL`; the sign-in form is protected too | Implemented |
| Dashboard pages | Strict Content-Security-Policy (same-origin styles, no scripts), `Referrer-Policy: same-origin` (so browsers send the Origin header the CSRF check needs), `Cache-Control: no-store, no-transform`, form input parsed without echoing values in errors | Implemented |
| One-time setup and reset links | 256-bit random tokens, only hashes stored, single use, 24-hour expiry, reissuing invalidates the previous link; the token leaves the address bar on the first request | Implemented |
| Second-factor secrets | Authenticator secrets are encrypted with the vault and bound to the user; recovery codes are stored only as hashes and work once; the enrollment page is not cached and runs no scripts | Implemented |
| Invitation links | Hash-only, single use, 72-hour expiry, reissuing replaces the link and ends open flows; the token travels in a query string and leaves the address bar on the first request; an invited administrator must confirm two-factor sign-in before the account is active | Implemented |
| User management by a hijacked administrator session | Administrator-only pages with CSRF and Origin checks; inviting, disabling, enabling, resetting two-factor and changing roles need the password and a current code confirmed within 10 minutes (wrong attempts share the code limits); administrators cannot lock themselves out and the last active administrator cannot be disabled or demoted; disabling, resetting two-factor and promoting sign the person out everywhere. The confirmation lives in memory, is tied to the session and covers any of these actions for 10 minutes; cancelling a pending invitation needs no confirmation (it only removes an account that was never activated) | Implemented |
| Administrator who lost the authenticator and all recovery codes | `users reset-2fa` on the server resets two-factor sign-in (audited); anyone able to run it already controls the server. The commands exist as modules (`pmsbridge.admin.users`, `pmsbridge.admin.oauth`); the `python -m pmsbridge.admin` entry point that runs them arrives with the packaging milestone | Implemented (command); Planned (entry point) |
| Open redirect or code theft through the authorization endpoint | Exact redirect matching (for loopback IP addresses only the port may differ, per RFC 8252); no redirect before the client and redirect address are validated, and no automatic redirect at all before the person has signed in and acted on the consent screen (request errors show a page naming the return host); the consent page allows form submission only to the exact redirect origin; the consent form re-validates every field and needs CSRF, Origin and a session | Implemented |
| Abuse of client registration (filling the database, lookalike names) | JSON only with a 16 KiB cap, per-address and global rate limits, unused registrations expire; the assistant's name is shown escaped and marked unverified, and the return address host is shown prominently with a warning for localhost | Implemented |
| Authorization code interception or replay | PKCE with S256 only, exact redirect binding, codes last 5 minutes, work once and are stored as hashes; a code is spent only when the client, return address and verifier match; replaying a code revokes its approval | Implemented |
| An approval outliving its person | Disabling a person, resetting their password or two-factor sign-in, or changing their role revokes the approvals they gave | Implemented |
| Someone reads the database | Only SHA-256 hashes of authorization codes, access and refresh tokens and client secrets (all 256-bit random values) are stored, so none can be replayed | Implemented |
| Guessing client secrets or abusing the token endpoint | Constant-time comparison and the same work for unknown clients. Failed requests are limited per visitor address (30 in 15 minutes; 300 for private addresses shared by everyone behind the proxy). Wrong secrets for an assistant that has a secret are also limited per assistant (20 in 15 minutes). A public assistant has no secret to guess, so its claimed id never blocks requests. Request bodies are small and form-only | Implemented |
| Server-side request forgery through an assistant's web-address identity (Client ID Metadata Documents) | Only https on port 443 to a host name with a path; every resolved address must be public (private, loopback, link-local, carrier-grade NAT, cloud metadata and IPv4-in-IPv6 forms are refused); the connection is pinned to the checked address so DNS rebinding cannot redirect it; the certificate is verified for the host name; no redirects; 5-second and size limits; fetches happen only after a person has signed in (a stored copy may be used before that to check the return address); 30 fetches per hour overall, 5 per host and 10 per person, one at a time with at most a 5-second wait for a turn; only authorization requests fetch, the token endpoint uses the stored copy; response bodies are never logged | Implemented |
| Impersonation through a metadata document | The consent page shows the verified host of the assistant's web address, the unverified name it claims and the return host, with an extra warning for loopback return addresses; nothing redirects before the person acts | Implemented |
| OAuth mix-up attacks | The issuer (`iss`) is included in every authorization response and advertised in the metadata | Implemented |
| Configuration | Settings come from environment variables only. Production requires HTTPS, a public address that matches `DOMAIN`, and encryption keys. Error messages name the setting, never its value | Implemented |
| Logging | One JSON object per line on standard output. Values under sensitive key names (passwords, tokens, session ids, CSRF tokens, OAuth codes and verifiers, one-time and recovery codes, signatures, assertions), bearer tokens, JWTs, cookies, e-mail addresses and encryption key entries are masked, including inside escaped or JSON-encoded messages, quoted values, URL-encoded pairs, multi-word names such as "Session ID" and raw header lists, within the limitations listed under accepted risks. Short names match only as whole words. A log call that fails while rendering is contained and its error text is dropped. Messages are length-capped | Implemented |
| Storage | SQLite in WAL mode with private permissions on the data folder, database, sidecar files and backups. Bound values are hidden from database error messages. Writes use `BEGIN IMMEDIATE` | Implemented |
| Storage | Schema upgrades run inside a transaction after a consistent online backup. A failed upgrade, a damaged file, or a database from a newer version stops the app with a plain message and leaves the file unchanged | Implemented |
| Encryption | AES-256-GCM with a random nonce per value. The key id and a per-field context string (for example the table, row and column) are authenticated, so a value copied to another row or tampered with does not decrypt. Versioned keys allow rotation without downtime: the status and re-encryption logic is implemented and tested, and the `pmsbridge rotate-keys` command that runs it on a server arrives in a later release | Implemented (encryption, rotation logic); Planned (rotation command) |
| Supply chain | Base images pinned by tag and digest; locked dependencies with hashes; `pip-audit` and Trivy (fixable HIGH and CRITICAL findings fail the build); GitHub Actions pinned by commit hash with a read-only token; Dependabot for actions, Python packages and both images | Implemented |
| Supply chain | Images and release bundles verified before an update is applied | Planned: packaging and release milestone |

## Key abuse cases

| Abuse case | Control | Status |
|---|---|---|
| A visitor forges `X-Forwarded-For` or `X-Forwarded-Proto` to hide their address or fake HTTPS | Caddy replaces the headers; the app accepts them only from Caddy's address. Another container or the host sending the headers is ignored | Implemented |
| A visitor forges a `Host` header to poison links or redirects | Caddy routes only `DOMAIN` to the app. The issuer, resource and every OAuth and CSRF address come from `PUBLIC_BASE_URL`, never from request headers (a forged `Host` sent straight to the app changes nothing in the discovery documents) | Implemented |
| Oversized or malformed requests, request smuggling | 2 MB body cap; Caddy rejects conflicting length headers and malformed requests; connections that never complete a request are closed by the proxy | Implemented |
| Information leakage through errors, headers or docs pages | Fixed error bodies, no framework or server banner on application responses, no API docs, `/readyz` hidden | Implemented |
| Sensitive data in logs | Redaction by key name and by pattern; the encryption key is also recognised by its shape; startup errors never repeat setting values | Implemented |
| Credential theft from a stolen disk or database file | Field-level AES-256-GCM encryption; the key is not in the database or the volume | Implemented |
| Credential theft from a stolen backup | Pre-upgrade backups hold only ciphertext plus the rest of the database; the key is not in them. Full backup archives will hold the key too, so copies leaving the server must be encrypted with a passphrase | Implemented (pre-upgrade backups); Planned (full backups) |
| Tampered or swapped ciphertext (copying one row's secret into another, flipping bits, truncation, key id swap) | GCM authentication with the header and a per-field context; any change fails with a clear error and no plaintext | Implemented |
| A damaged, hostile or newer database file | The file is refused with a plain message and left unchanged; only revision names known to this version are used in backup file names; unknown ones stop the app first | Implemented |
| Container compromise (a bug in the app) | Non-root, read-only filesystem, no capabilities, no new privileges, no Docker socket, private network with a single published service | Implemented |
| Tampered dependency or base image | Digests and hashes; vulnerability scanning in CI; pinned CI actions | Implemented |
| Tampered update | Digest-pinned images and checksum verification before applying updates | Planned |
| Stolen or replayed OAuth tokens | Access tokens last one hour, are bound to the MCP address and can be revoked at once by the person or an administrator; refresh tokens rotate on every use, are bound to the client and end 30 days after last use or 90 days after the approval; reusing a retired refresh token revokes the whole approval; tokens stop working the moment the person is disabled or reset. A runaway or abusive refresh loop is capped at 10 successful refreshes per approval per minute, and an administrator's refresh token stops working, and the approval is cancelled, if the administrator's two-factor sign-in has been removed | Implemented |
| An AI client using a PMS connection it was not granted | Allowed connections recorded at consent and checked on every tool call; a connection that was not granted gets the same answer as one that does not exist, and no other connection's name or id appears in a tool answer | Implemented |
| Personal data or secrets in the audit log | Tool calls are recorded with person, assistant, connection, tool, outcome and duration only, never arguments, results or tokens; entries are deleted after `AUDIT_RETENTION_DAYS` | Implemented |
| Unwanted changes to PMS data | Write actions off by default, enabled per connection by an admin, audited | Implemented in the tool framework; applies to each write tool as it is added |
| Exhausting the Guesty token budget (5 per 24 hours) | One refresh at a time per connection, a persistent encrypted token cache reused until shortly before expiry, budget tracking from Guesty's response headers, a pause that grows after each failed token request, no token requests for connections that need attention, and a block until the reset time when the budget is used up; only one module may request tokens (enforced by a test) | Implemented |
| A token issued for another client id or Guesty account | Refused; a security event is logged and the connection is marked as needing attention | Implemented |
| The same Guesty account or the same API key connected twice | Refused by uniqueness checks and database constraints; a repeated key is refused before any token is requested | Implemented |
| Abuse of outbound PMS requests | PMS base addresses are fixed in code; a circuit breaker stops calls to a failing PMS; responses are capped at 8 MiB; errors never contain PMS response bodies | Implemented |
| Brute-force login | argon2id, rate limiting, two-factor auth required for admins. Rate limits must not rely on the client address alone, because all IPv6 visitors currently appear as one address (see accepted risks) | Implemented: sign-in rate limits counted before the password check; two-factor sign-in (required for administrators) with wrong codes limited to 5 per 15 minutes and 20 per 24 hours per person, and each code step accepted once; addresses keyed per IPv6 /64 once dual-stack exists, today all IPv6 visitors share one address (see accepted risks) |
| Server-side request forgery | Assistant web addresses: guarded fetch (see Controls). PMS base URLs are fixed in code and never user-controlled, and every request path is checked before sending (no absolute addresses, host changes, query or fragment, or dot segments) | Implemented |
| Prompt injection through PMS content (a guest message, review or note tells the AI to send messages or change prices) | Write actions off by default and enabled per connection; write tools marked destructive so AI clients ask for confirmation; bounded calendar updates; every write audited | Planned |
| Malicious client registration or consent phishing (lookalike app names, attacker redirect URIs, flooding the registration endpoint) | Consent screen shows the client name (escaped, marked unverified) and the redirect host, with a warning for loopback addresses; exact redirect URI matching; registration rate limits; unused registrations expire. A lookalike name with the attacker's own return address cannot be stopped technically: the person sees the host and decides (see accepted risks) | Implemented |
| Tampered pagination cursors | Cursors are opaque, never contain URLs or hosts, are parsed strictly and rejected when malformed | Planned |
| Duplicate actions after timeouts (message sent twice) | Write operations are never retried automatically (the only exception is a "too many requests" answer with a wait time, which means the PMS did not process it); unknown outcomes are reported to the client | Implemented in the HTTP client and tool framework |
| Compression side channels (BREACH) once the dashboard shows secrets next to reflected input | Caddy does not compress responses at all, and CSRF tokens are also masked with a fresh random value per response | Implemented |

## Accepted risks and known gaps

These are known today. Items marked "tracked" have a regression test in `tests/security` that is
marked as an expected failure; it starts failing (and must be turned into a normal test) the day the
gap is fixed.

| Item | Impact | Status |
|---|---|---|
| No request rate limit on `/mcp` for signed-in assistants | A stolen access token or a misbehaving assistant can make many calls that never reach the PMS (for example `list_connections` or refused calls) and grow the tool-call record until the token expires (one hour). Calls that reach the PMS are already limited per PMS account. Outsiders without a token are refused after one database read, with no write | Known gap: a per-approval limit is added before the first tool that changes data |
| Log redaction is a safety net, not a guarantee. Known limitations: an unquoted value containing spaces is masked only up to the first space; short names glued to other words (`mysid`, `newcode`) are not recognised; double-encoded or percent-encoded names and e-mail addresses are not matched; JWTs not starting with `eyJ` are matched only under a sensitive name; an escaped secret containing a lookalike closing sequence can be closed early; only the last two words of a multi-word name decide sensitivity; names outside the lists (for example `pin`, `nonce`, `state`) are never masked; output is cut at 8,000 characters before masking; anything written outside Python logging (for example server output before logging starts) is not covered; Caddy's own log is handled separately by its log filter | A secret could reach the logs if code logs it in one of these shapes | Accepted: code must never log secrets; redaction is the second line of defence, covered by regression tests |
| The app port is reachable from the Docker host and from any container on the backend network. Traffic sent that way skips Caddy, so it skips the hidden `/readyz` (it answers with the check names), TLS and the `Host` routing. Forwarded headers on such traffic are ignored | Someone with a shell on the server (already root-equivalent through Docker) can read `/readyz` (check names only). A compromised container on the backend network could do the same; only the two project containers are attached | Accepted; the application enforces its own 2 MiB body limit and ignores forwarded headers from such traffic |
| Sign-in rate limits live in memory | A restart clears the counters, and anyone who knows an e-mail address can lock that account out of password sign-in for 5 minutes by failing on purpose | Accepted: limits are short, documented in the admin guide, and two-factor sign-in (required for administrators) adds a second factor |
| Second-factor guessing budget and lockout | A correct password alone does not give a session. Wrong codes are limited per person to 5 per 15 minutes and 20 per 24 hours (counted before checking, so simultaneous guesses count); with 3 valid codes out of a million at any time, that is at most about 0.006% per day. The limits live in memory and reset on restart, and someone who knows a password can lock that person out of the code step for up to 24 hours | Accepted: knowing the password is already required; recovery codes share the same limits |
| Someone using many addresses can use up the installation-wide limit of 30 client registrations per hour (malformed requests count too, and visitors that arrive from a private or internal address have no per-address limit, only the installation-wide one) | New AI assistants cannot register for up to an hour; assistants already registered keep working, and assistants that identify themselves by web address do not need to register | Accepted |
| Someone who knows a confidential assistant's client id can lock that assistant out of the token endpoint for 5 minutes by sending wrong secrets | The assistant cannot get or refresh tokens for a few minutes | Accepted: the limit is what stops secret guessing; public assistants are never affected |
| A signed-in person can use up a host's metadata fetch allowance (5 per hour) with other addresses on the same host. A link to the authorization endpoint with a web-address client id also makes the server fetch on behalf of whoever opens it while signed in, using that person's allowance of 10 per hour | New approvals for assistants on that host, or for that person, are delayed for up to an hour; existing approvals and stored copies keep working. Only public https hosts can be fetched, and nothing from a failed response is used or shown | Accepted |
| Consent phishing: someone registers an assistant with a trusted-looking name and their own return address and sends a person the authorization link | The person is asked to approve access for an assistant that is not the one they think it is; a mistaken approval hands the code to the attacker's address, which also needs the PKCE verifier the attacker chose | Accepted: the consent page escapes the name, marks it unverified and shows the return host in large type with a warning for loopback addresses; approvals can be cancelled on the connected-apps page and by `oauth revoke-ai-access`; web-address identities show a verified host |
| The e-mail address check is permissive: it accepts addresses that contain characters such as `<`, `>` or quotes (`<b>@x.test` is accepted by the invite form) | Pages are rendered with automatic escaping (checked on the users page and the sign-in page), and the application sends no mail, so there is no known way to use this | Open, low priority |
| Revoked approvals stay in the database until their assistant is deleted (unused registrations after 24 hours, others after 90 idle days), and expired codes, tokens and sessions are deleted only when a later request of the same kind runs | The database grows slowly with use; nothing secret is exposed because all stored values are hashes | Open, low priority |
| All IPv6 visitors currently reach the app from one internal address (the backend network is IPv4-only), so they share one per-address sign-in count | Repeated failed sign-ins from one IPv6 visitor could pause password sign-in for other IPv6 visitors | Mitigated: private and internal addresses get a much higher per-address limit (the per-e-mail limit still applies to every account); removed when the backend becomes dual-stack in a later release |
| The backend network is IPv4-only. IPv6 visitors reach Caddy through Docker's proxy, so the app sees them all as the bridge gateway address | The client address is not unique for IPv6 visitors; IP-based limits would lump them together | Accepted for now; a dual-stack option is planned |
| The backend network is not marked internal, so both containers have outbound access (the app needs the PMS APIs, Caddy needs the certificate authority) | A compromised app could reach other hosts, including a cloud provider's metadata address | Accepted: outbound HTTPS to public hosts is needed for assistant metadata documents, so egress cannot be limited to the PMS hosts; the metadata fetch refuses private, loopback, link-local and metadata addresses |
| Anyone with Docker access can read `ENCRYPTION_KEYS` (`docker inspect`, `docker compose config` prints the env file contents) | Root-equivalent access already exposes the key and the volume. Diagnostic commands the project adds later must not print it | Accepted |
| Caddy's upstream binary has an unfixed HIGH finding in `go-jose` (no fixed version exists) and a fixed HIGH finding in `golang.org/x/net` (CVE-2026-78661, accepted until 2026-12-22 in `.trivyignore.yaml`) | Whether either flaw can be reached in this deployment depends on which parts of Caddy are used; neither is known to be exploitable here. CI ignores unfixed findings, so the first one will only surface once a fix exists | Accepted; watch Caddy releases and remove the exception when one fixes it |
| The Caddy image does not apply operating-system package updates after the pinned base, and the application image still contains `pip` | MEDIUM and LOW findings with fixes available; not gating | Open, low priority |
| Neither container has a memory or process limit | A runaway process can use the host's memory | Open, low priority |
| The proxy does not limit connections per visitor | A flood of slow connections can use up the proxy's connections for a while; this is a single-tenant service behind its own domain | Accepted |

## Operator responsibilities

- Keep the `.env` file readable by root only, back up `ENCRYPTION_KEYS` separately from database backups,
  and keep retired keys as long as any backup made before a rotation is kept.
- Keep the server, Docker and the base operating system updated; the application cannot protect a
  compromised host.
- Publish only ports 80 and 443 (the compose file already does) and keep the Docker daemon socket private.

## How this is checked

- Automated, on every change: lint, type checks, tests, `pip-audit`, Trivy on both images, generated
  documentation freshness, and a compose smoke test of the edge (headers, redirect, hidden readiness).
- Security regression tests that need no running stack are in `tests/security`: hostile ciphertext,
  secrets pushed through logging, forged forwarded headers, hostile database files, deployment
  invariants, the known gaps listed above, and the attacks on accounts and OAuth (open redirects,
  cross-site posts, lockout, redirect address and PKCE tricks, code and refresh-token replay, tokens
  that must die with their person, hostile assistant addresses, staff against administrator pages).
- At the end of each milestone: static analysis (bandit, semgrep), secret scanning over the full
  history (gitleaks), image scans, a license check, and hands-on attacks against a running stack. Their
  findings are fixed or listed above.
