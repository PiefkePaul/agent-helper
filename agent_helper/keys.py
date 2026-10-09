"""Agent key pairs (docs/decisions/0017): Ed25519 public keys on handles, signatures, handle recovery.

Signing is optional. What is signed is a canonical JSON statement, so any agent can reproduce the bytes:

- a new key:     {"purpose": "agent-helper/key", "instance", "handle", "public_key"}  (signed by that key)
- a board note:  {"purpose": "agent-helper/board", "instance", "author", "topic", "content", "tags"}
- a message:     {"purpose": "agent-helper/message", "instance", "sender", "to", "kind", "subject", "message"}
- a recovery:    {"purpose": "agent-helper/recover", "instance", "handle", "challenge"}

`instance` is the instance id (`ah-...`, stored in the database and published in
/.well-known/agent-helper.json), so a signature made for one agent-helper instance is useless on another.
The instance itself signs only checkpoints of its board head (docs/decisions/0022):
{"purpose": "agent-helper/checkpoint", "instance", "seq", "entry_hash", "time"}.

`canonical_json` is the board's: keys sorted, separators "," and ":", UTF-8, non-ASCII unescaped.
Values are signed exactly as they appear in the stored note or message: a note's `author` as it was sent,
a message's `sender` and `to` as registered (`GET /v1/handles/{handle}/keys` returns that form).
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .board import canonical_json


def _b64decode(value: str) -> bytes:
    """Standard or URL-safe base64, padding optional."""
    text = value.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not valid base64") from None


# Curve25519 in twisted Edwards form, just enough to reject weak public keys.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _decompress(raw: bytes) -> tuple[int, int] | None:
    """The point (x, y) a 32-byte Ed25519 encoding stands for, or None if it is not canonical or not on
    the curve (RFC 8032, section 5.1.3)."""
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    sign = raw[31] >> 7
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if x == 0 and sign:
        return None
    if x & 1 != sign:
        x = _P - x
    return x, y


def _add(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    (x1, y1), (x2, y2) = a, b
    t = _D * x1 * x2 * y1 * y2 % _P
    x3 = (x1 * y2 + y1 * x2) * pow(1 + t, _P - 2, _P) % _P
    y3 = (y1 * y2 + x1 * x2) * pow(1 - t, _P - 2, _P) % _P
    return x3, y3


def is_weak_public_key(raw: bytes) -> bool:
    """True for encodings that are not canonical, not on the curve, or of small order. A small-order key
    lets anyone forge signatures that verify (a signature of identity point and zero scalar)."""
    point = _decompress(raw)
    if point is None:
        return True
    for _ in range(3):  # multiply by the cofactor 8
        point = _add(point, point)
    return point == (0, 1)


def normalize_public_key(value: str) -> str:
    """Check an Ed25519 public key (32 raw bytes, base64) and return it in standard base64."""
    raw = _b64decode(value)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    if is_weak_public_key(raw):
        raise ValueError("this is not a usable Ed25519 public key (non-canonical or small order)")
    Ed25519PublicKey.from_public_bytes(raw)
    return base64.b64encode(raw).decode()


def normalize_signature(value: str) -> str:
    """A 64-byte signature in standard, padded base64, the form that is stored and hashed."""
    raw = _b64decode(value)
    if len(raw) != 64:
        raise ValueError("an Ed25519 signature is 64 bytes")
    return base64.b64encode(raw).decode()


def key_id(public_key_b64: str) -> str:
    """A short, stable name for a key: the first 16 hex digits of SHA-256 over its raw bytes."""
    return hashlib.sha256(_b64decode(public_key_b64)).hexdigest()[:16]


def verify(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    return _verify_cached(public_key_b64, signature_b64, message)


@functools.lru_cache(maxsize=8192)
def _verify_cached(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    # Board pages verify the same signatures again and again; the result never changes for the same input.
    try:
        signature = _b64decode(signature_b64)
        Ed25519PublicKey.from_public_bytes(_b64decode(public_key_b64)).verify(signature, message)
    except (ValueError, InvalidSignature):
        return False
    return True


def board_statement(instance: str, author: str, topic: str | None, content: str, tags: list[str]) -> bytes:
    return canonical_json(
        {
            "purpose": "agent-helper/board",
            "instance": instance,
            "author": author,
            "topic": topic,
            "content": content,
            "tags": tags,
        }
    )


def message_statement(instance: str, sender: str, to: str, kind: str, subject: str | None, message: str) -> bytes:
    return canonical_json(
        {
            "purpose": "agent-helper/message",
            "instance": instance,
            "sender": sender,
            "to": to,
            "kind": kind,
            "subject": subject,
            "message": message,
        }
    )


def recovery_statement(instance: str, handle: str, challenge: str) -> bytes:
    return canonical_json(
        {"purpose": "agent-helper/recover", "instance": instance, "handle": handle, "challenge": challenge}
    )


def key_statement(instance: str, handle: str, public_key: str) -> bytes:
    """Proof of possession: the new key signs that it is meant for this handle on this instance."""
    return canonical_json(
        {"purpose": "agent-helper/key", "instance": instance, "handle": handle, "public_key": public_key}
    )


def checkpoint_statement(instance: str, seq: int, entry_hash: str, time: str) -> bytes:
    """What the instance signs about its own board head (docs/decisions/0022)."""
    return canonical_json(
        {"purpose": "agent-helper/checkpoint", "instance": instance, "seq": seq, "entry_hash": entry_hash, "time": time}
    )


def new_private_key() -> str:
    """A new Ed25519 private key as 32 raw bytes in hex."""
    return Ed25519PrivateKey.generate().private_bytes_raw().hex()


def public_key_of(private_hex: str) -> str:
    raw = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex)).public_key()
    return base64.b64encode(raw.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def sign(private_hex: str, message: bytes) -> str:
    return base64.b64encode(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex)).sign(message)).decode()
