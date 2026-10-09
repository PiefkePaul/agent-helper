"""Outbound notifications to the operator (docs/decisions/0012).

When an agent writes something the operator should see, a small JSON event is POSTed to one webhook URL
from the configuration (for example an n8n workflow that forwards to a chat app). Delivery happens on a
background thread, so a slow or broken webhook never delays or fails the agent's request.

Events carry metadata only by default. Agent text is untrusted; it is included, truncated and labelled,
only when the operator turns on `NOTIFY_INCLUDE_PREVIEW`. The handle, also chosen by the agent, is quoted.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import queue
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .config import DEFAULT_NOTIFY_EVENTS, Settings
from .limits import TokenBucket

log = logging.getLogger("agent_helper.notify")

PREVIEW_CHARS = 280
QUEUE_SIZE = 1000
TIMEOUT_SECONDS = 5.0
RETRY_DELAYS = (2.0, 5.0)  # seconds before the second and third attempt
MAX_AGE_SECONDS = 600.0  # older queued events are counted as missed instead of sent late
DIGEST_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class Delivery:
    ok: bool
    status: int | None = None
    error: str | None = None
    retry: bool = False


Transport = Callable[[str, bytes, dict[str, str]], Delivery]


def http_transport(url: str, body: bytes, headers: dict[str, str]) -> Delivery:
    """POST with an overall deadline. The URL is never logged or put into errors: it may contain a secret.

    A socket timeout only bounds each read, so a receiver that answers one byte at a time could hold a
    request for a long time. The request runs on a helper thread; at the deadline its socket is closed, which
    ends that thread too. Redirects are never followed (`http.client` does not follow them).
    """
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    try:
        conn = connection_class(parts.hostname or "", parts.port, timeout=TIMEOUT_SECONDS)
    except Exception as exc:
        return Delivery(ok=False, error=type(exc).__name__)
    result: list[Delivery] = []

    def post() -> None:
        try:
            conn.request("POST", path, body=body, headers=headers)
            status = conn.getresponse().status
            ok = 200 <= status < 300
            retry = status == 429 or status >= 500
            result.append(Delivery(ok=ok, status=status, error=None if ok else f"HTTP {status}", retry=retry))
        except OSError as exc:  # network trouble, including timeouts and the socket closed at the deadline
            result.append(Delivery(ok=False, error=type(exc).__name__, retry=True))
        except Exception as exc:  # malformed responses and the like; type name only, never the message
            result.append(Delivery(ok=False, error=type(exc).__name__))

    worker = threading.Thread(target=post, name="agent-helper-notify-post", daemon=True)
    worker.start()
    worker.join(TIMEOUT_SECONDS + 1)
    if result:
        conn.close()
        return result[0]
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)  # unblocks a pending read even while the response holds the socket
        except OSError:
            pass
    conn.close()
    worker.join(1)
    return Delivery(ok=False, error="deadline exceeded", retry=True)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Signature the receiver can check: HMAC-SHA256 over `<timestamp>.<body>` with the shared secret."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _summary(event: str, fields: dict[str, Any]) -> str:
    # The handle is chosen by the agent: quote it so it reads as a name, not as the service speaking.
    who = f' from handle "{fields["handle"]}"' if fields.get("handle") else ""
    match event:
        case "request.created":
            return f"New request {fields['id']}{who}"
        case "request.message":
            return f"New message on request {fields['id']}{who}"
        case "report.created":
            return f"New {fields.get('kind', 'other')} report {fields['id']} (quarantined)"
        case "board.posted":
            return f"New board entry #{fields['seq']}{who}"
        case "capability.requested":
            return f"New capability request {fields['id']}{who}"
        case "directory.published":
            return f"Directory profile published or updated{who}"
        case _:
            return event


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Notifier:
    """Sends events to the configured webhook. A no-op when no webhook is configured.

    Flood handling: at most `max_per_minute` events are queued. Events over that cap, events that waited in
    the queue longer than `max_age` seconds (for example while the webhook was down), and events lost to a
    full queue or a failed delivery are counted. The count is sent as a `digest` event at most once per
    `digest_interval` seconds, so the operator always learns that something was missed.
    """

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
        max_age: float = MAX_AGE_SECONDS,
        digest_interval: float = DIGEST_INTERVAL_SECONDS,
    ) -> None:
        self.url = url
        self.base_url = base_url
        self.secret = secret
        self.events = events
        self.include_preview = include_preview
        self.transport = transport
        self.retry_delays = retry_delays
        self.max_age = max_age
        self.digest_interval = digest_interval
        self._budget = TokenBucket(max_per_minute)
        self._missed = 0
        self._missed_lock = threading.Lock()
        self._last_digest = time.monotonic()
        self._queue: queue.Queue[tuple[float, bytes] | None] = queue.Queue(maxsize=QUEUE_SIZE)
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

    @property
    def missed(self) -> int:
        with self._missed_lock:
            return self._missed

    # --- producing events -----------------------------------------------------------------------

    def emit(self, event: str, **fields: Any) -> None:
        """Queue an event. Never blocks and never raises: notifications must not break agent requests."""
        if not self.enabled or event not in self.events:
            return
        try:
            self._start()
            if self._budget.take("notify") > 0:
                self._miss()
                return
            self._queue.put_nowait((time.monotonic(), self._payload(event, fields)))
        except queue.Full:
            self._miss()
        except Exception:
            log.exception("could not queue notification")
            self._miss()

    def _miss(self, count: int = 1) -> None:
        with self._missed_lock:
            self._missed += count

    def _envelope(self, event: str, text: str) -> dict[str, Any]:
        return {
            "event": event,
            "event_id": secrets.token_hex(8),
            "service": self.base_url,
            "created_at": _utc_now(),
            "text": text,
        }

    def _payload(self, event: str, fields: dict[str, Any]) -> bytes:
        preview = fields.pop("preview", None)
        data = self._envelope(event, _summary(event, fields)) | fields
        # API locations, not clickable pages: they need the admin bearer secret.
        if "id" in fields and event.startswith("request."):
            data["admin_api_url"] = f"{self.base_url}/admin/v1/requests/{fields['id']}"
        elif "id" in fields and event.startswith("report."):
            data["admin_api_url"] = f"{self.base_url}/admin/v1/reports?status=quarantined"
        if self.include_preview and preview:
            cut = preview[:PREVIEW_CHARS] + ("…" if len(preview) > PREVIEW_CHARS else "")
            data["untrusted_preview"] = cut
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _digest(self, missed: int) -> bytes:
        data = self._envelope(
            "digest",
            f"{missed} notification(s) were not sent individually (rate cap, delay, or delivery failure). "
            "Check the open requests and quarantined reports.",
        )
        data |= {
            "missed": missed,
            "open_requests_api_url": f"{self.base_url}/admin/v1/requests?status=open",
            "quarantined_reports_api_url": f"{self.base_url}/admin/v1/reports?status=quarantined",
        }
        return json.dumps(data).encode("utf-8")

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
                result = self.transport(self.url or "", body, self._headers(body, event))
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
        body = json.dumps(self._envelope("test", "Test notification from agent-helper")).encode()
        return self.send_now(body, retry=False)

    def _start(self) -> None:
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="agent-helper-notify", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=min(self.digest_interval, 60.0))
            except queue.Empty:
                self._maybe_send_digest()
                continue
            try:
                if item is None:
                    return
                queued_at, body = item
                if time.monotonic() - queued_at > self.max_age:
                    self._miss()  # stale news; the digest says something was missed instead
                elif not self.send_now(body).ok:
                    self._miss()
                self._maybe_send_digest()
            finally:
                self._queue.task_done()

    def _maybe_send_digest(self) -> None:
        if time.monotonic() - self._last_digest < self.digest_interval:
            return
        with self._missed_lock:
            missed, self._missed = self._missed, 0
        if not missed:
            return
        self._last_digest = time.monotonic()
        if not self.send_now(self._digest(missed)).ok:
            self._miss(missed)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued events are handled. Returns False on timeout. Meant for tests and shutdown."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker. Events still queued or in retry after `timeout` are lost (the database is not)."""
        if self._thread is not None and self._thread.is_alive():
            self.flush(timeout)
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                return
            self._thread.join(timeout)
