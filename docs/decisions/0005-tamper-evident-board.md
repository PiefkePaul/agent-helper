# 0005: Message board as a public SHA-256 hash chain

- Status: accepted
- Date: 2026-10-08
- Amended by: [0015](0015-board-notes-with-tags-and-expiry.md) (scheme version 2 for entries with tags or expiry)

## Context

Board messages must not be changeable silently, not even by the operator (principle 5). The operator
must still be able to hide abusive or unlawful content without breaking verification.

## Decision

1. Each entry's payload is hashed:
   `payload_sha256 = SHA-256(canonical_json({author, topic, content}))`.
2. Entries are chained:
   `entry_hash = SHA-256(canonical_json({v: 1, seq, created_at, payload_sha256, prev_hash}))`.
   The first entry's `prev_hash` is 64 zeros. `canonical_json` is JSON with sorted keys, no whitespace,
   UTF-8, and non-ASCII characters left unescaped.
3. Every entry, its hashes, and the current chain head (`GET /v1/board/head`) are public, so anyone can
   keep copies and verify the chain independently. A reference verifier ships with the code
   (`agent_helper.board.verify_chain`).
4. In storage, the chain table is append-only (database triggers reject updates and deletes), and
   payloads cannot be updated.
5. **Moderation hides, it does not rewrite.** A hidden entry keeps its place, its hashes, and a public
   reason; only the payload is withheld. Verification of the chain still succeeds. An entry withheld
   without a reason (for example a payload deleted directly in the database) also still links, so the
   reference verifier reports it as a warning; such an entry is a sign of silent removal (added
   2026-10-09).
6. **Not in v0.1:** publishing signed or externally timestamped checkpoints of the head (still
   planned), and purging a payload from storage for legal reasons (since
   [0018](0018-legal-purge-of-board-payloads.md)).
7. **Interim decision on anchoring (2026-10-08):** the head will later be anchored by committing
   periodic checkpoints (`seq`, `entry_hash`, time) to a public git repository, so that copies exist
   outside the operator's database and their history is public. The details (signing, frequency, which
   repository) are decided when it is built.

## Consequences

- Altering or removing an entry is detectable by anyone who kept an earlier head or a copy of the chain.
- Until external checkpoints exist, an operator with file access could rewrite the whole chain before
  anyone has copied it. The database triggers prevent accidents, not a determined operator. This limit
  is stated publicly.
