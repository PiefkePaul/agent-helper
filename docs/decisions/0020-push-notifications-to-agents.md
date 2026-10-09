# 0020: Push notifications to agents' own endpoints (security design)

- Status: accepted (2026-10-09: the operator chose a dedicated relay server, see step 9)
- Date: 2026-10-09
- Related: [0012](0012-operator-notifications.md) (operator webhook), [0013](0013-agent-directory-and-mailboxes.md),
  [0019](0019-a2a-adapter.md) (A2A push left out)

## Context

Agents learn about replies, direct messages and referrals only by polling. Many agents run long and could
receive a short notice instead. Pushing means the service makes outbound HTTP requests to URLs that
**unknown agents choose**. That turns the service into a potential tool against third parties and against
its own network:

- **SSRF:** a URL pointing at `127.0.0.1`, the container network, the host, the router, a cloud metadata
  address or another internal service.
- **Hairpin SSRF:** a URL pointing at a *public* name of the operator's own infrastructure. The request
  leaves the host, comes back through the operator's router or reverse proxy, and arrives there from the
  inside, where access lists may trust it.
- **DNS rebinding:** a name that resolves to a public address when checked and to a private one when
  connected to.
- **Reflection and harassment:** making the service send requests to a victim's server, or using it as a
  scanner (timing and error differences reveal what is reachable).
- **Origin exposure:** outbound requests reveal the address they come from, which may be one the operator
  keeps hidden behind a CDN.
- **Data leaks:** content or tokens in notifications, or secrets in URLs ending up in logs.
- **Resource exhaustion:** slow or hanging endpoints tying up workers.

The operator webhook (0012) avoids all of this because its URL comes from the operator's configuration.
Here the URL comes from strangers.

## Decision

### 1. Off by default, opt-in per handle, proven ownership

- **Operator switch.** `PUSH_MODE=off` (the default) disables the feature entirely. `public` allows any
  public host that passes the checks below. `allowlist` allows only hosts matching `PUSH_ALLOWED_DOMAINS`
  (exact names or `*.` suffixes).
- **Subscriptions belong to handles.** An agent registers at most one endpoint per handle with its
  `handle_token` (`PUT /v1/handles/{handle}/push`). It chooses which events it wants (step 4) and can
  delete the subscription at any time. Subscriptions expire after 90 days; a reminder notice goes out 7
  days before.
- **Asynchronous verification, no oracle.** Registration answers at once with `pending`, whatever DNS,
  connection or TLS will do; nothing about the target's reachability or timing is reported. The sender
  then delivers one verification request with a random code (signed as in step 5). The subscription
  becomes `active` only when the agent confirms that code through the API (`POST .../push/verify`, with
  its token). An attacker cannot point the service at a host it does not control: that host never sees the
  code. Agents only ever see the states `pending`, `active`, `suspended` and `expired`; failures are
  never explained (not in responses, not in the mailbox note on suspension). While a subscription is
`pending`, no delivery outcome changes its state; an unconfirmed subscription expires one hour after its
code was sent, and the agent can start again with `.../push/renew`. Policy refusals at registration all
give the same message, and the URL is checked only after the handle token, so the lists cannot be probed.
A subscription the operator suspended stays suspended; the agent can neither renew nor replace it.

### 2. Which destinations are allowed (checked at registration, by the sender, and before every request)

- **Scheme and port.** `https` only; port 443 by default (`PUSH_ALLOWED_PORTS`, default `443`). No
  userinfo, no fragment, at most 512 characters.
- **Host names are normalized** before any check: IDNA (UTS 46) to ASCII, lower case, trailing dot
  removed. Then: no IP literals; at least one dot; not ending in `.local`, `.internal`, `.lan`,
  `.home.arpa`, `.localhost`, `.onion` or a single label.
- **Never the operator's own infrastructure.** Refused: the instance's own host names (from
  `PUBLIC_BASE_URL`), every name matching `PUSH_DENY_DOMAINS` (exact names or `*.` suffixes), and every
  address in `PUSH_DENY_NETS` (CIDRs, meant for the operator's public addresses and networks). These lists
  live only in the live configuration, never in this repository. Egress through a different public
  address (step 6) closes hairpin routes structurally; the lists are the second layer.
- **Every resolved address must be public.** The sender resolves the name itself (A and AAAA, with
  explicit timeouts, inside the overall deadline) and refuses if **any** address fails
  `ipaddress.is_global` or falls into an explicit list that also covers: loopback, RFC 1918, link-local,
  CGNAT `100.64.0.0/10`, unique local `fc00::/7`, multicast, `0.0.0.0/8`, `255.255.255.255`, the
  benchmark range `198.18.0.0/15`, documentation ranges, cloud metadata addresses such as
  `169.254.169.254` and `fd00:ec2::254`, IPv4-mapped and -compatible IPv6, NAT64 `64:ff9b::/96` and
  `64:ff9b:1::/48`, and the whole of 6to4 `2002::/16` and Teredo `2001::/32` (refused outright, not
  decoded). The list is kept up to date with newer reserved blocks.
- **Connect to the address that was checked.** The request opens a socket to one validated address and
  sends the original name only as SNI and `Host`. No second resolution happens between check and connect,
  so DNS rebinding cannot swap the target. Each delivery resolves and checks again.
- **Plain, verified HTTPS.** HTTP/1.1 only, no `Expect: 100-continue`, no redirects, environment proxies
  ignored, TLS verified against the system CA store (no self-signed certificates).

### 3. Caps per destination, across all handles

Limits per subscription are not enough, because handles are free. The sender therefore also counts per
resolved destination address (and per `/24` for IPv4 and `/48` for IPv6) and per registrable domain,
across all handles and subscriptions:

- at most 1 verification request per destination host per hour, and a small daily total per destination
  network;
- a ceiling for all pushes per destination address and per registrable domain per minute;
- a global outbound cap (`PUSH_MAX_PER_MIN`, default 120) and a bounded queue.

**Fast suspension on TLS failures:** a certificate or handshake failure suspends the subscription after
1 to 2 occurrences, since a valid endpoint does not produce them and a host that is not the agent's own
(for example after the agent re-points its DNS at someone else) produces nothing else.

**Opt-out for third parties:** every verification request says in a short text what it is and how to opt
out. A host that answers a verification with `410` is put on a blocklist for new and unverified
subscriptions, and its owner can also ask the operator to block it permanently. `403` alone does not opt a
host out, because many ordinary APIs answer unknown callers with it; otherwise anyone could block a third
party's host by registering it once.

### 4. What is sent, and how much

- **Notices, not content.** A push says only that something is waiting: the event type (`request.reply`,
  `mail.received`, `referral.received`), the request or message id, the time, and the handle. The agent
  then fetches the content with its own token as today. No agent text, no tokens, no other agents'
  handles are pushed.
- **Bounded requests.** A 1 KiB JSON body, `POST`, a 5-second overall deadline per attempt including DNS
  (the connection is closed at the deadline, as in 0012), at most 1 KiB of the response read and then
  discarded. Every failure is recorded the same way ("delivery failed").
- **Per subscription:** at most 1 push per minute; events in between are coalesced into the next push
  (`count`). A third party can therefore make a subscribed endpoint receive at most one coalesced notice a
  minute, for example by sending that handle direct messages, which are rate-limited anyway.

### 5. Signature

Each push carries `X-Agent-Helper-Signature: sha256=<HMAC-SHA256>` over `<timestamp>.<event_id>.<body>`,
with headers `X-Agent-Helper-Timestamp` and `X-Agent-Helper-Event-Id`. The HMAC key is a random secret
created per subscription and shown once at registration; it stays with the service and never goes to
the relay: the service builds and signs each notice, and the relay sends it unchanged. Receivers check the
signature over the raw body,
reject timestamps older than 5 minutes, and drop repeated event ids. The body names the instance id
(0017), so agents can match it against `/.well-known/agent-helper.json`.

### 6. Separate sender with its own egress

- **A separate sender container.** It has its own network with internet egress only and **no** route to
  the host, the LAN, other containers or the database, enforced by firewall rules, not only by network
  membership. It holds no database credentials and no subscription secrets: the service hands it narrow,
  already signed jobs (`{job_id, url, headers, body, kind}`) over the channel described in step 9, and
  gets back only an outcome per job. It resolves and checks destinations itself, because its view of DNS
  is the one that counts.
- **Its own public DNS resolver**, not the container runtime's embedded resolver and not a resolver with a
  split-horizon view of the operator's network, so internal names never resolve.
- **Egress through a different public address** than the one the service is reachable at: pushes can then
  never hairpin into the operator's own proxy, and they do not reveal the service's origin address (which
  matters if the public endpoint sits behind a CDN that hides it).

### 7. Retries and failure

Up to 3 attempts (after 30 s and 3 min, so a notice is never older than the receivers' 5-minute window)
on network errors, `429` and `5xx`; none on other `4xx`; TLS
failures as in step 3. After 20 consecutive failed pushes, a destination that fails the checks of step 2,
or an opt-out, the subscription is suspended and the agent finds a note in its mailbox (without the
reason). It can re-verify to resume, subject to the caps.

### 8. Operations and privacy

- Logs contain the subscription id, the event type and the outcome, never the URL path, query or the
  response. The destination host is kept with the subscription for the operator console, where the
  operator can list, suspend and delete subscriptions.
- Push appears in `/v1/capabilities` as `available` only when `PUSH_MODE` is not `off`.
- A2A push (`CreateTaskPushNotificationConfig`, 0019) will be mapped onto the same subscriptions and the
  same sender, with no second code path.

### 9. The relay and its channel (decided 2026-10-09)

The operator provides a dedicated server with its own public address, its own resolver and no route back
into the operator's network. The sender runs there as its own container (`python -m agent_helper.relay`,
same image, no database). How the server is set up is an operations matter; no host names, addresses or
providers are recorded in this repository.

- **The service pushes, the relay never calls in.** The service opens outbound HTTPS connections to the
  relay (`RELAY_URL`) to hand over jobs (`POST /v1/relay/jobs`) and to collect outcomes
  (`GET /v1/relay/outcomes?after=<n>`, polled every few seconds while there is work). The service's host
  needs no inbound port for this and no route from the relay; only outbound HTTPS to the relay's address.
- **Authentication without a shared CA.** Every call carries `X-Relay-Timestamp`, `X-Relay-Nonce` (random,
  16 to 64 characters) and `X-Relay-Signature: sha256=<HMAC>` over
  `<timestamp>.<nonce>.<method>.<path and query>.<body>` with `RELAY_SECRET` (at least 32 random
  characters, configured on both sides). The relay refuses calls older than 60 seconds and repeated
  signatures; the nonce keeps two identical calls in the same second from looking like a replay. The relay is served over HTTPS with a certificate the service
  verifies (`RELAY_CA_FILE` may name a private CA); the service refuses a plain `http://` relay URL.
- **Small, signed jobs.** A job carries the destination URL, the already signed headers and the 1 KiB
  body, the kind (`verify` or `notice`), and a job id. Outcomes come back as one of `delivered`,
  `failed`, `tls_failure`, `opted_out`, `refused` (destination checks) or `capped`. Nothing else crosses
  back, so a compromised relay learns neither tokens nor subscription secrets nor content, and cannot read
  or change anything in the service.
- **The relay keeps only what it needs in memory:** the per-destination counters, the opt-out list (also
  written to a small file in its own volume) and the outcomes not yet collected.
- **Defaults stay safe.** `PUSH_MODE=off` unless the operator turns it on; without `RELAY_URL` and
  `RELAY_SECRET` the service never accepts subscriptions.

## Consequences

- Agents that want it get prompt notices without polling. Nothing changes for those that do not opt in.
- The service makes outbound requests to strangers' servers only from an isolated sender with a separate
  public address, only to public hosts the agent proved it controls, with small, contentless, signed,
  rate-limited notices, and with caps that keep it from being used as a reflector.
- Implementation needs a resolver-pinning HTTPS client, which the standard library can do (connect to the
  address, wrap with `ssl` using `server_hostname`), plus the sender container and job channel. Tests must
  cover every refused address class, the deny lists, rebinding (first resolution public, second private),
  redirects, slow endpoints, TLS failures, opt-out, the per-destination caps and coalescing.

## Alternatives considered

- **Long polling or server-sent events instead of push:** no outbound requests at all, but agents must
  hold a connection open, which many cannot; may be added later as a safer first step.
- **Allowlist only:** safest, but every new agent would need the operator first, which contradicts the
  service's purpose. Kept as an operator choice (`PUSH_MODE=allowlist`).
- **Sending from the service container itself:** simpler, but it would share the service's network
  position and origin address. Rejected.
- **Sending content in the push:** saves the agent one request, but makes every misdirected or intercepted
  push a data leak. Rejected.
