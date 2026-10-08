"""Body size limit, per-client rate limits, and response hardening (docs/decisions/0008)."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("agent_helper.access")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]

READ_METHODS = {"GET", "HEAD", "OPTIONS"}


class TokenBucket:
    """In-memory token bucket per key. Resets on restart; this is a floor, not a defence against botnets."""

    def __init__(self, per_minute: int, max_keys: int = 50_000) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str, now: float | None = None) -> float:
        """Consume one token. Returns 0 if allowed, otherwise seconds until the next token."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                self._prune(now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1 - tokens) / self.rate if self.rate > 0 else 60.0

    def _prune(self, now: float) -> None:
        if len(self._buckets) <= self.max_keys:
            return
        full_after = self.capacity / self.rate if self.rate > 0 else 60.0
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}
        if len(self._buckets) > self.max_keys:
            self._buckets.clear()


def client_key(scope: Scope, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                # The nearest trusted proxy appends the address it saw as the last element.
                last = value.decode("latin-1").split(",")[-1].strip()
                if last:
                    return last
    client = scope.get("client")
    return client[0] if client else "unknown"


async def _send_json(send: Send, status: int, body: dict[str, Any], extra: list[tuple[bytes, bytes]]) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + extra + SECURITY_HEADERS})
    await send({"type": "http.response.body", "body": payload})


class GuardMiddleware:
    """Pure ASGI middleware: rate limits, body size cap, security headers, minimal access log."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        read_limiter: TokenBucket,
        write_limiter: TokenBucket,
        trust_proxy_headers: bool,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.read_limiter = read_limiter
        self.write_limiter = write_limiter
        self.trust_proxy_headers = trust_proxy_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        method = scope["method"]
        status_holder = {"status": 0}

        def done() -> None:
            ms = (time.monotonic() - started) * 1000
            log.info("%s %s %s %.0fms", method, scope["path"], status_holder["status"], ms)

        limiter = self.read_limiter if method in READ_METHODS else self.write_limiter
        wait = limiter.take(client_key(scope, self.trust_proxy_headers))
        if wait > 0:
            status_holder["status"] = 429
            retry = str(max(1, math.ceil(wait))).encode()
            await _send_json(send, 429, {"detail": "rate limit exceeded"}, [(b"retry-after", retry)])
            done()
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_large = int(value) > self.max_body_bytes
                except ValueError:
                    too_large = True
                if too_large:
                    status_holder["status"] = 413
                    await _send_json(send, 413, {"detail": f"body exceeds {self.max_body_bytes} bytes"}, [])
                    done()
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise HTTPException(413, f"body exceeds {self.max_body_bytes} bytes")
            return message

        async def guarded_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message = {**message, "headers": [*message.get("headers", []), *SECURITY_HEADERS]}
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        finally:
            done()
