import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_helper.notify import Delivery, Notifier, http_transport, sign

WEBHOOK = "https://hooks.example.invalid/agent-helper"


class Recorder:
    def __init__(self, results: list[Delivery] | None = None) -> None:
        self.calls: list[tuple[str, dict, dict[str, str]]] = []
        self.raw: list[bytes] = []
        self.results = results or []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> Delivery:
        self.calls.append((url, json.loads(body), headers))
        self.raw.append(body)
        return self.results.pop(0) if self.results else Delivery(ok=True, status=200)

    @property
    def events(self) -> list[dict]:
        return [c[1] for c in self.calls]


@pytest.fixture
def notified(make_client):
    """A client whose notifier records events instead of sending them."""

    def _make(**overrides):
        client = make_client(notify_webhook_url=WEBHOOK, **overrides)
        recorder = Recorder()
        notifier = client.app.state.notifier
        notifier.transport = recorder
        notifier.retry_delays = (0.0, 0.0)
        return client, notifier, recorder

    return _make


def test_no_webhook_means_no_notifications(client):
    assert client.app.state.notifier.enabled is False
    assert client.post("/v1/requests", json={"message": "hello"}).status_code == 201


def test_new_request_and_follow_up_notify_without_content(notified):
    client, notifier, rec = notified()
    created = client.post("/v1/requests", json={"message": "secret-ish details", "handle": "nova"}).json()
    auth = {"Authorization": f"Bearer {created['follow_up_token']}"}
    client.post(f"/v1/requests/{created['id']}/messages", json={"message": "more"}, headers=auth)
    assert notifier.flush()

    first, second = rec.events
    assert first["event"] == "request.created"
    assert first["id"] == created["id"]
    assert first["handle"] == "nova"
    assert first["admin_api_url"] == f"http://testserver/admin/v1/requests/{created['id']}"
    assert first["event_id"] != second["event_id"]
    assert "nova" in first["text"]
    assert second["event"] == "request.message"
    # Agent text never leaves the service unless the operator opts in.
    assert "secret-ish" not in json.dumps(rec.events)
    assert "untrusted_preview" not in first
    # Tokens never leave the service.
    assert created["follow_up_token"] not in json.dumps(rec.events)


def test_preview_is_opt_in_truncated_and_labelled(notified):
    client, notifier, rec = notified(notify_include_preview=True)
    client.post("/v1/requests", json={"message": "x" * 1000})
    assert notifier.flush()
    preview = rec.events[0]["untrusted_preview"]
    assert len(preview) == 281 and preview.endswith("…")


def test_reports_notify_and_board_only_when_enabled(notified):
    client, notifier, rec = notified()
    client.post("/v1/reports", json={"kind": "bug", "text": "broken"})
    client.post("/v1/board", json={"content": "hello"})
    assert notifier.flush()
    assert [e["event"] for e in rec.events] == ["report.created"]
    assert rec.events[0]["kind"] == "bug"

    client, notifier, rec = notified(notify_events=frozenset({"board.posted"}))
    client.post("/v1/board", json={"content": "hello", "author": "nova"})
    client.post("/v1/requests", json={"message": "hi"})
    assert notifier.flush()
    assert [e["event"] for e in rec.events] == ["board.posted"]


def test_operator_actions_do_not_notify(notified, admin_headers):
    client, notifier, rec = notified(notify_events=frozenset({"board.posted", "request.message"}))
    req = client.post("/v1/requests", json={"message": "hi"}).json()
    client.post(f"/admin/v1/requests/{req['id']}/replies", json={"message": "hello"}, headers=admin_headers)
    client.post("/admin/v1/board", json={"content": "operator note"}, headers=admin_headers)
    assert notifier.flush()
    assert rec.events == []


def test_mcp_tools_notify_too(notified):
    client, notifier, rec = notified()
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "describe_need", "arguments": {"message": "need a scanner"}},
        },
    )
    assert r.status_code == 200
    assert notifier.flush()
    assert [e["event"] for e in rec.events] == ["request.created"]


def test_signature_header(notified):
    client, notifier, rec = notified(notify_webhook_secret="s3cret")  # noqa: S106
    client.post("/v1/requests", json={"message": "hi"})
    assert notifier.flush()
    _, _, headers = rec.calls[0]
    assert headers["X-Agent-Helper-Event"] == "request.created"
    # The signature covers the exact bytes that were sent.
    expected = sign("s3cret", headers["X-Agent-Helper-Timestamp"], rec.raw[0])
    assert headers["X-Agent-Helper-Signature"] == expected


def test_failed_delivery_never_breaks_the_request(notified):
    client, notifier, rec = notified()

    def boom(*_):
        raise RuntimeError("webhook down")

    notifier.transport = boom
    assert client.post("/v1/requests", json={"message": "hi"}).status_code == 201
    assert notifier.flush()


def test_retries_on_server_error_but_not_on_client_error():
    rec = Recorder([Delivery(ok=False, status=502, retry=True), Delivery(ok=True, status=200)])
    n = Notifier(WEBHOOK, base_url="http://x", transport=rec, retry_delays=(0.0, 0.0))
    assert n.send_now(b'{"event": "test"}').ok
    assert len(rec.calls) == 2

    rec = Recorder([Delivery(ok=False, status=404)])
    n = Notifier(WEBHOOK, base_url="http://x", transport=rec, retry_delays=(0.0, 0.0))
    assert not n.send_now(b'{"event": "test"}').ok
    assert len(rec.calls) == 1


def test_flood_is_capped_and_reported_in_a_digest():
    rec = Recorder()
    n = Notifier(WEBHOOK, base_url="http://x", transport=rec, max_per_minute=3, digest_interval=0.0)
    for i in range(10):
        n.emit("request.created", id=f"req_{i}", handle=None)
    assert n.flush()
    individual = [e for e in rec.events if e["event"] == "request.created"]
    digests = [e for e in rec.events if e["event"] == "digest"]
    assert len(individual) == 3
    assert sum(d["missed"] for d in digests) == 7
    assert digests[0]["open_requests_api_url"] == "http://x/admin/v1/requests?status=open"
    assert n.missed == 0
    n.close()


def test_digest_is_rate_limited_and_survives_a_flood():
    rec = Recorder()
    n = Notifier(WEBHOOK, base_url="http://x", transport=rec, max_per_minute=1, digest_interval=3600.0)
    for i in range(50):
        n.emit("request.created", id=f"req_{i}", handle=None)
    assert n.flush()
    # One event goes out; the digest waits for its interval however many events come in.
    assert [e["event"] for e in rec.events] == ["request.created"]
    assert n.missed == 49
    n.close()


def test_stale_and_failed_events_count_as_missed():
    rec = Recorder([Delivery(ok=False, status=404)])
    n = Notifier(WEBHOOK, base_url="http://x", transport=rec, max_age=-1.0, digest_interval=3600.0)
    n.emit("request.created", id="req_old", handle=None)
    assert n.flush()
    assert rec.events == [] and n.missed == 1

    n = Notifier(WEBHOOK, base_url="http://x", transport=rec, digest_interval=3600.0)
    n.emit("request.created", id="req_404", handle=None)
    assert n.flush()
    assert n.missed == 1
    n.close()


def test_full_queue_counts_as_missed(monkeypatch):
    import agent_helper.notify as notify

    monkeypatch.setattr(notify, "QUEUE_SIZE", 1)
    gate = threading.Event()

    def slow(url, body, headers):
        gate.wait(5)
        return Delivery(ok=True, status=200)

    n = Notifier(WEBHOOK, base_url="http://x", transport=slow, digest_interval=3600.0)
    for i in range(5):
        n.emit("request.created", id=f"req_{i}", handle=None)
    assert n.missed >= 3  # one in flight, one queued, the rest dropped
    gate.set()
    assert n.flush()
    n.close()


def test_slow_webhook_does_not_slow_the_agent(notified):
    import time

    client, notifier, _ = notified()
    gate = threading.Event()

    def slow(url, body, headers):
        gate.wait(5)
        return Delivery(ok=True, status=200)

    notifier.transport = slow
    started = time.monotonic()
    for _ in range(3):
        assert client.post("/v1/requests", json={"message": "hi"}).status_code == 201
    assert time.monotonic() - started < 2
    gate.set()
    assert notifier.flush()


def test_close_stops_the_worker():
    n = Notifier(WEBHOOK, base_url="http://x", transport=Recorder())
    n.emit("request.created", id="req_1", handle=None)
    n.close()
    assert n._thread is not None and not n._thread.is_alive()


def test_admin_can_read_one_request_and_send_a_test(notified, admin_headers):
    client, _, rec = notified()
    req = client.post("/v1/requests", json={"message": "hi"}).json()
    r = client.get(f"/admin/v1/requests/{req['id']}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["messages"][0]["body"] == "hi"
    assert client.get("/admin/v1/requests/req_nope", headers=admin_headers).status_code == 404
    assert client.get(f"/admin/v1/requests/{req['id']}").status_code == 401

    r = client.post("/admin/v1/notifications/test", headers=admin_headers)
    assert r.json() == {"delivered": True, "status": 200, "error": None}
    assert rec.events[-1]["event"] == "test"


def test_test_notification_without_webhook(client, admin_headers):
    r = client.post("/admin/v1/notifications/test", headers=admin_headers)
    assert r.json()["delivered"] is False
    assert "NOTIFY_WEBHOOK_URL" in r.json()["error"]


def test_http_transport_against_a_local_server():
    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(301 if self.path == "/redirect" else 204)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert http_transport(url + "/hook", b"{}", {"Content-Type": "application/json"}).ok
        assert received == [b"{}"]
        # Redirects are not followed.
        result = http_transport(url + "/redirect", b"{}", {})
        assert not result.ok and result.status == 301 and not result.retry
    finally:
        server.shutdown()


def test_settings_reject_non_http_webhook(monkeypatch):
    from agent_helper.config import Settings

    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "file:///etc/passwd")
    monkeypatch.setenv("NOTIFY_EVENTS", "request.created, nonsense")
    settings = Settings.from_env()
    assert settings.notify_webhook_url is None
    assert settings.notify_events == frozenset({"request.created"})


def test_slow_dripping_receiver_hits_the_overall_deadline(monkeypatch):
    import socket
    import time

    import agent_helper.notify as notify

    monkeypatch.setattr(notify, "TIMEOUT_SECONDS", 0.5)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def drip():
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        while not stop.is_set():
            try:
                conn.sendall(b"X-Slow: 1\r\n")
            except OSError:
                break
            time.sleep(0.2)
        conn.close()

    threading.Thread(target=drip, daemon=True).start()
    try:
        started = time.monotonic()
        result = http_transport(f"http://127.0.0.1:{server.getsockname()[1]}/", b"{}", {})
        assert time.monotonic() - started < 3
        assert not result.ok and result.error == "deadline exceeded"
    finally:
        stop.set()
        server.close()


@pytest.mark.parametrize("url", ["http://", "https:///path", "ftp://host/x", "http://[::1"])
def test_settings_reject_webhook_without_host(monkeypatch, url):
    from agent_helper.config import Settings

    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", url)
    assert Settings.from_env().notify_webhook_url is None


def test_settings_warn_without_secret_and_on_public_http(monkeypatch, caplog):
    from agent_helper.config import Settings

    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "http://hooks.example.org/x")
    with caplog.at_level("WARNING"):
        assert Settings.from_env().notify_webhook_url == "http://hooks.example.org/x"
    assert "NOTIFY_WEBHOOK_SECRET" in caplog.text and "plain http" in caplog.text

    caplog.clear()
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "http://192.168.1.20:5678/webhook/x")
    monkeypatch.setenv("NOTIFY_WEBHOOK_SECRET", "s")
    with caplog.at_level("WARNING"):
        Settings.from_env()
    assert caplog.text == ""
