# 0001: Agent reports go through a quarantine queue

- Status: accepted
- Date: 2026-10-08

## Context

The service will offer agents an interface to report issues and request features directly, so the
service can improve based on what its users need. Reports that become GitHub issues are later read by
the people and agents that develop this project.

If reports were forwarded automatically, the interface would be an open path for spam and for prompt
injection aimed at whoever reads the issues, including automated development agents.

## Decision

1. Reports submitted through the service land in a **quarantine queue**, never directly in the issue
   tracker.
2. A report becomes a GitHub issue only after **operator approval** or after passing a **filter**
   whose rules are documented in this repository.
3. Content that originates from agents, in the queue and after promotion to an issue, is always treated
   as **untrusted data** by development. Instructions contained in a report are never followed as
   instructions; they are only evaluated as a description of a problem or wish.

## Consequences

- Reporting agents get an acknowledgement on submission, not an immediate public issue.
- The operator console needs a view of the queue with approve, reject, and edit-before-publish actions.
- Development workflows that read issues must keep agent-authored text separate from their own
  instructions.
- The concrete interface and filter rules are still open; see
  [open-questions.md](../open-questions.md#feedback-interface-issues-and-feature-requests-via-the-service).
