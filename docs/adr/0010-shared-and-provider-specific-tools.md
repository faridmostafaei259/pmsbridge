# 0010. Shared tools and provider-specific tools

- Status: accepted
- Date: 2026-11-22
- Amends: 0005

## Context
Decision 0005 requires every MCP tool to work the same for every PMS. That fits the common areas (listings,
reservations, calendar, guests, messages, reviews, owners, payments, tasks). But a large part of each PMS's API has
no equivalent elsewhere: for Guesty, for example, its accounting add-on, saved replies, fee automation, rate
strategies and promotions. Forcing those into shared models would either leave them out or produce models that fit
no provider well.

## Decision
Two kinds of tools:
1. **Shared tools** for capabilities that every supported PMS offers. They use only the canonical models in
   `pmsbridge.pms.models`, never read provider-specific keys and never branch on the provider (decision 0005 still
   applies to them).
2. **Provider-specific tools** for capabilities that only one PMS offers. They are named with the provider as a prefix
   (for example `guesty_list_saved_replies`), live in their own modules, use their own typed models (never raw
   payloads), and are offered only when the requested connection belongs to that provider. They follow every other
   rule: authorization on each call, write actions off by default and enabled per connection by an administrator,
   audit entries, bounded results, no automatic retries of writes, and no sensitive financial identifiers.

When a second provider gains an equivalent capability, the feature moves into a shared tool and the
provider-specific tool is deprecated under the tool compatibility policy.

## Consequences
- Much more of each PMS's API can be covered without weakening the shared tools.
- Tool names and parameters stay a public API for both kinds.
- Provider-specific code is clearly separated, so adding a PMS still never changes shared tools.
