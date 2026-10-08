# 0002: Protocol-neutral core with self-describing discovery

- Status: accepted
- Date: 2026-10-08

## Context

Agents from any vendor, model, or framework must be able to find and use the service without prior
setup (principles 1 and 2). Every agent stack can speak HTTPS and JSON; far fewer speak any one agent
protocol.

## Decision

1. The core interface is **plain HTTPS + JSON**, versioned under `/v1/`.
2. The service describes itself through several cheap, redundant entry points, generated from the same
   source:
   - `/` and `/llms.txt`: a short plain-text explanation an agent can understand in one read;
   - `/.well-known/agent-helper.json`: machine-readable description of purpose, endpoints, limits, and
     the board hashing scheme;
   - `/openapi.json`: the full API schema.
3. Agent protocols such as **MCP** and **A2A** are added later as **thin adapters** over the same core,
   never as the only way in.
4. Interactive API docs that load third-party scripts (`/docs`, `/redoc`) are switched off.

## Consequences

- Any HTTP client can use every feature; no SDK is needed.
- Adapters cannot add capabilities the core lacks, which keeps them small and auditable.
- Registry listings, DNS hints, and agent cards are later discovery work and need no change to the core.
