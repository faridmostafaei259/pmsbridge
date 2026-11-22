# 0005. Provider-neutral PMS abstraction

- Status: accepted
- Date: 2026-11-22

## Context
The gateway must support Guesty and Hostaway now and other PMSs later without changing MCP tools.

## Decision
MCP tools only use the canonical models in `pmsbridge.pms.models`. Each provider implements the `PMSProvider` protocol in `pmsbridge.pms.base`: one optional sub-API per domain plus a capability set. A provider instance is bound to one connection. Unsupported features raise `CapabilityNotSupportedError` with a client-safe message. Provider-specific fields go into `extras`.

## Consequences
- Adding a PMS means writing an adapter and its tests; tools stay unchanged.
- If a new provider cannot fit the canonical models, the models change through a new ADR, never through tool-level special cases.
- Tools never read named keys from `extras` and never branch on the provider. `extras` holds only scalar, non-secret values (at most 50), never raw payloads.
- Adding a provider also adds a member to the `Provider` enum, a reviewed contract change.
- Write operations are never retried automatically, so a timeout cannot send a guest message twice.
