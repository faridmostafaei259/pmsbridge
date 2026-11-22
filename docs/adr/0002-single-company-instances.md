# 0002. One instance per company

- Status: accepted
- Date: 2026-11-22

## Context
The project is self-hosted. Each property management company runs its own instance on its own server.

## Decision
An instance serves exactly one company. It supports several staff users and several PMS connections, but there is no multi-tenancy. Isolation between companies comes from separate deployments. Hosting several companies on one instance is out of scope.

## Consequences
- No tenant model, no row-level security, far less complexity.
- Authorization inside an instance is per user and per PMS connection (OAuth grants list the allowed connections).
- An agency serving several companies runs one instance per company.
