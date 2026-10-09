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
    # Things that do not break the chain but deserve a look, e.g. an entry withheld without a public reason.
    warnings: tuple[str, ...] = ()


def _valid_expiry(e: Mapping[str, Any], now: str) -> bool:
    """An entry may only claim to be expired if its scheme has an expiry, and the expiry lies after its
    creation and before `now`. Version 1 entries cannot expire: a missing v1 payload must show as hidden."""
    expires_at = e.get("expires_at")
    return e.get("v", 1) >= 2 and isinstance(expires_at, str) and e["created_at"] < expires_at <= now


def verify_chain(entries: Iterable[Mapping[str, Any]], now: str | None = None) -> VerifyResult:
    """Verify entries as returned by GET /v1/board, in ascending seq order, starting at seq 1.

    Hidden and expired entries carry no payload; for them only the chain links are checked. A hidden entry
    without a `hidden_reason` still verifies but is reported in `warnings`: moderation is meant to be public
    (docs/decisions/0005), so a payload that is gone without a reason may have been removed silently. An entry
    marked expired must be version 2 with an expiry between its creation and `now` (default: the current
    UTC time); its `expires_at` is part of the version 2 entry hash, so it cannot be changed.
    """
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prev = GENESIS_HASH
    expected_seq = 1
    checked = 0
    warnings: list[str] = []
    for e in entries:
        seq = e["seq"]
        if seq != expected_seq:
            return VerifyResult(False, checked, prev, f"expected seq {expected_seq}, got {seq}", seq)
        if e["prev_hash"] != prev:
            return VerifyResult(False, checked, prev, "prev_hash does not match previous entry", seq)
        v = e.get("v", 1)
        if e.get("hidden") and not (e.get("hidden_reason") or "").strip():
            warnings.append(f"entry {seq} is withheld without a public reason")
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
    return VerifyResult(True, checked, prev, warnings=tuple(warnings))


def verify_checkpoints(
    entries: Iterable[Mapping[str, Any]],
    checkpoints: Iterable[Mapping[str, Any]],
    instance: str,
    public_key: str,
    other_keys: Mapping[str, Mapping[str, str]] | None = None,
) -> list[str]:
    """Errors in signed checkpoints (docs/decisions/0022): a bad signature, or a checkpoint whose
    entry_hash differs from the chain's entry at that seq (the chain was rewritten after it was signed).
    `entries` as returned by GET /v1/board; an empty list means every checkpoint holds. `other_keys`
    (key id -> {public_key, status, since}) are earlier keys of this instance: a checkpoint made with a
    "rotated" one is checked with it; one made with a "revoked" key is ignored, never an error (and never
    proof); `checkpoint_notes` reports both."""
    return _check_checkpoints(entries, checkpoints, instance, public_key, other_keys)[0]


def checkpoint_notes(
    entries: Iterable[Mapping[str, Any]],
    checkpoints: Iterable[Mapping[str, Any]],
    instance: str,
    public_key: str,
    other_keys: Mapping[str, Mapping[str, str]] | None = None,
) -> list[str]:
    """Things worth a look that are not errors: checkpoints made with a rotated key, and checkpoint times
    that go backwards (a clock that had jumped ahead)."""
    return _check_checkpoints(entries, checkpoints, instance, public_key, other_keys)[1]


def _check_checkpoints(
    entries: Iterable[Mapping[str, Any]],
    checkpoints: Iterable[Mapping[str, Any]],
    instance: str,
    public_key: str,
    other_keys: Mapping[str, Mapping[str, str]] | None,
) -> tuple[list[str], list[str]]:
    from . import keys  # keys imports this module

    hashes = {e["seq"]: e["entry_hash"] for e in entries}
    current_id = keys.key_id(public_key)
    errors: list[str] = []
    notes: list[str] = []
    last_time = None
    for cp in sorted(checkpoints, key=lambda c: c["seq"]):
        statement = keys.checkpoint_statement(instance, cp["seq"], cp["entry_hash"], cp["time"])
        key = public_key
        earlier = (other_keys or {}).get(cp.get("key_id", "")) if cp.get("key_id") != current_id else None
        if earlier is not None and earlier.get("status") == "revoked":
            notes.append(f"checkpoint #{cp['seq']}: signed with a revoked key ({cp['key_id']}), ignored")
            continue
        if earlier is not None:
            key = earlier["public_key"]
            notes.append(f"checkpoint #{cp['seq']}: made with another key ({cp['key_id']}, rotated)")
        if not keys.verify(key, cp["signature"], statement):
            errors.append(f"checkpoint #{cp['seq']}: signature does not verify")
        elif cp["seq"] not in hashes:
            errors.append(f"checkpoint #{cp['seq']}: the chain has no such entry")
        elif hashes[cp["seq"]] != cp["entry_hash"]:
            errors.append(f"checkpoint #{cp['seq']}: the chain differs from what was signed")
        if last_time is not None and cp["time"] < last_time:
            notes.append(f"checkpoint #{cp['seq']}: its time is earlier than the one before (a clock error)")
        last_time = cp["time"] if last_time is None else max(last_time, cp["time"])
    return errors, notes
