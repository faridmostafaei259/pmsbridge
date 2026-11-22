# 0009. No data collection; the operator is responsible for use

- Status: accepted
- Date: 2026-11-22

## Context
pmsbridge is a tool that a property management company installs on its own server to let its own AI assistant
work with its own PMS account. The maintainers do not run a hosted service and do not want to receive, store or
process anyone's data.

## Decision
- pmsbridge never sends data to the maintainers or to any third party chosen by the project: no telemetry, no
  analytics, no license checks, no crash reporting. Outbound connections go only to the operator's PMS, the
  certificate authority, GitHub release metadata when the operator runs an update check, and the web address an AI
  assistant uses as its identity, fetched under the guarded rules in decision 0011 (no data is sent with that request).
- PMS credentials and all PMS data stay on the operator's server. Data the AI assistant reads through pmsbridge is
  sent to that assistant, which the operator chose and which is governed by the assistant provider's own terms.
- The operator decides what the assistant may do. Actions that change PMS data are available but switched off by
  default; an administrator turns them on per connection with a clear warning. The operator is responsible for the
  assistant's actions and for complying with laws that apply to their guest data.
- The software is provided as is, without warranty, under the Elastic License 2.0.

## Consequences
- The README, user guide and admin guide state this plainly, including where data goes.
- Features that would need a central service (usage statistics, remote support access) are out of scope.
