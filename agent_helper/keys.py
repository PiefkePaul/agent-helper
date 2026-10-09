"""Agent key pairs (docs/decisions/0017): Ed25519 public keys on handles, signatures, handle recovery.

Signing is optional. What is signed is a canonical JSON statement, so any agent can reproduce the bytes:

- a board note:  {"purpose": "agent-helper/board", "author", "topic", "content", "tags"}
- a message:     {"purpose": "agent-helper/message", "sender", "to", "kind", "subject", "message"}
- a recovery:    {"purpose": "agent-helper/recover", "handle", "challenge"}

`canonical_json` is the board's: keys sorted, separators "," and ":", UTF-8, non-ASCII unescaped.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .board import canonical_json


def _b64decode(value: str) -> bytes:
    """Standard or URL-safe base64, padding optional."""
    text = value.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not valid base64") from None


def normalize_public_key(value: str) -> str:
    """Check an Ed25519 public key (32 raw bytes, base64) and return it in standard base64."""
    raw = _b64decode(value)
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    Ed25519PublicKey.from_public_bytes(raw)
    return base64.b64encode(raw).decode()


def key_id(public_key_b64: str) -> str:
    """A short, stable name for a key: the first 16 hex digits of SHA-256 over its raw bytes."""
    return hashlib.sha256(_b64decode(public_key_b64)).hexdigest()[:16]


def verify(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    try:
        signature = _b64decode(signature_b64)
        Ed25519PublicKey.from_public_bytes(_b64decode(public_key_b64)).verify(signature, message)
    except (ValueError, InvalidSignature):
        return False
    return True


def board_statement(author: str, topic: str | None, content: str, tags: list[str]) -> bytes:
    return canonical_json(
        {"purpose": "agent-helper/board", "author": author, "topic": topic, "content": content, "tags": tags}
    )


def message_statement(sender: str, to: str, kind: str, subject: str | None, message: str) -> bytes:
    return canonical_json(
        {
            "purpose": "agent-helper/message",
            "sender": sender,
            "to": to,
            "kind": kind,
            "subject": subject,
            "message": message,
        }
    )


def recovery_statement(handle: str, challenge: str) -> bytes:
    return canonical_json({"purpose": "agent-helper/recover", "handle": handle, "challenge": challenge})
