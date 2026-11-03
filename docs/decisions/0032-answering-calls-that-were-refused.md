# 0032: Answering calls that were refused

- Status: accepted
- Date: 2026-11-03
- Builds on [0011](0011-discovery-channels.md), [0019](0019-a2a-adapter.md),
  [0024](0024-a2a-0-3-and-common-discovery-files.md), [0025](0025-refused-calls-and-daily-summary.md),
  [0028](0028-more-entry-points-agents-try.md)

## Context

A month of usage counts and two weeks of proxy logs showed which calls still end in an error or a 404:

- MCP `server/discover` refused with "unsupported protocol version" (about 170, plus about 50 header
  mismatches). The refusal listed `2025-11-25` and the other legacy versions as supported, yet a request
  that named one of them in `_meta` was refused, because only `2026-07-28` was accepted there. A client
  that retried with a version from the list was refused again.
- `GET /mcp` with `Accept: text/event-stream` answered 405 (about 1,800 in two weeks, mostly checkers).
  405 is allowed, but many checkers rate it as a broken server.
- A2A `GetTask`, `tasks/get` and `CancelTask` answered "task not found" (about 850). The message did not
  say whether the token was missing or the id was not one of ours.
- A2A requests with a 1.0 method name but no `A2A-Version` header, or with a patch version such as
  `1.0.0`, were refused as an unsupported version (about 190).
- 404s for `/agent-directory.json`, `/.well-known/payment-manifest`, `/.well-known/mpp`, and for help pages
  such as `/docs` and `/about`.

## Decision

1. **MCP versions.** A version in `_meta` is accepted when it is any version this server lists as
   supported. A legacy version there is served with legacy semantics (no `Mcp-Method` header needed, `ping`
   answered, errors with status 200); its `MCP-Protocol-Version` header must match when present. Unknown
   versions are still refused with -32022 as the 2026-07-28 revision requires, and counted as
   `mcp:version-refused:newer|older|between|other` (never the value itself), so the next evaluation
   shows what clients send. A header mismatch now names the expected headers in `error.data.expected`.
   A legacy notification sent with an id by mistake (seen for `notifications/initialized`) gets an empty
   result.
2. **GET stream.** A legacy client that opens the GET event stream gets a valid one: a comment saying that
   no server-initiated messages are sent, `retry: 3600000` (do not reconnect for an hour), and the end of
   the stream. The legacy revisions allow a server to close the stream at any time, and nothing is held
   open. A request that carries `MCP-Protocol-Version: 2026-07-28` still gets 405, as that revision has no
   GET stream. The old HTTP+SSE transport (an `endpoint` event, answers over the stream) is not offered:
   POST answers come in the response, so such a client would wait in vain.
3. **A2A dialect.** The method names of 0.3 (`message/send`) and 1.0 (`SendMessage`) never overlap, so the
   method name decides how a request is served and how the answer is shaped; this replaces point 2 of 0024
   ("the versions do not mix"). The header only refuses versions not served at all: accepted are none,
   `0.2`, `0.3` (each with an optional patch number) and `1.0` (also with a patch number). Requests whose
   header and method name disagree are counted as `a2a:dialect:mixed`. The HTTP+JSON binding has no method name;
   there a message without the header but with the 1.0 role `ROLE_USER` is served as 1.0.
4. **A2A task lookups.** The follow-up token may also come in the body, as `params.metadata.followUpToken`
   or, on `SendMessage`, `message.metadata.followUpToken`, for clients that cannot set an `Authorization`
   header. It still never goes into a URL. "Task not found" now says what is missing: the token, or an id
   that is not one of ours (ids here start with `req_`; message or context ids chosen by the client are
   not task ids). The hint depends only on what the caller sent, never on whether a task exists. The three
   cases are counted as `a2a:task-not-found:no-token|foreign-id|wrong-token`.
5. **Small files and pages.** `/agent-directory.json` serves the same index as the `.well-known` path;
   `/.well-known/payment-manifest`, `/.well-known/mpp` and `/pricing` say the service is free. `/docs`,
   `/documentation`, `/about`, `/help`, `/faq`, `/features`, `/support` and `/contact` redirect (307) to the
   front page, which explains everything to people and agents alike.

Deliberately unchanged: OAuth discovery stays 404 (0028), and `/.well-known/glama.json` stays off unless
the operator configures it.

## Consequences

- Clients that name a version this server already supports, or a 1.0 method without the header, now get
  answers instead of errors; the refusals that remain say what to change.
- Accepting the token in the body widens where a secret may appear in a request. Bodies are not logged
  and the token is checked exactly as before, so this does not weaken access, but clients should prefer
  the header.
- New usage counts show which refusals remain and why, without recording any value a client sent.
