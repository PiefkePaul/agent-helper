"""Push notices to agents' own endpoints, service side (docs/decisions/0020).

The service never contacts an agent's endpoint itself. It keeps the subscriptions, builds and signs each
notice, and hands the finished jobs to the relay over an outbound, HMAC-authenticated HTTPS connection
(`RELAY_URL`). It then collects the outcomes from the relay and applies them (failures, suspension).

Everything runs on one background thread. The store's event hook only queues; it never touches the
database or the network.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import http.client
import json
import logging
import queue
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from .config import Settings
from .pushcheck import Destination, DestinationRefused, check_url, parse_list
from .relay import MAX_COUNT, MAX_JOBS_PER_CALL, OUTCOMES, canonical_json, sign
from .store import PUSH_EVENTS, MailLimits, Store

log = logging.getLogger("agent_helper.push")

MAX_BODY = 1024
TICK_SECONDS = 2.0
SWEEP_SECONDS = 600.0
# The relay may retry for up to 3.5 minutes; together a notice stays inside receivers' 5-minute window.
OUTBOX_MAX_AGE = 60.0
OUTSTANDING_MAX_AGE = 900.0
OPT_OUT_TEXT = (
    "This is a one-time check from an agent-helper service: someone registered this URL to receive short "
    "notices for an AI agent. If that was not you, answer this request with HTTP 410 and no further requests "
    "will be sent to this host."
)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def notice_signature(secret: str, timestamp: str, event_id: str, body: bytes) -> str:
    """What receivers check: HMAC-SHA256 over `<timestamp>.<event_id>.<body>` with the subscription secret."""
    message = f"{timestamp}.{event_id}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_job(kind: str, url: str, secret: str, payload: dict[str, Any]) -> dict[str, Any]:
    event_id = payload["event_id"]
    body = canonical_json(payload)
    if len(body) > MAX_BODY:
        raise ValueError("notice body too large")
    timestamp = str(int(time.time()))
    # Only the values; the relay builds the request headers itself (it accepts no headers from jobs).
    return {
        "job_id": "job_" + secrets.token_hex(12),
        "kind": kind,
        "url": url,
        "body": body,
        "event_id": event_id,
        "timestamp": timestamp,
        "signature": notice_signature(secret, timestamp, event_id, body.encode()),
    }


class RelayTransport(Protocol):
    def call(self, method: str, path: str, body: bytes) -> tuple[int, bytes]: ...


class HttpsRelay:
    """Calls the relay over verified HTTPS, signing every call with RELAY_SECRET."""

    def __init__(self, url: str, secret: str, ca_file: str | None = None, timeout: float = 10.0) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("the relay URL must be https")
        self.host = parts.hostname
        self.port = parts.port or 443
        self.prefix = parts.path.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        self.context = ssl.create_default_context(cafile=ca_file)

    def call(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        full = self.prefix + path
        timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Nonce": nonce,
            "X-Relay-Signature": sign(self.secret, timestamp, nonce, method, full, body),
        }
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.context)
        result: list[tuple[int, bytes] | BaseException] = []

        def run() -> None:
            try:
                conn.request(method, full, body=body if method != "GET" else None, headers=headers)
                response = conn.getresponse()
                result.append((response.status, response.read(1_000_000)))
            except BaseException as exc:  # noqa: BLE001 (handed to the caller)
                result.append(exc)

        # One deadline for the whole call: a relay that trickles bytes must not hold up the push thread.
        worker = threading.Thread(target=run, name="agent-helper-relay-call", daemon=True)
        worker.start()
        worker.join(self.timeout)
        if not result:
            if conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            conn.close()
            raise TimeoutError("relay call exceeded its deadline")
        conn.close()
        if isinstance(result[0], BaseException):
            raise result[0]
        return result[0]


class PushManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        limits: MailLimits,
        transport: RelayTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.limits = limits
        self.enabled = settings.push_enabled
        self.clock = clock
        self.transport = transport
        if self.enabled and transport is None:
            ca = str(settings.relay_ca_file) if settings.relay_ca_file else None
            self.transport = HttpsRelay(settings.relay_url, settings.relay_secret, ca)  # type: ignore[arg-type]
        own = urlsplit(settings.public_base_url).hostname
        self.deny_domains = parse_list(settings.push_deny_domains) + ((own, "*." + own) if own else ())
        self.allow_domains = parse_list(settings.push_allowed_domains) if settings.push_mode == "allowlist" else None
        self.allowed_ports = tuple(int(p) for p in parse_list(settings.push_allowed_ports) if p.isdigit()) or (443,)
        self._events: queue.Queue[tuple[str, str, Any]] = queue.Queue(maxsize=10_000)
        self._outbox: collections.deque[tuple[float, dict[str, Any]]] = collections.deque(maxlen=2_000)
        self._outstanding: dict[str, tuple[str, str, float]] = {}  # job_id -> (subscription id, kind, sent at)
        self._after = 0
        self._last_sweep = -1e9
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- called from request handlers -------------------------------------------------------------

    def check(self, url: str) -> Destination:
        """The checks that need no network (the relay repeats them and resolves). Raises DestinationRefused."""
        return check_url(
            url, allowed_ports=self.allowed_ports, deny_domains=self.deny_domains, allow_domains=self.allow_domains
        )

    def emit(self, event: str, **fields: Any) -> None:
        """The store's event hook: queue and return. Never blocks, never raises."""
        if not self.enabled or event not in PUSH_EVENTS or not fields.get("handle"):
            return
        try:
            self._events.put_nowait((event, fields["handle"], fields.get("id")))
            self._wake.set()
        except queue.Full:
            log.warning("push event queue full; event dropped")

    def nudge(self) -> None:
        self._wake.set()

    # --- background thread ------------------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="agent-helper-push", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 (the thread must survive a bug or a broken relay)
                log.exception("push tick failed")

    def tick(self) -> None:
        """One round: record events, build verification requests and due notices, send, collect outcomes."""
        now = self.clock()
        while True:
            try:
                event, handle, ref_id = self._events.get_nowait()
            except queue.Empty:
                break
            self.store.push_record(handle, event, ref_id)
        if now - self._last_sweep >= SWEEP_SECONDS:
            self._last_sweep = now
            for sub in self.store.push_sweep():
                self._queue(
                    sub, "notice", self._payload(sub, "subscription.expiring", {"expires_at": sub["expires_at"]})
                )
        for sub, code in self.store.push_take_verifications():
            extra = {"code": code, "about": OPT_OUT_TEXT, "confirm": f"POST /v1/handles/{sub['handle']}/push/verify"}
            self._queue(sub, "verify", self._payload(sub, "push.verify", extra))
        for sub in self.store.push_take_due(now):
            events = sub["pending_events"]
            self._queue(
                sub,
                "notice",
                self._payload(sub, "notice", {"count": min(sub["pending_count"], MAX_COUNT), "events": events}),
            )
        self._flush(now)
        self._collect(now)

    def _payload(self, sub: dict[str, Any], kind: str, extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": kind,
            "event_id": "evt_" + secrets.token_hex(12),
            "time": _utc_now(),
            "handle": sub["handle"],
            "instance": self.store.instance,
            **extra,
        }

    def _queue(self, sub: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
        try:
            self.check(sub["url"])  # the lists may have changed since registration
        except DestinationRefused:
            self.store.push_outcome(sub["id"], kind, "refused", self.limits)
            return
        # Ids are short, but the body must stay under 1 KiB whatever they hold: drop the oldest ones.
        while True:
            try:
                job = signed_job(kind, sub["url"], sub["secret"], payload)
                break
            except ValueError:
                if not payload.get("events"):
                    log.warning("push notice too large; dropped")
                    return
                payload["events"] = payload["events"][1:]
        job["_sub"] = sub["id"]
        self._outbox.append((self.clock(), job))

    def _flush(self, now: float) -> None:
        while self._outbox and now - self._outbox[0][0] > OUTBOX_MAX_AGE:
            self._outbox.popleft()
        while self._outbox:
            batch = [self._outbox[i] for i in range(min(MAX_JOBS_PER_CALL, len(self._outbox)))]
            jobs = [{k: v for k, v in job.items() if not k.startswith("_")} for _, job in batch]
            try:
                status, _ = self.transport.call(  # type: ignore[union-attr]
                    "POST", "/v1/relay/jobs", json.dumps({"jobs": jobs}).encode()
                )
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                log.warning("push relay unreachable: %s", type(exc).__name__)
                return
            if status != 200:
                log.warning("push relay refused jobs: HTTP %s", status)
                return
            for _, job in batch:
                self._outbox.popleft()
                self._outstanding[job["job_id"]] = (job["_sub"], job["kind"], now)

    def _collect(self, now: float) -> None:
        self._outstanding = {k: v for k, v in self._outstanding.items() if now - v[2] < OUTSTANDING_MAX_AGE}
        if not self._outstanding:
            return
        try:
            status, raw = self.transport.call("GET", f"/v1/relay/outcomes?after={self._after}", b"")  # type: ignore[union-attr]
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            log.warning("push relay unreachable: %s", type(exc).__name__)
            return
        if status != 200:
            log.warning("push relay refused the outcome query: HTTP %s", status)
            return
        try:
            data = json.loads(raw)
            items, last = data["outcomes"], int(data["last"])
        except (ValueError, KeyError, TypeError):
            log.warning("push relay sent an unreadable outcome list")
            return
        if last < self._after:  # the relay restarted; its counter began again
            self._after = 0
            return
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            meta = self._outstanding.pop(str(item.get("job_id")), None)
            outcome = item.get("outcome")
            if meta is not None and outcome in OUTCOMES:
                self.store.push_outcome(meta[0], meta[1], outcome, self.limits)
            if isinstance(item.get("seq"), int):
                self._after = max(self._after, item["seq"])
