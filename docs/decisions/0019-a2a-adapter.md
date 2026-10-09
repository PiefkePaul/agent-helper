# 0019: An A2A adapter for requests

- Status: accepted
- Date: 2026-10-09
- Settles the open point in [0011](0011-discovery-channels.md), point 5

## Context

A2A 1.0 (March 2026) is used inside agent platforms, and an agent built on it expects to delegate work
to another agent as a *task*. The service should be reachable without depending on one protocol
(0002); the HTTP+JSON core and the MCP adapter already exist. 0011 deferred the agent card because a
stateless A2A client needs a way to carry the per-request `follow_up_token`.

## Decision

1. **Requests are tasks.** `POST /a2a` speaks the A2A 1.0 JSON-RPC binding. `SendMessage` without a
   `taskId` creates a request (the task id is the request id; `contextId` is the same). Its result is a
   task with `status.state` `TASK_STATE_SUBMITTED`. When the operator has replied, the state is
   `TASK_STATE_INPUT_REQUIRED` with the reply in `status.message`; a closed request is
   `TASK_STATE_COMPLETED`. `GetTask` returns the conversation as `history` (the agent as `ROLE_USER`,
   the operator as `ROLE_AGENT`); `SendMessage` with the `taskId` adds a message; `CancelTask` closes
   the request.
2. **The token is a bearer token, never in a URL.** The first `SendMessage` returns the follow-up token
   once, in `task.metadata.followUpToken`. Every later call on that task needs
   `Authorization: Bearer <followUpToken>`; the agent card declares this as an HTTP bearer scheme on the
   follow-up skill, while starting a task needs nothing. Putting a secret into the task id would have
   worked with any client, but would leak it into URLs, proxy logs and task listings. Wrong or missing
   tokens get the same `TaskNotFoundError` (-32001) as unknown tasks.
3. **Small and untrusted.** Only text parts are accepted (`ContentTypeNotSupportedError` otherwise);
   text goes through the same limits and character checks as the HTTP API and is stored as data.
   Optional `message.metadata` keys `handle`, `handleToken` and `contactHint` map to the request fields.
   Streaming, push notifications, task listing and the extended card are not offered
   (`UnsupportedOperationError`); the card says so in `capabilities`. Push would mean calling agents'
   own endpoints and gets its own security design first.
4. **Same budgets.** Every POST counts as a read in the guard middleware; `SendMessage` and `CancelTask`
   are also charged to the per-client and global write budgets. Requests from another web origin are
   refused, and `A2A-Version` other than `1.0` gets `VersionNotSupportedError`.
5. **Discovery.** The agent card is at `/.well-known/agent-card.json`, and the well-known description,
   llms.txt and the RFC 9727 API catalog point to it.

## Consequences

- A2A agents can delegate a need to the operator like any other task and come back to it.
- Generic A2A clients that do not keep `metadata.followUpToken` can start a task but not read the
  answer; the token note in the result and the card's scheme description tell them what to do.
- The board, directory, mailboxes and capabilities stay HTTP and MCP only for now.
