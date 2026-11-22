# 0006. Distribution and installation

- Status: accepted
- Date: 2026-11-22

## Context
Operators are often not infrastructure experts. Installation, TLS, updates and backups must be automatic.

## Decision
Each release publishes multi-arch images (linux/amd64, linux/arm64) to the project's GitHub Container Registry, with an SBOM, a signature, and a release bundle (install.sh, the operator CLI, compose files with images pinned by digest, checksums). A single command installs Docker if needed, configures the firewall, obtains certificates, creates the first admin, and enables nightly backups. Updates are deliberately manual: the operator runs one command,
`sudo pmsbridge update`, which verifies the release, backs up, migrates, health-checks and rolls back
automatically on failure. Nothing updates on its own. Building from source stays possible with a flag.

## Consequences
- Releases must be reproducible and verified by automated install, upgrade and restore tests.
- The release pipeline becomes part of the security boundary.
- MCP tool names, parameters and result shapes are a public API: minor and patch releases only add to them,
  so connected AI clients keep working across updates. A snapshot check in CI enforces this.
- Operators must run the update command to receive security fixes; the docs and the optional update check
  make new releases visible.
