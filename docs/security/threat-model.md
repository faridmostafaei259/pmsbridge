# Threat model

This document is updated at the end of every development milestone. It says what is protected, who can
reach it, where the trust boundaries are, and which controls exist. Every control is marked
**Implemented** (present in the code today and tested) or **Planned** (a later milestone owns it).

Status: foundation milestone (container image, Docker Compose stack with Caddy, settings, logging,
SQLite storage and migrations, encryption vault, key rotation commands). There are no user accounts,
OAuth endpoints, PMS connections or MCP tools yet, so the controls for those are all marked Planned.

## Assets

| Asset | Where it lives today | Notes |
|---|---|---|
| `ENCRYPTION_KEYS` (the keys that protect stored credentials) | The operator's `.env` file; passed to the app container as an environment variable | Never written to logs, error messages, settings printouts or process arguments. Anyone who can run Docker commands on the server can read it (`docker inspect`, `docker compose config`); that is root-equivalent access and is out of scope for the application. |
| The SQLite database (`/data/pmsbridge.db`, plus the `-wal` and `-shm` files) | The `app_data` Docker volume | Folder `0700`, files `0600`, owned by the container user (uid 10001). Secrets are encrypted per field (AES-256-GCM); the rest of the file is not encrypted. |
| Pre-upgrade database backups (`/data/backups/pre-migration-*.db`) | Same volume | Complete copies of the database taken before each schema change, files `0600`, only the newest few kept (`PRE_MIGRATION_BACKUPS_KEEP`, default 5). They hold the same encrypted values as the live file, so they are only as safe as the key. |
| Caddy's certificate private keys, ACME account and (in `internal` mode) the private certificate authority | The `caddy_data` and `caddy_config` volumes | Folders `0700`, files `0600`, owned by the Caddy user (uid 10001). |
| The container images | Built locally from the repository; base images pinned by tag and digest | A tampered base image would run with the privileges of the container. |
| The CI pipeline and the lock file | GitHub Actions workflow; `uv.lock` with hashes | Actions are pinned by commit hash, the token is read-only, and no secrets are used. |
| PMS credentials (Guesty client ID and secret, Hostaway account ID and API key) and cached PMS tokens | Not stored yet. They will live in encrypted database columns | Planned: PMS connections milestone. |
| Guest personal data and reservation details returned by the PMS | Not handled yet | Planned: never logged, never stored beyond short-lived caches. |
| The ability to change PMS data (prices, guest messages, review replies, tasks) | Not available yet | Planned: write tools off by default. |
| OAuth signing keys, refresh tokens, user passwords and session cookies | Not available yet | Planned: accounts and OAuth milestones. |
| Full backup archives made by `pmsbridge backup` | Not available yet | Planned: packaging milestone. They will contain both the database and `.env`, so an unencrypted archive exposes the key together with the encrypted data. |

## Entry points

- **Public HTTPS (port 443) and the HTTP redirect (port 80)**, served by Caddy. Only these two ports are
  published. Today the application answers `/healthz` (a fixed `{"status":"ok"}`) and 404 for everything
  else. `/readyz` is answered with 404 by Caddy and never reaches the application.
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
                                                       +--> PMS APIs (outbound HTTPS, fixed hosts; not used yet)
Operator / Docker host  ------------------------------> everything above (full trust)
```

- **Internet to Caddy.** All visitor input is untrusted. Caddy terminates TLS, limits request bodies and
  decides which paths reach the app.
- **Caddy to the app.** The app trusts forwarded headers (`X-Forwarded-For`, `X-Forwarded-Proto`) from
  exactly one address: Caddy's fixed address on the private network. Every other sender, including other
  containers and the host's gateway address, is treated as an ordinary visitor.
- **App to its volume.** The app is the only writer of `/data`. It runs as an unprivileged user on a
  read-only root filesystem.
- **App to PMS APIs.** Planned: outbound HTTPS to fixed, hard-coded hosts.
- **Operator's server to the release channel.** Planned: checksums and image digests.

## Controls

| Area | Control | Status |
|---|---|---|
| Transport | TLS 1.2 and 1.3 only (older versions are refused), HTTP redirected to HTTPS, no HTTP/3 | Implemented |
| Transport | HSTS only with a publicly trusted certificate (`auto` mode), without subdomains or preload; never in `internal` mode | Implemented |
| Edge | Request bodies capped at 2 MB; `/readyz` hidden; `Server` header removed on proxied responses | Implemented |
| Edge | Only the configured `DOMAIN` is routed to the app, so a forged `Host` header does not reach it | Implemented |
| Edge | Caddy overwrites any `X-Forwarded-For` and `X-Forwarded-Proto` sent by a visitor; the app ignores forwarded headers from anyone but Caddy | Implemented |
| Edge | Access logs are off (request URLs can carry one-time OAuth values). Caddy's error log is not filtered yet; see the known gaps | Implemented (error log: known gap) |
| Responses | Every response carries `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, a restrictive `Content-Security-Policy`, `Permissions-Policy` and `Cache-Control: no-store` | Implemented |
| Responses | Unhandled errors return a fixed message with no traceback, path or exception text; details go to the server log only. API documentation pages are off | Implemented |
| Containers | Non-root user (uid 10001), read-only root filesystem, all capabilities dropped, `no-new-privileges`, default seccomp profile, small `noexec` tmpfs for `/tmp`, no privileged mode, no Docker socket, no host paths mounted | Implemented |
| Containers | Only Caddy publishes ports (80 and 443); the app publishes none, so it cannot be reached from outside the server. The app port is reachable from the Docker host and from containers on the backend network (see accepted risks). Caddy's admin interface listens inside its container only | Implemented |
| Application | An application-level request size limit, so that traffic which skips Caddy is capped too | Planned: edge hardening |
| Configuration | Settings come from environment variables only. Production requires HTTPS, a public address that matches `DOMAIN`, and encryption keys. Error messages name the setting, never its value | Implemented |
| Logging | One JSON object per line on standard output. Values under sensitive key names, bearer tokens, cookies, e-mail addresses and key entries are masked. Messages are length-capped | Implemented (known gaps below) |
| Storage | SQLite in WAL mode with private permissions on the data folder, database, sidecar files and backups. Bound values are hidden from database error messages. Writes use `BEGIN IMMEDIATE` | Implemented |
| Storage | Schema upgrades run inside a transaction after a consistent online backup. A failed upgrade, a damaged file, or a database from a newer version stops the app with a plain message and leaves the file unchanged | Implemented |
| Encryption | AES-256-GCM with a random nonce per value. The key id and a per-field context string (for example the table, row and column) are authenticated, so a value copied to another row or tampered with does not decrypt. Versioned keys allow rotation with `docker compose exec app python -m pmsbridge.admin keys status` and `... keys reencrypt` (the host wrapper `pmsbridge rotate-keys` is planned) | Implemented (in-container commands); Planned (host wrapper) |
| Supply chain | Base images pinned by tag and digest; locked dependencies with hashes; `pip-audit` and Trivy (fixable HIGH and CRITICAL findings fail the build); GitHub Actions pinned by commit hash with a read-only token; Dependabot for actions, Python packages and both images | Implemented |
| Supply chain | Images and release bundles verified before an update is applied | Planned: packaging and release milestone |

## Key abuse cases

| Abuse case | Control | Status |
|---|---|---|
| A visitor forges `X-Forwarded-For` or `X-Forwarded-Proto` to hide their address or fake HTTPS | Caddy replaces the headers; the app accepts them only from Caddy's address. Another container or the host sending the headers is ignored | Implemented |
| A visitor forges a `Host` header to poison links or redirects | Caddy routes only `DOMAIN` to the app. Issuer and resource URLs will come from `PUBLIC_BASE_URL`, never from request headers | Implemented (routing); Planned (OAuth URLs) |
| Oversized or malformed requests, request smuggling | 2 MB body cap; Caddy rejects conflicting length headers and malformed requests; connections that never complete a request are closed by the proxy | Implemented |
| Information leakage through errors, headers or docs pages | Fixed error bodies, no framework or server banner on application responses, no API docs, `/readyz` hidden | Implemented |
| Sensitive data in logs | Redaction by key name and by pattern; the encryption key is also recognised by its shape; startup errors never repeat setting values | Implemented (see known gaps) |
| Credential theft from a stolen disk or database file | Field-level AES-256-GCM encryption; the key is not in the database or the volume | Implemented |
| Credential theft from a stolen backup | Pre-upgrade backups hold only ciphertext plus the rest of the database; the key is not in them. Full backup archives will hold the key too, so copies leaving the server must be encrypted with a passphrase | Implemented (pre-upgrade backups); Planned (full backups) |
| Tampered or swapped ciphertext (copying one row's secret into another, flipping bits, truncation, key id swap) | GCM authentication with the header and a per-field context; any change fails with a clear error and no plaintext | Implemented |
| A damaged, hostile or newer database file | The file is refused with a plain message and left unchanged; only revision names known to this version are used in backup file names; unknown ones stop the app first | Implemented |
| Container compromise (a bug in the app) | Non-root, read-only filesystem, no capabilities, no new privileges, no Docker socket, private network with a single published service | Implemented |
| Tampered dependency or base image | Digests and hashes; vulnerability scanning in CI; pinned CI actions | Implemented |
| Tampered update | Digest-pinned images and checksum verification before applying updates | Planned |
| Stolen or replayed OAuth tokens | Short-lived, audience-bound access tokens; rotating refresh tokens; hashed storage; revocation | Planned |
| An AI client using a PMS connection it was not granted | Allowed connections recorded at consent and checked on every tool call | Planned |
| Unwanted changes to PMS data | Write actions off by default, enabled per connection by an admin, audited | Planned |
| Exhausting the Guesty token budget (5 per 24 hours) | Single locked refresh path, persistent token cache, budget tracking | Planned |
| Brute-force login | argon2id, rate limiting, two-factor auth required for admins. Rate limits must not rely on the client address alone, because all IPv6 visitors currently appear as one address (see accepted risks) | Planned |
| Server-side request forgery | PMS base URLs are fixed in code and never user-controlled | Planned |
| Prompt injection through PMS content (a guest message, review or note tells the AI to send messages or change prices) | Write actions off by default and enabled per connection; write tools marked destructive so AI clients ask for confirmation; bounded calendar updates; every write audited | Planned |
| Malicious client registration or consent phishing (lookalike app names, attacker redirect URIs, flooding the registration endpoint) | Consent screen shows the client name and redirect host; exact redirect URI matching; registration rate limits; unused registrations expire | Planned |
| Tampered pagination cursors | Cursors are opaque, never contain URLs or hosts, are parsed strictly and rejected when malformed | Planned |
| Duplicate actions after timeouts (message sent twice) | Write operations are never retried automatically; unknown outcomes are reported to the client | Planned |
| Compression side channels (BREACH) once the dashboard shows secrets next to reflected input | Do not compress responses that contain secrets or one-time tokens, or mask them per response | Planned: accounts milestone |

## Accepted risks and known gaps

These are known today. Items marked "tracked" have a regression test in `tests/security` that is
marked as an expected failure; it starts failing (and must be turned into a normal test) the day the
gap is fixed.

| Item | Impact | Status |
|---|---|---|
| Log redaction is a safety net, not a guarantee, and it has real gaps. It does not recognise: secrets inside escaped JSON (a JSON string that itself contains JSON); a quoted value that contains an escaped quote (the tail after the quote is written in clear); URL-encoded pairs such as `password%3D...`; these key names: `session`, `sessionid`, `sid`, `csrf`, `code` (an OAuth authorization code in a query string), `code_verifier`, `totp`, `otp`, `mfa_code`, `recovery_code`, `assertion`, `client_assertion`, `jwt`, `signature`, `hmac`, `signing_key`, `pwd`, `passcode`; a bare JWT in free text; or a key entry glued to a word character | A secret logged in one of these forms would be written in clear. Code must not log secrets or guest details at all. Severity: major | Tracked (about 50 expected-failure tests). Must be fixed before the accounts and OAuth milestone, which is the first to handle such values |
| Caddy's error log (written when the app is down or a proxied request fails, for example a 502) records the full request URI including the query string, the request path, and all request headers except `Cookie` and `Authorization` (for example `X-Csrf-Token`). Request bodies are not logged | A one-time code or token in a URL, or a secret in a custom header, would be written to the proxy log whenever a request fails | Tracked; planned: a Caddy log filter that drops the query string and masks sensitive headers (edge hardening) |
| A log call whose argument raises an unexpected error while being rendered can raise into the caller (only four error types are contained) | A failing log call could break a request; the error text is masked when the request error is logged | Tracked |
| `TRUSTED_PROXIES=*` is accepted in production | An operator who sets it lets any container on the backend network, and the Docker host, set the client address | Tracked; the default and the compose file use Caddy's single address; planned: refuse it in production (edge hardening) |
| The app port is reachable from the Docker host and from any container on the backend network. Traffic sent that way skips Caddy, so it also skips the 2 MB body cap, the hidden `/readyz` (it answers with the check names), TLS and the `Host` routing. Forwarded headers on such traffic are ignored | Someone with a shell on the server (already root-equivalent through Docker) can send oversized bodies or read `/readyz`. A compromised container on the backend network could do the same; only the two project containers are attached | Accepted; mitigated later by the planned application-level request size limit |
| The backend network is IPv4-only. IPv6 visitors reach Caddy through Docker's proxy, so the app sees them all as the bridge gateway address | The client address is not unique for IPv6 visitors; IP-based limits would lump them together | Accepted for now; a dual-stack option is planned |
| The backend network is not marked internal, so both containers have outbound access (the app needs the PMS APIs, Caddy needs the certificate authority) | A compromised app could reach other hosts, including a cloud provider's metadata address | Accepted; planned: restrict egress once the PMS hosts are known |
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
  invariants, and the known gaps listed above.
- At the end of each milestone: static analysis (bandit, semgrep), secret scanning over the full
  history (gitleaks), image scans, a license check, and hands-on attacks against a running stack. Their
  findings are fixed or listed above.
