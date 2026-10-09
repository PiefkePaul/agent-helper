"""Push relay (docs/decisions/0020, step 9): runs on its own server, sends signed notices to agents.

Run with `python -m agent_helper.relay`. It has no database and no subscription secrets. The service hands
it jobs over HTTPS (`POST /v1/relay/jobs`) and collects outcomes (`GET /v1/relay/outcomes`); both calls are
authenticated with an HMAC over the request using RELAY_SECRET. The relay never connects to the service.

For every attempt the relay resolves the destination itself, refuses any non-public or denied address,
applies caps per destination, and connects to exactly the address it checked.
"""

from __future__ import annotations

import collections
import hashlib
import heapq
import hmac
import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import MIN_ADMIN_SECRET_LENGTH, parse_networks
from .limits import TokenBucket
from .pushcheck import (
    Attempt,
    Destination,
    DestinationRefused,
    Resolver,
    check_url,
    parse_list,
    pinned_post,
    public_addresses,
    system_resolver,
)

log = logging.getLogger("agent_helper.relay")

SIGNATURE_WINDOW_SECONDS = 60
RETRY_DELAYS = (30.0, 180.0)  # before the second and third attempt; a notice is never older than 5 minutes
MAX_JOBS_PER_CALL = 50
MAX_OUTCOMES = 10_000
MAX_QUEUE = 5_000
WORKERS = 8  # one slow name server or endpoint must not hold up everyone else
MAX_REQUEST_BYTES = 128 * 1024
MAX_OPT_OUTS = 100_000
# Checked with fullmatch: no trailing newline, no Unicode digits or letters (explicit ASCII classes).
EVENT_ID = re.compile(r"evt_[0-9a-f]{24}")
SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}")
TIMESTAMP = re.compile(r"[0-9]{1,12}")
NONCE = re.compile(r"[0-9A-Za-z_-]{16,64}")
JOB_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
SHORT_TEXT = re.compile(r"[\x20-\x7e]{1,600}")  # printable ASCII only
PUSH_EVENT_TYPES = ("request.reply", "mail.received", "referral.received")
# Exactly these fields per notice type: the relay sends nothing but what the design allows.
BODY_FIELDS = {
    "push.verify": {"type", "event_id", "time", "handle", "instance", "code", "about", "confirm"},
    "notice": {"type", "event_id", "time", "handle", "instance", "count", "events"},
    "subscription.expiring": {"type", "event_id", "time", "handle", "instance", "expires_at"},
}
BODY_KIND = {"push.verify": "verify", "notice": "notice", "subscription.expiring": "notice"}
OUTCOMES = ("delivered", "failed", "tls_failure", "opted_out", "refused", "capped")


def sign(secret: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    """HMAC that authenticates a call from the service to the relay. The nonce makes every call unique, so
    a repeated signature is always a replay."""
    message = f"{timestamp}.{nonce}.{method.upper()}.{path}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _short(value: Any) -> bool:
    return _matches(SHORT_TEXT, value)


def valid_body(job: dict[str, Any]) -> bool:
    """The notice body is JSON with a fixed set of fields for its type, matching the job. Anything else is
    refused, so the relay cannot be used to send arbitrary content."""
    raw = job.get("body")
    if not isinstance(raw, str) or len(raw) > 1024 or not raw.isascii():
        return False
    try:
        body = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    kind = body.get("type")
    if kind not in BODY_FIELDS or set(body) != BODY_FIELDS[kind] or BODY_KIND[kind] != job.get("kind"):
        return False
    if body["event_id"] != job.get("event_id") or not _matches(UTC_TIME, body["time"]):
        return False
    if not (_short(body["handle"]) and len(body["handle"]) <= 64 and _short(body["instance"])):
        return False
    if kind == "push.verify":
        return all(_short(body[k]) for k in ("code", "about", "confirm"))
    if kind == "subscription.expiring":
        return _matches(UTC_TIME, body["expires_at"])
    events = body["events"]
    count = body["count"]
    return (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 1
        and isinstance(events, list)
        and len(events) <= 10
        and all(
            isinstance(e, dict)
            and set(e) == {"event", "id"}
            and e["event"] in PUSH_EVENT_TYPES
            and (_matches(JOB_ID, e["id"]) or (isinstance(e["id"], int) and not isinstance(e["id"], bool)))
            for e in events
        )
    )


def notice_headers(job: dict[str, Any]) -> dict[str, str]:
    """The only headers the relay ever sends. Jobs carry values, never header names, so a compromised or
    buggy service cannot make the relay send arbitrary headers."""
    return {
        "Content-Type": "application/json",
        "User-Agent": "agent-helper-push",
        "X-Agent-Helper-Event-Id": job["event_id"],
        "X-Agent-Helper-Timestamp": job["timestamp"],
        "X-Agent-Helper-Signature": job["signature"],
    }


@dataclass(frozen=True)
class RelaySettings:
    secret: str
    mode: str = "public"  # "public" or "allowlist"
    allowed_domains: tuple[str, ...] = ()
    deny_domains: tuple[str, ...] = ()
    deny_nets: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    allowed_ports: tuple[int, ...] = (443,)
    max_per_minute: int = 120
    per_destination_per_minute: int = 30
    data_dir: Path = Path("./relay-data")
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> RelaySettings:
        mode = os.environ.get("PUSH_MODE", "public").strip().lower()
        if mode not in ("public", "allowlist"):
            raise SystemExit("PUSH_MODE on the relay must be 'public' or 'allowlist'")
        deny_nets = parse_networks(os.environ.get("PUSH_DENY_NETS", ""))
        if os.environ.get("PUSH_DENY_NETS", "").strip() and not deny_nets:
            raise SystemExit("PUSH_DENY_NETS contains an invalid entry")
        return cls(
            secret=os.environ.get("RELAY_SECRET", ""),
            mode=mode,
            allowed_domains=parse_list(os.environ.get("PUSH_ALLOWED_DOMAINS", "")),
            deny_domains=parse_list(os.environ.get("PUSH_DENY_DOMAINS", "")),
            deny_nets=deny_nets or (),
            allowed_ports=tuple(int(p) for p in parse_list(os.environ.get("PUSH_ALLOWED_PORTS", "443"))),
            max_per_minute=int(os.environ.get("PUSH_MAX_PER_MIN", "120")),
            data_dir=Path(os.environ.get("RELAY_DATA_DIR", "./relay-data")),
            log_level=os.environ.get("LOG_LEVEL", "info"),
        )


def registrable_domain(host: str) -> str:
    """An approximation of the registrable domain without a public-suffix list: the last two labels, or
    three when the second-to-last is a short label under a two-letter country code (as in example.co.uk)."""
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _network_key(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network((addr, prefix), strict=False))


@dataclass(order=True)
class _Scheduled:
    due: float
    seq: int
    job: dict[str, Any] = field(compare=False)
    attempt: int = field(compare=False, default=1)


class Sender:
    """Queues jobs, delivers them with retries, keeps caps and the opt-out list, and records outcomes."""

    def __init__(
        self,
        settings: RelaySettings,
        resolver: Resolver = system_resolver,
        post: Callable[..., Attempt] = pinned_post,
        clock: Callable[[], float] = time.monotonic,
        autostart: bool = True,
    ) -> None:
        self.settings = settings
        self.autostart = autostart
        self.resolver = resolver
        self.post = post
        self.clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._heap: list[_Scheduled] = []
        self._seq = 0
        self._outcomes: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_OUTCOMES)
        self._outcome_seq = 0
        self._global = TokenBucket(settings.max_per_minute)
        self._per_destination = TokenBucket(settings.per_destination_per_minute)
        self._verify_last: dict[str, float] = {}
        self._optout_file = settings.data_dir / "optout.txt"
        self._optout: set[str] = set()
        if self._optout_file.exists():
            self._optout = {line.strip() for line in self._optout_file.read_text("utf-8").splitlines() if line}
        self._threads: list[threading.Thread] = []
        self._caps_lock = threading.Lock()
        self._stopped = False

    # --- intake ---------------------------------------------------------------------------------

    def submit(self, jobs: list[dict[str, Any]]) -> int:
        accepted, overflow = 0, []
        with self._lock:
            for job in jobs:
                if len(self._heap) >= MAX_QUEUE:
                    overflow.append(job)
                    continue
                self._seq += 1
                heapq.heappush(self._heap, _Scheduled(self.clock(), self._seq, job))
                accepted += 1
            self._wake.notify()
        for job in overflow:
            self._record(job, "capped")
        if self.autostart:
            self._start()
        return accepted

    def outcomes(self, after: int, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            items = [o for o in self._outcomes if o["seq"] > after][:limit]
            return items, self._outcome_seq

    # --- work -----------------------------------------------------------------------------------

    def _start(self) -> None:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            while len(self._threads) < WORKERS:
                thread = threading.Thread(target=self._run, name="agent-helper-relay", daemon=True)
                thread.start()
                self._threads.append(thread)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._wake.notify_all()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._stopped and (not self._heap or self._heap[0].due > self.clock()):
                    wait = None if not self._heap else max(0.0, self._heap[0].due - self.clock())
                    self._wake.wait(timeout=wait if wait is not None else 5.0)
                if self._stopped:
                    return
                item = heapq.heappop(self._heap)
            try:
                self._attempt(item)
            except Exception:  # noqa: BLE001 (a bug must not stop the relay)
                log.exception("push job failed unexpectedly")
                self._record(item.job, "failed")

    def run_due(self) -> None:
        """Process every job that is due now, on the calling thread (for tests)."""
        while True:
            with self._lock:
                if not self._heap or self._heap[0].due > self.clock():
                    return
                item = heapq.heappop(self._heap)
            self._attempt(item)

    def _record(self, job: dict[str, Any], outcome: str) -> None:
        with self._lock:
            self._outcome_seq += 1
            self._outcomes.append({"seq": self._outcome_seq, "job_id": job.get("job_id"), "outcome": outcome})
        log.info("push %s %s", job.get("kind"), outcome)  # no URL, no body

    def _retry(self, item: _Scheduled) -> bool:
        if item.attempt > len(RETRY_DELAYS):
            return False
        with self._lock:
            self._seq += 1
            delay = RETRY_DELAYS[item.attempt - 1]
            heapq.heappush(self._heap, _Scheduled(self.clock() + delay, self._seq, item.job, item.attempt + 1))
            self._wake.notify()
        return True

    def _attempt(self, item: _Scheduled) -> None:
        job = item.job
        kind = job.get("kind")
        try:
            dest = check_url(
                job.get("url", ""),
                allowed_ports=self.settings.allowed_ports,
                deny_domains=self.settings.deny_domains,
                allow_domains=self.settings.allowed_domains if self.settings.mode == "allowlist" else None,
            )
            if dest.host in self._optout:  # every kind of job, not only verifications
                raise DestinationRefused("host opted out")
        except DestinationRefused as exc:
            log.info("push refused: %s", exc)
            self._record(job, "opted_out" if str(exc) == "host opted out" else "refused")
            return
        # Caps that need no DNS come first, so a name server that never answers is capped too.
        if not self._take_name_caps(dest, kind, item.attempt):
            self._record(job, "capped")
            return
        try:
            addresses = public_addresses(dest.host, dest.port, self.settings.deny_nets, self.resolver)
        except DestinationRefused as exc:
            log.info("push refused: %s", exc)
            self._record(job, "refused")
            return
        if not self._take_address_caps(addresses[0]):
            self._record(job, "capped")
            return

        body = job["body"].encode() if isinstance(job["body"], str) else bytes(job["body"])
        attempt = Attempt(status=None)
        for address in addresses[:4]:  # resolver order; the next one only if this one did not answer
            attempt = self.post(dest, address, notice_headers(job), body)
            if attempt.status is not None or attempt.tls_failure:
                break
        if attempt.tls_failure:
            self._record(job, "tls_failure")
        elif attempt.status is not None and 200 <= attempt.status < 300:
            self._record(job, "delivered")
        elif kind == "verify" and attempt.status == 410:
            # Only 410 opts a host out: 403 is what many ordinary APIs answer to an unknown caller.
            self._opt_out(dest.host)
            self._record(job, "opted_out")
        elif attempt.status is None or attempt.status == 429 or attempt.status >= 500:
            if not self._retry(item):
                self._record(job, "failed")
        else:
            self._record(job, "failed")

    def _take_name_caps(self, dest: Destination, kind: str | None, attempt: int) -> bool:
        now = self.clock()
        if kind == "verify" and attempt == 1:  # retries of an admitted verification are not new ones
            with self._caps_lock:
                if len(self._verify_last) > 50_000:
                    self._verify_last = {h: t for h, t in self._verify_last.items() if now - t < 3600}
                last = self._verify_last.get(dest.host)
                if last is not None and now - last < 3600:
                    return False
                self._verify_last[dest.host] = now
        if self._per_destination.take(registrable_domain(dest.host), now=now) > 0:
            return False
        return self._global.take("global", now=now) == 0

    def _take_address_caps(self, address: Any) -> bool:
        now = self.clock()
        return all(self._per_destination.take(key, now=now) == 0 for key in (str(address), _network_key(address)))

    def _opt_out(self, host: str) -> None:
        with self._lock:
            if host in self._optout or len(self._optout) >= MAX_OPT_OUTS:
                return
            self._optout.add(host)
            try:
                self.settings.data_dir.mkdir(parents=True, exist_ok=True)
                with self._optout_file.open("a", encoding="utf-8") as fh:
                    fh.write(host + "\n")
            except OSError:
                log.warning("could not persist the opt-out list")


class _SeenSignatures:
    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_time(self, signature: str, now: float) -> bool:
        with self._lock:
            self._seen = {s: t for s, t in self._seen.items() if now - t < 2 * SIGNATURE_WINDOW_SECONDS}
            if signature in self._seen:
                return False
            self._seen[signature] = now
            return True


def create_relay_app(settings: RelaySettings, sender: Sender | None = None) -> FastAPI:
    if len(settings.secret) < MIN_ADMIN_SECRET_LENGTH:
        raise SystemExit(f"RELAY_SECRET must be at least {MIN_ADMIN_SECRET_LENGTH} characters")
    sender = sender or Sender(settings)
    seen = _SeenSignatures()
    app = FastAPI(title="agent-helper relay", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sender = sender

    async def authenticate(request: Request) -> bytes:
        timestamp = request.headers.get("x-relay-timestamp", "")
        given = request.headers.get("x-relay-signature", "")
        nonce = request.headers.get("x-relay-nonce", "")
        if not NONCE.fullmatch(nonce) or not TIMESTAMP.fullmatch(timestamp) or not SIGNATURE.fullmatch(given):
            raise HTTPException(401, "unauthorized")
        age = abs(time.time() - int(timestamp))
        if age > SIGNATURE_WINDOW_SECONDS:
            raise HTTPException(401, "unauthorized")
        # Read the body only now, and never more than the largest valid call.
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "too large")
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        expected = sign(settings.secret, timestamp, nonce, request.method, path, body)
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(401, "unauthorized")
        if not seen.first_time(given, time.time()):
            raise HTTPException(401, "unauthorized")
        return body

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/relay/jobs")
    async def jobs(request: Request) -> JSONResponse:
        body = await authenticate(request)
        try:
            data = json.loads(body)
        except ValueError:
            raise HTTPException(400, "bad json") from None
        items = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) > MAX_JOBS_PER_CALL:
            raise HTTPException(400, f"send 'jobs', a list of at most {MAX_JOBS_PER_CALL}")
        valid = []
        for job in items:
            if not (
                isinstance(job, dict)
                and _matches(JOB_ID, job.get("job_id"))
                and job.get("kind") in ("verify", "notice")
                and isinstance(job.get("url"), str)
                and _matches(EVENT_ID, job.get("event_id"))
                and _matches(TIMESTAMP, job.get("timestamp"))
                and _matches(SIGNATURE, job.get("signature"))
                and valid_body(job)
            ):
                raise HTTPException(400, "invalid job")
            valid.append(job)
        return JSONResponse({"accepted": sender.submit(valid)})

    @app.get("/v1/relay/outcomes")
    async def outcomes(request: Request, after: int = 0) -> JSONResponse:
        await authenticate(request)
        items, last = sender.outcomes(max(0, after))
        return JSONResponse({"outcomes": items, "last": last})

    return app


def main() -> None:
    import uvicorn

    settings = RelaySettings.from_env()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("RELAY_LISTEN_PORT", "8090"))
    # Either a TLS-terminating proxy in front of the relay, or a certificate and key given here.
    cert, key = os.environ.get("RELAY_TLS_CERT") or None, os.environ.get("RELAY_TLS_KEY") or None
    uvicorn.run(
        create_relay_app(settings),
        host=os.environ.get("RELAY_LISTEN_HOST", "0.0.0.0"),  # noqa: S104 (the relay's own container)
        port=port,
        access_log=False,
        proxy_headers=False,
        ssl_certfile=cert,
        ssl_keyfile=key,
    )


if __name__ == "__main__":
    main()
