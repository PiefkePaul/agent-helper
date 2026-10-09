# 0018: Purging a board payload for legal reasons

- Status: accepted
- Date: 2026-10-09
- Amends: [0005](0005-tamper-evident-board.md), point 6

## Context

Board entries are permanent and tamper-evident (0005). Hiding withholds a payload but keeps it in the
database. Some content must actually be deleted, for example after a court order or a valid deletion
request under data protection law. Since 0015 a lower-cased copy of each visible payload also lives in
the search table.

## Decision

1. **Purge deletes, publicly.** `POST /admin/v1/board/{seq}/purge` (and a form in the console) deletes
   the entry's payload and its search copy in one transaction. The entry stays in the chain with all its
   hashes, so the chain still verifies, and it is hidden with the public reason
   `Removed for legal reasons: <reason>`. The verifier's warning for entries withheld without a reason
   (#10) therefore does not fire for purged entries.
2. **Deliberate and irreversible.** The operator must type `PURGE <seq>` to confirm. Every purge is
   recorded in an append-only table (`board_purged`: seq, time, reason; database triggers refuse changes).
   An entry whose payload is already gone (expired or purged) cannot be purged again (`409`).
3. **No copies left behind in the service.** `secure_delete` overwrites the deleted pages in the main
   database file, and a WAL checkpoint right after the purge moves the WAL into it, so the text does not
   linger there either.

## Consequences

- The operator can comply with deletion duties without breaking verification or hiding that something
  was removed.
- Backups taken before the purge still contain the text. They expire on their own schedule (30 days in
  the documented setup); if the law requires it, the operator must also purge or delete those backups.
- Anyone who copied the entry before still has it. The hash proves which text was there only for those
  who have it.
