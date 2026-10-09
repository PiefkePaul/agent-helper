"""Hashing scheme of the public message board (see docs/decisions/0005-tamper-evident-board.md).

This module has no dependencies beyond the standard library so it can be copied and used by anyone
who wants to verify the board independently.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEME_VERSION = 3  # the newest version; entries keep the version they were written with
GENESIS_HASH = "0" * 64


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_hash(
    author: str | None,
    topic: str | None,
    content: str,
    tags: list[str] | None = None,
    expires_at: str | None = None,
    v: int = 1,
    key_id: str | None = None,
    signature: str | None = None,
) -> str:
    """Version 1 hashes {author, topic, content}; version 2 adds {tags, expires_at} (docs/decisions/0015);
    version 3 adds the author's {key_id, signature} (docs/decisions/0017)."""
    payload: dict[str, Any] = {"author": author, "topic": topic, "content": content}
    if v >= 2:
        payload |= {"tags": tags or [], "expires_at": expires_at}
    if v >= 3:
        payload |= {"key_id": key_id, "signature": signature}
    return sha256_hex(canonical_json(payload))


def entry_hash(
    seq: int, created_at: str, payload_sha256: str, prev_hash: str, v: int = 1, expires_at: str | None = None
) -> str:
    """Version 2 also hashes `expires_at` (null when there is none) into the chain itself, so the expiry stays
    verifiable after the payload is deleted, and an entry without an expiry can never pose as expired."""
    fields: dict[str, Any] = {
        "v": v,
        "seq": seq,
        "created_at": created_at,
        "payload_sha256": payload_sha256,
        "prev_hash": prev_hash,
    }
    if v >= 2:
        fields["expires_at"] = expires_at
    return sha256_hex(canonical_json(fields))


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    head_hash: str
    error: str | None = None
    failed_seq: int | None = None


def _valid_expiry(e: Mapping[str, Any], now: str) -> bool:
    """An entry may only claim to be expired if its scheme has an expiry, and the expiry lies after its
    creation and before `now`. Version 1 entries cannot expire: a missing v1 payload must show as hidden."""
    expires_at = e.get("expires_at")
    return e.get("v", 1) >= 2 and isinstance(expires_at, str) and e["created_at"] < expires_at <= now


def verify_chain(entries: Iterable[Mapping[str, Any]], now: str | None = None) -> VerifyResult:
    """Verify entries as returned by GET /v1/board, in ascending seq order, starting at seq 1.

    Hidden and expired entries carry no payload; for them only the chain links are checked. An entry
    marked expired must be version 2 with an expiry between its creation and `now` (default: the current
    UTC time); its `expires_at` is part of the version 2 entry hash, so it cannot be changed.
    """
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prev = GENESIS_HASH
    expected_seq = 1
    checked = 0
    for e in entries:
        seq = e["seq"]
        if seq != expected_seq:
            return VerifyResult(False, checked, prev, f"expected seq {expected_seq}, got {seq}", seq)
        if e["prev_hash"] != prev:
            return VerifyResult(False, checked, prev, "prev_hash does not match previous entry", seq)
        v = e.get("v", 1)
        if e.get("expired") and not _valid_expiry(e, now):
            return VerifyResult(False, checked, prev, "entry claims an expiry it cannot have", seq)
        if not e.get("hidden") and not e.get("expired"):
            actual = payload_hash(
                e.get("author"),
                e.get("topic"),
                e["content"],
                e.get("tags"),
                e.get("expires_at"),
                v,
                e.get("key_id"),
                e.get("signature"),
            )
            if actual != e["payload_sha256"]:
                return VerifyResult(False, checked, prev, "payload does not match payload_sha256", seq)
        actual_entry = entry_hash(seq, e["created_at"], e["payload_sha256"], prev, v, e.get("expires_at"))
        if actual_entry != e["entry_hash"]:
            return VerifyResult(False, checked, prev, "entry_hash does not match", seq)
        prev = actual_entry
        expected_seq += 1
        checked += 1
    return VerifyResult(True, checked, prev)
