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

### Security
- Strict default Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and `no-referrer` headers; error responses never include stack traces.
- Forwarded headers are trusted only from Caddy's fixed address on a private network.
- Caddy and the application run as unprivileged users with all capabilities dropped and read-only root filesystems. The internal `/readyz` endpoint is blocked at the public edge, request bodies are capped at 2 MB, and only Caddy publishes ports.
- Image scans in CI fail on fixable HIGH or CRITICAL findings. One known finding in the upstream Caddy binary is excepted until 2026-12-22.
