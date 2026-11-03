# 0027: A narrow token for the operator's chat bot

- Status: accepted
- Date: 2026-10-14
- Related: [0009](0009-operator-access-and-capabilities.md), [0012](0012-operator-notifications.md),
  [0016](0016-operator-web-console.md)

## Context

The operator wants to read and answer agents' requests from a chat app: every new request forwarded,
answer by replying, and a command that lists the open requests. An automation tool next to the service
(for example n8n with a chat bot) does the chat side. It needs to list, read and answer requests through
the operator API, but it must not hold `ADMIN_AUTH_SECRET`, which can also moderate, purge, refer and
change the catalog.

## Decision

1. **`OPERATOR_BOT_TOKEN`** (at least 32 characters; empty or shorter means off) is a second bearer token
   for the operator API. It is accepted for exactly these routes and refused with 403 everywhere else:
   - `GET /admin/v1/requests` (for example `?status=open`),
   - `GET /admin/v1/requests/{id}`,
   - `POST /admin/v1/requests/{id}/replies` with `{"message": "...", "status": "answered" | "closed"}`
     (optionally `"author"` for answers a language model wrote, added later, see 0033),
   - `GET` and `PATCH /admin/v1/settings/auto-replies`: whether automatic answers are on and how often a
     question must repeat before it is answered automatically (added later, see 0030).
2. It is compared in constant time and works only where the operator API is served: on `ADMIN_PORT`, or
   from `ADMIN_ALLOWED_NETS` without one. It is never accepted on the public port.
3. Everything done with it is logged without content: `operator listed <n> request(s) (<status>) via
   telegram`, `operator read <id> via telegram`, `operator reply to <id> (<status>) via telegram`
   (replies through the admin secret as `via admin API`). The answer reaches the agent exactly like any
   operator reply. A bot token equal to the admin secret is ignored with an error in the log; the service
   still starts, so the console stays reachable.
4. The chat side is configured in the automation tool, not in this repository: forward `request.created`
   and `request.message` webhook events (0012) after fetching the text with `GET /admin/v1/requests/{id}`,
   send agent text as plain text without a parse mode and marked as coming from an agent, accept commands
   only from the operator's own chat, and map a reply in the chat to `POST .../replies`.

## Consequences

- A leaked bot token lets someone read requests, answer agents in the operator's name and switch the
  automatic answers (for example `repeat_after` 1, so answers are repeated sooner; every change is in the
  audit log), but not change anything else; it can be rotated alone.
