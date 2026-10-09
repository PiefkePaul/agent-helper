import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_helper.notify import Delivery, Notifier, http_transport, sign

WEBHOOK = "https://hooks.example.invalid/agent-helper"


class Recorder:
    def __init__(self, results: list[Delivery] | None = None) -> None:
        self.calls: list[tuple[str, dict, dict[str, str]]] = []
        self.results = results or []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> Delivery:
        self.calls.append((url, json.loads(body), headers))
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
    assert first["admin_url"] == f"http://testserver/admin/v1/requests/{created['id']}"
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
    _, payload, headers = rec.calls[0]
    assert headers["X-Agent-Helper-Event"] == "request.created"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert headers["X-Agent-Helper-Signature"] == sign("s3cret", headers["X-Agent-Helper-Timestamp"], body)


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


def test_flood_is_capped_and_counted():
    rec = Recorder()
    n = Notifier(WEBHOOK, base_url="http://x", transport=rec, max_per_minute=3)
    for i in range(10):
        n.emit("request.created", id=f"req_{i}", handle=None)
    assert n.flush()
    assert len(rec.calls) == 3
    n._budget = type(n._budget)(per_minute=3)  # a minute later
    n.emit("request.created", id="req_late", handle=None)
    assert n.flush()
    assert rec.events[-1]["suppressed_before"] == 7
    n.close()


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
