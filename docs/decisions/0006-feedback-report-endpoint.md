# 0006: Feedback reports via a quarantine endpoint

- Status: accepted
- Date: 2026-10-08

## Context

[0001](0001-agent-report-quarantine.md) decided that agent reports pass a quarantine queue. This record
fixes the v0.1 interface.

## Decision

1. `POST /v1/reports` accepts `kind` (`bug`, `feature`, `capability`, `security`, `other`) and free text.
   Reports are stored with status `quarantined` and get a follow-up token, as requests do.
2. The agent reads the status with `GET /v1/reports/{id}` and its token.
3. The operator sets the status to `accepted`, `rejected`, or `duplicate` and may add a note for the
   reporter.
4. v0.1 has **no automatic forwarding** to GitHub, not even after acceptance. Promotion to an issue stays
   a manual step by the operator.

## Consequences

- No agent-authored text can reach the issue tracker without a human decision.
- Filter rules for any future automatic promotion still need their own decision record.
