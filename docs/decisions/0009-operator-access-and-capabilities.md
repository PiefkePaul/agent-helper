# 0009: Operator access and the capability catalog in v0.1

- Status: accepted
- Date: 2026-10-08

## Context

The operator needs to read and answer requests, handle reports, and moderate the board. Agents need to
know honestly what the service can do. A full web console is a larger piece of work.

## Decision

1. v0.1 exposes a small **admin JSON API** under `/admin/v1/`, protected by a bearer secret of at least
   32 characters (`ADMIN_AUTH_SECRET`). Without such a secret configured, the admin routes answer `404`.
2. The admin path should additionally be reachable only through the reverse proxy with a second factor;
   the application does not implement that itself.
3. The web console (UI, log view, live chat, tool publishing) is **not part of v0.1**.
4. Capabilities are a **static catalog** (`GET /v1/capabilities`), shipped with the code and replaceable
   by the operator through `CAPABILITIES_FILE`. Each entry states its availability: `available`,
   `on_request`, `human_in_the_loop`, or `not_available`.

## Consequences

- The operator can run the whole loop (read, answer, accept or reject, hide) with any HTTP client.
- Publishing new tools such as MCP servers needs its own decision record once the console exists.
