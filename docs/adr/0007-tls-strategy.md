# 0007. TLS and domains

- Status: accepted
- Date: 2026-11-22

## Context
Claude and ChatGPT require a publicly reachable HTTPS URL with a publicly trusted certificate. Operators must never handle certificates manually.

## Decision
Caddy obtains and renews certificates automatically (ACME, Let's Encrypt with fallback) and stores them in a persistent volume. Supported modes: auto, dns (DNS-01 for firewalled servers), custom (operator certificates), proxy (behind an existing reverse proxy), internal (testing only). `PUBLIC_BASE_URL` is the single source of truth for the OAuth issuer, resource URLs, redirects and cookies. HSTS is sent only in the auto, dns and custom modes, where a publicly trusted certificate is in use.

## Consequences
- The only manual step is creating one DNS record, once.
- Changing the domain later forces AI clients to reconnect, because the OAuth issuer changes.
