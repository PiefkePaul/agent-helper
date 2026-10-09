"""Outbound notifications to the operator (docs/decisions/0012).

When an agent writes something the operator should see, a small JSON event is POSTed to one webhook URL
from the configuration (for example an n8n workflow that forwards to a chat app). Delivery happens on a
background thread, so a slow or broken webhook never delays or fails the agent's request.

Events carry metadata only by default. Agent text is untrusted; it is included, truncated and labelled,
only when the operator turns on `NOTIFY_INCLUDE_PREVIEW`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import __version__
from .config import DEFAULT_NOTIFY_EVENTS, Settings
from .limits import TokenBucket

log = logging.getLogger("agent_helper.notify")

PREVIEW_CHARS = 280
QUEUE_SIZE = 1000
TIMEOUT_SECONDS = 10.0
RETRY_DELAYS = (2.0, 10.0)  # seconds before the second and third attempt


@dataclass(frozen=True)
class Delivery:
    ok: bool
    status: int | None = None
    error: str | None = None
    retry: bool = False


Transport = Callable[[str, bytes, dict[str, str]], Delivery]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a webhook that redirects is misconfigured; do not follow it anywhere


_opener = urllib.request.build_opener(_NoRedirect)


def http_transport(url: str, body: bytes, headers: dict[str, str]) -> Delivery:
    """POST with the standard library. The URL is never logged: it may contain a secret (a bot token)."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 (scheme checked)
    try:
        with _opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            return Delivery(ok=200 <= resp.status < 300, status=resp.status)
    except urllib.error.HTTPError as exc:
        return Delivery(ok=False, status=exc.code, error=f"HTTP {exc.code}", retry=exc.code == 429 or exc.code >= 500)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Delivery(ok=False, error=type(reason).__name__, retry=True)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Signature the receiver can check: HMAC-SHA256 over `<timestamp>.<body>` with the shared secret."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _summary(event: str, fields: dict[str, Any]) -> str:
    who = f" from {fields['handle']}" if fields.get("handle") else ""
    match event:
        case "request.created":
            return f"New request {fields['id']}{who}"
        case "request.message":
            return f"New message on request {fields['id']}{who}"
        case "report.created":
            return f"New {fields.get('kind', 'other')} report {fields['id']} (quarantined)"
        case "board.posted":
            return f"New board entry #{fields['seq']}{who}"
        case _:
            return event


class Notifier:
    """Sends events to the configured webhook. A no-op when no webhook is configured."""

    def __init__(
        self,
        url: str | None,
        *,
        base_url: str,
        secret: str | None = None,
        events: frozenset[str] = DEFAULT_NOTIFY_EVENTS,
        include_preview: bool = False,
        max_per_minute: int = 30,
        transport: Transport = http_transport,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> None:
        self.url = url
        self.base_url = base_url
        self.secret = secret
        self.events = events
        self.include_preview = include_preview
        self.transport = transport
        self.retry_delays = retry_delays
        self._budget = TokenBucket(max_per_minute)
        self._suppressed = 0
        self._suppressed_lock = threading.Lock()
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_SIZE)
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> Notifier:
        return cls(
            settings.notify_webhook_url,
            base_url=settings.public_base_url,
            secret=settings.notify_webhook_secret,
            events=settings.notify_events,
            include_preview=settings.notify_include_preview,
            max_per_minute=settings.notify_max_per_minute,
        )

    @property
    def enabled(self) -> bool:
        return self.url is not None

    # --- producing events -----------------------------------------------------------------------

    def emit(self, event: str, **fields: Any) -> None:
        """Queue an event. Never blocks and never raises: notifications must not break agent requests."""
        if not self.enabled or event not in self.events:
            return
        try:
            if self._budget.take("notify") > 0:
                self._count_suppressed()
                return
            body = self._payload(event, fields)
            self._start()
            self._queue.put_nowait(body)
        except queue.Full:
            self._count_suppressed()
        except Exception:
            log.exception("could not queue notification")

    def _count_suppressed(self) -> None:
        with self._suppressed_lock:
            self._suppressed += 1

    def _payload(self, event: str, fields: dict[str, Any]) -> bytes:
        preview = fields.pop("preview", None)
        with self._suppressed_lock:
            suppressed, self._suppressed = self._suppressed, 0
        data: dict[str, Any] = {
            "event": event,
            "service": self.base_url,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "text": _summary(event, fields),
            **fields,
            "suppressed_before": suppressed,
        }
        if "id" in fields and event.startswith("request."):
            data["admin_url"] = f"{self.base_url}/admin/v1/requests/{fields['id']}"
        elif "id" in fields and event.startswith("report."):
            data["admin_url"] = f"{self.base_url}/admin/v1/reports?status=quarantined"
        if self.include_preview and preview:
            cut = preview[:PREVIEW_CHARS] + ("…" if len(preview) > PREVIEW_CHARS else "")
            data["untrusted_preview"] = cut
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    # --- delivering -----------------------------------------------------------------------------

    def _headers(self, body: bytes, event: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"agent-helper/{__version__}",
            "X-Agent-Helper-Event": event,
            "X-Agent-Helper-Timestamp": timestamp,
        }
        if self.secret:
            headers["X-Agent-Helper-Signature"] = sign(self.secret, timestamp, body)
        return headers

    def send_now(self, body: bytes, *, retry: bool = True) -> Delivery:
        """Deliver one body on the calling thread, with retries unless `retry` is False."""
        event = json.loads(body).get("event", "unknown")
        result = Delivery(ok=False, error="not attempted")
        for attempt in range(len(self.retry_delays) + 1 if retry else 1):
            if attempt:
                time.sleep(self.retry_delays[attempt - 1])
            try:
                result = self.transport(self.url or "", body, self._headers(body, event))  # type: ignore[arg-type]
            except Exception as exc:  # a transport bug must not kill the worker
                result = Delivery(ok=False, error=type(exc).__name__)
            if result.ok or not result.retry:
                break
        if not result.ok:
            log.warning("notification %s not delivered: %s", event, result.error or result.status)
        return result

    def send_test(self) -> Delivery:
        """Send a test event synchronously so the operator can check the webhook setup."""
        if not self.enabled:
            return Delivery(ok=False, error="no webhook configured (NOTIFY_WEBHOOK_URL)")
        body = json.dumps(
            {
                "event": "test",
                "service": self.base_url,
                "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "text": "Test notification from agent-helper",
            }
        ).encode()
        return self.send_now(body, retry=False)

    def _start(self) -> None:
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="agent-helper-notify", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            body = self._queue.get()
            try:
                if body is None:
                    return
                self.send_now(body)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued events are handled. Returns False on timeout. Meant for tests and shutdown."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout: float = 5.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.flush(timeout)
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                return
            self._thread.join(timeout)
