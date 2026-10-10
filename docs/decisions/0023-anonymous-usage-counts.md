# 0023: Anonymous usage counts, and an IndexNow key file

- Status: accepted
- Date: 2026-10-10
- Builds on [0008](0008-safety-limits-v0-1.md) and [0011](0011-discovery-channels.md)

## Context

The discovery files from 0011 are served, but the operator cannot tell whether anyone, and which kind of
client, ever reads them. The request log (0008) is per request and kept only as long as the host keeps
container logs; it is not meant for counting, and it must not grow into a record of who came by.
Separately, the search engines that support IndexNow accept a list of changed URLs only from a site that
proves ownership with a key file at its root.

## Decision

1. **Only daily totals are kept.** A row is `(day, metric, family, count)`. The metric is the kind of
   request: an endpoint group (`llms.txt`, `mcp:post`, `v1:requests:post`, `landing:html`), an MCP tool
   (`mcp:tool:<name>`, only for tools this service has), an A2A method, a refused A2A version, or a probe
   for a well-known file this service does not have (`probe:ai-plugin.json`; any other 404 is
   `not_found`). Every part of a metric comes from a fixed set: a path segment under `/v1/` only when the
   service has a route there (else `v1:other`), an HTTP method only when it is an ordinary one (else
   `other`). Callers cannot invent metrics. Operator pages and `/healthz` are not counted.
2. **Clients are reduced to a family.** The User-Agent header is classified into one of `ai_agent`,
   `ai_crawler`, `search_crawler`, `monitor`, `http_library`, `browser`, `other_bot` or `none`, and then
   dropped. No address, no full User-Agent, no time of day, no request content and nothing that links two
   requests is stored.
3. **Self-reported MCP client names** (`clientInfo.name`) are counted as `mcp:client` only when they start
   with a known client name (a fixed list in `usage.py`, such as `claude-desktop` or `cursor`), and then
   only as that name; everything else counts as `other`. A free-form name could carry an agent's own handle
   and leave a trace of it per day, and random names would grow the table. The list is extended by pull
   request when an `other` count suggests a common client is missing.
4. **Counts are held in memory and added to the database every 5 minutes** and at shutdown. Rows older
   than `USAGE_RETENTION_DAYS` (default 400) are deleted. `USAGE_STATS=false` turns counting off.
   The operator reads them at `GET /admin/v1/usage?days=N` and on the console page "Usage".
5. **The numbers are tendencies, not proof.** Every input is self-reported by the client and trivially
   faked. A rising `ai_agent` count on `/llms.txt` suggests agents fetch it; it does not prove that a
   particular product did.
6. **IndexNow.** When `INDEXNOW_KEY` is set (8 to 128 characters of `a-z A-Z 0-9 -`, checked at start),
   the key is served as plain text at `/<key>.txt`. The key is public by design. Submitting URLs is a
   manual operator step (see operations), not something the service does on its own.

## Consequences

- The operator can see whether discovery works (crawlers, agent fetchers, probes for files we lack) without
  keeping personal data. A small set of counts per day stays small.
- The family list needs maintenance as new crawlers appear; unknown bots land in `other_bot`.
- Probe counts show which conventions visitors expect; they are a hint for future discovery files, not a
  commitment to add them.
- Counts lost in a crash are at most the last 5 minutes.
