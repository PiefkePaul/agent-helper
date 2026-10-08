"""Hashing scheme of the public message board (see docs/decisions/0005-tamper-evident-board.md).

This module has no dependencies beyond the standard library so it can be copied and used by anyone
who wants to verify the board independently.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEME_VERSION = 1
GENESIS_HASH = "0" * 64


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_hash(author: str | None, topic: str | None, content: str) -> str:
    return sha256_hex(canonical_json({"author": author, "topic": topic, "content": content}))


def entry_hash(seq: int, created_at: str, payload_sha256: str, prev_hash: str) -> str:
    return sha256_hex(
        canonical_json(
            {
                "v": SCHEME_VERSION,
                "seq": seq,
                "created_at": created_at,
                "payload_sha256": payload_sha256,
                "prev_hash": prev_hash,
            }
        )
    )


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    head_hash: str
    error: str | None = None
    failed_seq: int | None = None


def verify_chain(entries: Iterable[Mapping[str, Any]]) -> VerifyResult:
    """Verify entries as returned by GET /v1/board, in ascending seq order, starting at seq 1.

    Hidden entries carry no payload; for them only the chain links are checked.
    """
    prev = GENESIS_HASH
    expected_seq = 1
    checked = 0
    for e in entries:
        seq = e["seq"]
        if seq != expected_seq:
            return VerifyResult(False, checked, prev, f"expected seq {expected_seq}, got {seq}", seq)
        if e["prev_hash"] != prev:
            return VerifyResult(False, checked, prev, "prev_hash does not match previous entry", seq)
        if not e.get("hidden"):
            actual = payload_hash(e.get("author"), e.get("topic"), e["content"])
            if actual != e["payload_sha256"]:
                return VerifyResult(False, checked, prev, "payload does not match payload_sha256", seq)
        actual_entry = entry_hash(seq, e["created_at"], e["payload_sha256"], prev)
        if actual_entry != e["entry_hash"]:
            return VerifyResult(False, checked, prev, "entry_hash does not match", seq)
        prev = actual_entry
        expected_seq += 1
        checked += 1
    return VerifyResult(True, checked, prev)
