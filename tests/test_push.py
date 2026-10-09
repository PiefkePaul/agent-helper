"""Push notices (docs/decisions/0020): destination checks, the relay, and the service side."""

import datetime
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_helper.push import notice_signature
from agent_helper.pushcheck import (
    Attempt,
    Destination,
    DestinationRefused,
    check_url,
    pinned_post,
    public_addresses,
)
from agent_helper.relay import RelaySettings, Sender, create_relay_app, registrable_domain, sign

RELAY_SECRET = "relay-secret-" + "r" * 32
PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


# --- destination checks -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com/x#frag",
        "https://hooks.example.com:8443/x",
        "https://93.184.215.14/x",
        "https://[2606:2800:21f::1]/x",
        "https://2130706433/x",
        "https://localhost/x",
        "https://intranet/x",
        "https://printer.local/x",
        "https://nas.lan/x",
        "https://svc.internal/x",
        "https://box.home.arpa/x",
        "https://hidden.onion/x",
        "https://hooks.example.com/" + "a" * 600,
        "https://hooks.example.com/a b",
        "https://hooks.example.com/a\x00",
        "ftp://hooks.example.com/x",
    ],
)
def test_check_url_refuses(url):
    with pytest.raises(DestinationRefused):
        check_url(url)


def test_check_url_normalizes_and_applies_lists():
    dest = check_url("https://Hooks.EXAMPLE.com./in?x=1")
    assert (dest.host, dest.port, dest.path) == ("hooks.example.com", 443, "/in?x=1")
    assert check_url("https://bücher.example/x").host == "xn--bcher-kva.example"
    with pytest.raises(DestinationRefused):
        check_url("https://a.b.own.example/x", deny_domains=["*.own.example"])
    with pytest.raises(DestinationRefused):
        check_url("https://own.example/x", deny_domains=["*.own.example"])
    with pytest.raises(DestinationRefused):
        check_url("https://other.example/x", allow_domains=["*.agents.example"])
    assert check_url("https://x.agents.example/x", allow_domains=["*.agents.example"]).host == "x.agents.example"
    assert check_url("https://hooks.example.com:8443/x", allowed_ports=(443, 8443)).port == 8443


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.5.4",
        "192.168.1.1",
        "100.64.0.1",
        "169.254.169.254",
        "0.0.0.0",  # noqa: S104
        "255.255.255.255",
        "198.18.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "::ffff:127.0.0.1",
        "::ffff:93.184.215.14",
        "64:ff9b::7f00:1",
        "64:ff9b:1::1",
        "2002:7f00:1::1",
        "2001:0:4136:e378::1",
        "2001:db8::1",
        "ff02::1",
    ],
)
def test_non_public_addresses_are_refused(address):
    with pytest.raises(DestinationRefused):
        public_addresses("hooks.example.com", 443, resolver=lambda h, p: [address])


def test_one_bad_address_refuses_the_name_and_deny_nets_apply():
    with pytest.raises(DestinationRefused):
        public_addresses("hooks.example.com", 443, resolver=lambda h, p: [PUBLIC_V4, "10.0.0.1"])
    deny = (ipaddress.ip_network("93.184.215.0/24"),)
    with pytest.raises(DestinationRefused):
        public_addresses("hooks.example.com", 443, deny, resolver=lambda h, p: [PUBLIC_V4])
    assert [str(a) for a in public_addresses("h.example.com", 443, resolver=lambda h, p: [PUBLIC_V4, PUBLIC_V6])] == [
        PUBLIC_V4,
        PUBLIC_V6,
    ]

    def broken(host, port):
        raise socket.gaierror("no such name")

    with pytest.raises(DestinationRefused):
        public_addresses("hooks.example.com", 443, resolver=broken)


# --- pinned HTTPS client ------------------------------------------------------------------------


def _self_signed(tmp_path: Path, name: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_file, key_file


class _TLSServer:
    """A one-thread HTTPS server on 127.0.0.1 that answers every request with `response` (raw bytes)."""

    def __init__(self, cert: Path, key: Path, response: bytes | None, tls: bool = True) -> None:
        self.response = response
        self.requests: list[bytes] = []
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.tls = tls
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        try:
            if self.tls:
                conn = self.ctx.wrap_socket(conn, server_side=True)
            else:
                conn.sendall(b"not tls at all\r\n")
            data = b""
            conn.settimeout(3)
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            # Read the whole body too: closing with unread data makes the kernel send a reset.
            head, _, rest = data.partition(b"\r\n\r\n")
            lengths = [
                int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")
            ]
            while len(rest) < (lengths[0] if lengths else 0):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                rest += chunk
            self.requests.append(data)
            if self.response is None:
                time.sleep(3)  # hang
                return
            conn.sendall(self.response)
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()


def _dest(port: int, host: str = "receiver.example.com") -> Destination:
    return Destination(url=f"https://{host}:{port}/hook", host=host, port=port, path="/hook")


def test_pinned_post_connects_to_the_checked_address_and_verifies_tls(tmp_path):
    cert, key = _self_signed(tmp_path, "receiver.example.com")
    server = _TLSServer(cert, key, b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/\r\nContent-Length: 0\r\n\r\n")
    ctx = ssl.create_default_context(cafile=str(cert))
    attempt = pinned_post(_dest(server.port), ipaddress.ip_address("127.0.0.1"), {"X-A": "1"}, b"{}", ctx)
    # The redirect is returned as it is, never followed.
    assert attempt == Attempt(status=302)
    head = server.requests[0].decode()
    assert head.startswith("POST /hook HTTP/1.1")
    assert f"Host: receiver.example.com:{server.port}" in head


def test_pinned_post_reports_tls_failures(tmp_path):
    cert, key = _self_signed(tmp_path, "someone-else.example.com")
    server = _TLSServer(cert, key, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    ctx = ssl.create_default_context(cafile=str(cert))
    attempt = pinned_post(_dest(server.port), ipaddress.ip_address("127.0.0.1"), {}, b"{}", ctx)
    assert attempt.tls_failure  # the certificate is for another name


def test_pinned_post_gives_up_at_the_deadline(tmp_path):
    cert, key = _self_signed(tmp_path, "receiver.example.com")
    server = _TLSServer(cert, key, None)
    ctx = ssl.create_default_context(cafile=str(cert))
    started = time.monotonic()
    attempt = pinned_post(_dest(server.port), ipaddress.ip_address("127.0.0.1"), {}, b"{}", ctx, timeout=0.5)
    assert attempt.status is None
    assert time.monotonic() - started < 3


# --- relay sender -------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakePost:
    def __init__(self, *results: Attempt) -> None:
        self.results = list(results)
        self.calls: list[tuple[Destination, str, dict, bytes]] = []

    def __call__(self, dest, address, headers, body):
        self.calls.append((dest, str(address), headers, body))
        return self.results.pop(0) if self.results else Attempt(status=200)


def _sender(tmp_path, *results: Attempt, resolver=None, **settings) -> tuple[Sender, FakePost, FakeClock]:
    post, clock = FakePost(*results), FakeClock()
    relay_settings = RelaySettings(secret=RELAY_SECRET, data_dir=tmp_path / "relay", **settings)
    sender = Sender(
        relay_settings,
        resolver=resolver or (lambda h, p: [PUBLIC_V4]),
        post=post,
        clock=clock,
        autostart=False,
    )
    return sender, post, clock


def _body(kind: str = "notice", **changes) -> str:
    common = {"event_id": EVT, "time": "2026-10-09T10:00:00Z", "handle": "nova", "instance": "ah-0123"}
    if kind == "verify":
        body = {"type": "push.verify", "code": "abc", "about": "a check", "confirm": "POST /v1/x"}
    else:
        body = {"type": "notice", "count": 1, "events": [{"event": "mail.received", "id": 7}]}
    return json.dumps(common | body | changes, separators=(",", ":"))


EVT = "evt_" + "a" * 24


def _job(n: int = 1, kind: str = "notice", url: str = "https://hooks.example.com/in") -> dict:
    return {
        "job_id": f"job_{n}",
        "kind": kind,
        "url": url,
        "body": _body(kind),
        "event_id": "evt_" + "a" * 24,
        "timestamp": "1700000000",
        "signature": "sha256=" + "b" * 64,
    }


def _outcomes(sender: Sender) -> dict[str, str]:
    return {o["job_id"]: o["outcome"] for o in sender.outcomes(0)[0]}


def test_sender_delivers_to_the_resolved_address(tmp_path):
    sender, post, _ = _sender(tmp_path)
    sender.submit([_job()])
    sender.run_due()
    assert _outcomes(sender) == {"job_1": "delivered"}
    dest, address, headers, body = post.calls[0]
    assert (dest.host, address, body) == ("hooks.example.com", PUBLIC_V4, _body().encode())
    assert headers == {
        "Content-Type": "application/json",
        "User-Agent": "agent-helper-push",
        "X-Agent-Helper-Event-Id": "evt_" + "a" * 24,
        "X-Agent-Helper-Timestamp": "1700000000",
        "X-Agent-Helper-Signature": "sha256=" + "b" * 64,
    }


def test_relay_never_sends_headers_from_jobs(tmp_path):
    client, sender = _relay_client(tmp_path)
    job = _job() | {"headers": {"Authorization": "Bearer stolen", "Host": "internal"}}
    body = json.dumps({"jobs": [job]}).encode()
    signed = _signed("POST", "/v1/relay/jobs", body)
    assert client.post("/v1/relay/jobs", content=body, headers=signed).status_code == 200
    sender.run_due()
    sent = sender.post.calls[0][2]
    assert "Authorization" not in sent and "Host" not in sent
    for bad in ({"event_id": "evt_x\r\nX: y"}, {"timestamp": "12a"}, {"signature": "sha256=zz"}):
        body = json.dumps({"jobs": [_job() | bad]}).encode()
        response = client.post("/v1/relay/jobs", content=body, headers=_signed("POST", "/v1/relay/jobs", body))
        assert response.json() == {"accepted": 0, "refused": 1}


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": EVT + "\n"},
        {"timestamp": "1700000000\n"},
        {"timestamp": "\u0661\u0662\u0663"},  # Arabic-Indic digits
        {"signature": "sha256=" + "b" * 64 + "\n"},
        {"job_id": "job\n"},
        {"body": "not json"},
        {"body": "[]"},
        {"body": _body(extra="field")},
        {"body": _body(event_id="evt_" + "c" * 24)},
        {"body": _body(type="push.verify")},
        {"body": _body(count=0)},
        {"body": _body(count=True)},
        {"body": _body(events=[{"event": "other", "id": 1}])},
        {"body": _body(events=[{"event": "mail.received", "id": 1, "x": 2}])},
        {"body": _body(handle="\u00e9")},
        {"body": _body(time="yesterday")},
        {"body": _body("verify")},  # a verification body in a notice job
        {"body": _body().replace(",", ", ")},  # padding
        {"body": _body().replace('"count":1', '"count":1,"count":2')},  # duplicate key
        {"body": _body().replace('"nova"', '"\\u006eova"')},  # another escape for the same text
        {"body": _body(count=10_001)},
    ],
)
def test_relay_accepts_only_strict_jobs(tmp_path, changes):
    client, sender = _relay_client(tmp_path)
    body = json.dumps({"jobs": [_job() | changes]}).encode()
    signed = _signed("POST", "/v1/relay/jobs", body)
    assert client.post("/v1/relay/jobs", content=body, headers=signed).json() == {"accepted": 0, "refused": 1}
    sender.run_due()
    assert sender.post.calls == []


def test_repeated_job_ids_are_all_refused(tmp_path):
    client, sender = _relay_client(tmp_path)
    jobs = [_job(1), _job(2), _job(1, url="https://b.example.org/x")]
    body = json.dumps({"jobs": jobs}).encode()
    response = client.post("/v1/relay/jobs", content=body, headers=_signed("POST", "/v1/relay/jobs", body))
    assert response.json() == {"accepted": 1, "refused": 2}
    sender.run_due()
    assert [c[0].host for c in sender.post.calls] == ["hooks.example.com"]
    outcomes = [(o["job_id"], o["outcome"]) for o in sender.outcomes(0)[0]]
    assert sorted(outcomes) == [("job_1", "refused"), ("job_1", "refused"), ("job_2", "delivered")]


def test_one_bad_job_does_not_block_the_others(tmp_path):
    client, sender = _relay_client(tmp_path)
    jobs = [_job(1), _job(2) | {"body": "padded "}, "not a job", _job(3, url="https://b.example.org/x")]
    body = json.dumps({"jobs": jobs}).encode()
    response = client.post("/v1/relay/jobs", content=body, headers=_signed("POST", "/v1/relay/jobs", body))
    assert response.json() == {"accepted": 2, "refused": 2}
    sender.run_due()
    assert _outcomes(sender) == {"job_1": "delivered", "job_2": "refused", "job_3": "delivered"}


def test_relay_accepts_every_body_the_service_builds(pushing):
    client, push, sender, clock = pushing
    store = client.app.state.store
    _verify(client, push, sender, _handle(client))
    other = _handle(client, "orbit")
    client.post("/v1/messages", json={"sender": "orbit", "to": "nova", "message": "x", "handle_token": other})
    soon = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store._db.execute("UPDATE push_subscriptions SET expires_at = ?", (soon,))
    clock.now += 700
    push.tick()
    sender.run_due()
    kinds = [json.loads(c[3])["type"] for c in sender.post.calls]
    assert sorted(kinds) == ["notice", "push.verify", "subscription.expiring"]


def test_relay_header_checks_are_strict(tmp_path):
    client, _ = _relay_client(tmp_path)
    body = json.dumps({"jobs": [_job()]}).encode()
    for key, value in (("X-Relay-Timestamp", None), ("X-Relay-Nonce", "n" * 15)):
        headers = _signed("POST", "/v1/relay/jobs", body)
        headers[key] = value if value is not None else headers[key] + " "
        assert client.post("/v1/relay/jobs", content=body, headers=headers).status_code == 401


def test_opt_out_stops_notices_too(tmp_path):
    sender, post, _ = _sender(tmp_path, Attempt(status=410))
    sender.submit([_job(1, kind="verify")])
    sender.run_due()
    sender.submit([_job(2)])
    sender.run_due()
    assert len(post.calls) == 1
    assert _outcomes(sender)["job_2"] == "opted_out"


def test_relay_calls_have_an_overall_deadline(tmp_path):
    from agent_helper.push import HttpsRelay

    cert, key = _self_signed(tmp_path, "relay.example.com")
    server = _TLSServer(cert, key, None)  # accepts, reads, never answers
    relay = HttpsRelay(f"https://relay.example.com:{server.port}", RELAY_SECRET, str(cert), timeout=0.5)
    relay.host = "127.0.0.1"  # the certificate check still uses the name below
    relay.context.check_hostname = False
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        relay.call("GET", "/v1/relay/outcomes?after=0", b"")
    assert time.monotonic() - started < 2


def test_sender_retries_after_30_s_and_3_min_then_gives_up(tmp_path):
    sender, post, clock = _sender(tmp_path, Attempt(status=503), Attempt(status=None), Attempt(status=429))
    sender.submit([_job()])
    sender.run_due()
    assert len(post.calls) == 1 and _outcomes(sender) == {}
    clock.now += 29
    sender.run_due()
    assert len(post.calls) == 1
    clock.now += 1
    sender.run_due()
    assert len(post.calls) == 2
    clock.now += 180
    sender.run_due()
    assert len(post.calls) == 3
    assert _outcomes(sender) == {"job_1": "failed"}


def test_sender_does_not_retry_other_4xx_or_tls_failures(tmp_path):
    sender, post, clock = _sender(tmp_path, Attempt(status=404), Attempt(status=None, tls_failure=True))
    sender.submit([_job(1), _job(2, url="https://other.example.org/in")])
    sender.run_due()
    clock.now += 1000
    sender.run_due()
    assert len(post.calls) == 2
    assert _outcomes(sender) == {"job_1": "failed", "job_2": "tls_failure"}


def test_rebinding_is_caught_on_every_attempt(tmp_path):
    answers = [[PUBLIC_V4], ["127.0.0.1"]]
    sender, post, clock = _sender(tmp_path, Attempt(status=503), resolver=lambda h, p: answers.pop(0))
    sender.submit([_job()])
    sender.run_due()
    clock.now += 30
    sender.run_due()
    assert len(post.calls) == 1  # the second resolution was private: no connection at all
    assert _outcomes(sender) == {"job_1": "refused"}


def test_relay_rechecks_urls_and_deny_lists(tmp_path):
    sender, post, _ = _sender(
        tmp_path,
        deny_domains=("*.own.example",),
        deny_nets=(ipaddress.ip_network("93.184.215.0/24"),),
    )
    sender.submit([_job(1, url="https://10.0.0.1/x"), _job(2, url="https://a.own.example/x"), _job(3)])
    sender.run_due()
    assert post.calls == []
    assert _outcomes(sender) == {"job_1": "refused", "job_2": "refused", "job_3": "refused"}


def test_403_does_not_opt_a_host_out(tmp_path):
    sender, post, clock = _sender(tmp_path, Attempt(status=403))
    sender.submit([_job(1, kind="verify")])
    sender.run_due()
    assert _outcomes(sender) == {"job_1": "failed"}
    clock.now += 3600
    sender.submit([_job(2, kind="verify")])
    sender.run_due()
    assert _outcomes(sender)["job_2"] == "delivered"


def test_a_verification_retry_is_not_capped(tmp_path):
    sender, post, clock = _sender(tmp_path, Attempt(status=None))
    sender.submit([_job(1, kind="verify")])
    sender.run_due()
    clock.now += 30
    sender.run_due()
    assert _outcomes(sender) == {"job_1": "delivered"}
    assert len(post.calls) == 2


def test_the_next_address_is_tried_when_one_does_not_answer(tmp_path):
    sender, post, _ = _sender(tmp_path, Attempt(status=None), resolver=lambda h, p: [PUBLIC_V6, PUBLIC_V4])
    sender.submit([_job()])
    sender.run_due()
    assert [c[1] for c in post.calls] == [PUBLIC_V6, PUBLIC_V4]
    assert _outcomes(sender) == {"job_1": "delivered"}


def test_a_name_server_that_never_answers_is_cut_off(monkeypatch):
    import agent_helper.pushcheck as pc

    monkeypatch.setattr(pc, "RESOLVE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(pc.socket, "getaddrinfo", lambda *a, **k: time.sleep(2))
    started = time.monotonic()
    with pytest.raises(DestinationRefused):
        public_addresses("slow.example.com", 443)
    assert time.monotonic() - started < 1


def test_opt_out_on_410_is_remembered_across_restarts(tmp_path):
    sender, post, clock = _sender(tmp_path, Attempt(status=410))
    sender.submit([_job(1, kind="verify")])
    sender.run_due()
    assert _outcomes(sender) == {"job_1": "opted_out"}
    again, post2, _ = _sender(tmp_path)
    again.submit([_job(2, kind="verify")])
    again.run_due()
    assert post2.calls == []
    assert _outcomes(again) == {"job_2": "opted_out"}


def test_one_verification_per_host_per_hour(tmp_path):
    sender, post, clock = _sender(tmp_path)
    sender.submit([_job(1, kind="verify"), _job(2, kind="verify")])
    sender.run_due()
    assert _outcomes(sender) == {"job_1": "delivered", "job_2": "capped"}
    clock.now += 3600
    sender.submit([_job(3, kind="verify")])
    sender.run_due()
    assert _outcomes(sender)["job_3"] == "delivered"


def test_caps_per_destination_across_names(tmp_path):
    sender, post, _ = _sender(tmp_path, per_destination_per_minute=3)
    # Different names, same address: the cap is per address (and /24, and registrable domain).
    jobs = [_job(i, url=f"https://h{i}.example{i}.org/x") for i in range(5)]
    sender.submit(jobs)
    sender.run_due()
    outcomes = _outcomes(sender)
    assert list(outcomes.values()).count("delivered") == 3
    assert list(outcomes.values()).count("capped") == 2


def test_global_cap(tmp_path):
    sender, _, _ = _sender(tmp_path, max_per_minute=2, per_destination_per_minute=100)
    sender.submit([_job(i) for i in range(4)])
    sender.run_due()
    assert list(_outcomes(sender).values()).count("capped") == 2


def test_registrable_domain():
    assert registrable_domain("a.b.example.com") == "example.com"
    assert registrable_domain("x.example.co.uk") == "example.co.uk"


# --- relay HTTP API -------------------------------------------------------------------------------


def _relay_client(tmp_path) -> tuple[TestClient, Sender]:
    sender, _, _ = _sender(tmp_path)
    app = create_relay_app(RelaySettings(secret=RELAY_SECRET, data_dir=tmp_path / "relay"), sender)
    return TestClient(app), sender


def _signed(method: str, path: str, body: bytes = b"", ts: int | None = None, secret: str = RELAY_SECRET) -> dict:
    stamp = str(int(time.time()) if ts is None else ts)
    nonce = secrets.token_hex(16)
    return {
        "X-Relay-Timestamp": stamp,
        "X-Relay-Nonce": nonce,
        "X-Relay-Signature": sign(secret, stamp, nonce, method, path, body),
    }


def test_relay_api_requires_a_fresh_valid_signature(tmp_path):
    client, sender = _relay_client(tmp_path)
    body = json.dumps({"jobs": [_job()]}).encode()
    assert client.post("/v1/relay/jobs", content=body).status_code == 401
    bad = _signed("POST", "/v1/relay/jobs", body, secret="wrong-" + "w" * 40)
    assert client.post("/v1/relay/jobs", content=body, headers=bad).status_code == 401
    old = _signed("POST", "/v1/relay/jobs", body, ts=int(time.time()) - 120)
    assert client.post("/v1/relay/jobs", content=body, headers=old).status_code == 401
    other_path = _signed("POST", "/v1/relay/outcomes", body)
    assert client.post("/v1/relay/jobs", content=body, headers=other_path).status_code == 401
    good = _signed("POST", "/v1/relay/jobs", body)
    assert client.post("/v1/relay/jobs", content=body, headers=good).json() == {"accepted": 1, "refused": 0}
    assert client.post("/v1/relay/jobs", content=body, headers=good).status_code == 401  # replay
    sender.run_due()
    out = client.get("/v1/relay/outcomes?after=0", headers=_signed("GET", "/v1/relay/outcomes?after=0"))
    assert out.json()["outcomes"][0]["outcome"] == "delivered"
    assert client.get("/v1/relay/outcomes?after=0").status_code == 401


def test_relay_api_validates_jobs(tmp_path):
    client, _ = _relay_client(tmp_path)
    for jobs in ([{"job_id": "x"}], [_job() | {"body": "x" * 2000}], [_job() | {"kind": "other"}]):
        body = json.dumps({"jobs": jobs}).encode()
        response = client.post("/v1/relay/jobs", content=body, headers=_signed("POST", "/v1/relay/jobs", body))
        assert response.json() == {"accepted": 0, "refused": 1}
    for data in ({"jobs": [_job()] * 51}, {"jobs": "x"}, []):
        body = json.dumps(data).encode()
        assert (
            client.post("/v1/relay/jobs", content=body, headers=_signed("POST", "/v1/relay/jobs", body)).status_code
            == 400
        )


def test_relay_refuses_large_bodies_before_reading_them(tmp_path):
    client, _ = _relay_client(tmp_path)
    body = b"x" * (200 * 1024)
    response = client.post("/v1/relay/jobs", content=body, headers=_signed("POST", "/v1/relay/jobs", body))
    assert response.status_code == 413
    assert client.post("/v1/relay/jobs", content=body).status_code == 401


def test_relay_needs_a_long_secret(tmp_path):
    with pytest.raises(SystemExit):
        create_relay_app(RelaySettings(secret="short", data_dir=tmp_path))  # noqa: S106


# --- service side -------------------------------------------------------------------------------


class RelayBridge:
    """Hands the service's relay calls to an in-process relay app, signing them like the real client."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.up = True

    def call(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        if not self.up:
            raise OSError("relay down")
        headers = _signed(method, path, body)
        if method == "GET":
            response = self.client.get(path, headers=headers)
        else:
            response = self.client.post(path, content=body, headers=headers)
        return response.status_code, response.content


@pytest.fixture
def pushing(make_client, tmp_path):
    client = make_client(push_mode="public", relay_url="https://relay.example.net", relay_secret=RELAY_SECRET)
    push = client.app.state.push
    push.close()  # tests drive the manager themselves
    relay_client, sender = _relay_client(tmp_path)
    push.transport = RelayBridge(relay_client)
    clock = FakeClock()
    push.clock = clock
    push._last_sweep = -1e9  # the background thread may have run once before it was stopped
    return client, push, sender, clock


def _handle(client, name: str = "nova") -> str:
    created = client.post("/v1/requests", json={"message": "hi", "handle": name}).json()
    return created["handle_token"]


def _verify(client, push, sender, token: str, name: str = "nova") -> dict:
    auth = {"Authorization": f"Bearer {token}"}
    put = client.put(f"/v1/handles/{name}/push", json={"url": "https://hooks.example.com/in"}, headers=auth)
    assert put.status_code == 200, put.text
    push.tick()
    sender.run_due()
    _, _, headers, body = sender.post.calls[-1]
    payload = json.loads(body)
    assert payload["type"] == "push.verify"
    ok = client.post(f"/v1/handles/{name}/push/verify", json={"code": payload["code"]}, headers=auth)
    assert ok.json()["status"] == "active"
    return put.json() | {"verify_headers": headers, "verify_body": body}


def test_push_is_off_by_default(client):
    token = _handle(client)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.put("/v1/handles/nova/push", json={"url": "https://h.example.com/"}, headers=auth).status_code == 404
    assert client.get("/v1/capabilities/push-notifications").status_code == 404
    assert "push" not in client.get("/llms.txt").text.lower()


def test_push_needs_a_relay(make_client):
    client = make_client(push_mode="public")
    assert client.app.state.push.enabled is False
    token = _handle(client)
    put = client.put(
        "/v1/handles/nova/push", json={"url": "https://h.example.com/"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert put.status_code == 404


def test_subscription_flow_with_signed_notices(pushing):
    client, push, sender, clock = pushing
    assert client.get("/v1/capabilities/push-notifications").json()["availability"] == "available"
    token = _handle(client)
    sub = _verify(client, push, sender, token)
    secret = sub["secret"]
    # The verification request is signed with the subscription secret and explains the opt-out.
    headers, body = sub["verify_headers"], sub["verify_body"]
    expected = notice_signature(secret, headers["X-Agent-Helper-Timestamp"], headers["X-Agent-Helper-Event-Id"], body)
    assert headers["X-Agent-Helper-Signature"] == expected
    assert "410" in json.loads(body)["about"]

    # The first message is announced at once; the next ones within a minute become one notice. Notices
    # name no sender and carry no text.
    other = _handle(client, "orbit")

    def send(text: str) -> None:
        sent = client.post(
            "/v1/messages", json={"sender": "orbit", "to": "nova", "message": text, "handle_token": other}
        )
        assert sent.status_code == 201

    def notices() -> list[dict]:
        return [json.loads(c[3]) for c in sender.post.calls if json.loads(c[3])["type"] == "notice"]

    send("first secret text")
    push.tick()
    sender.run_due()
    assert [n["count"] for n in notices()] == [1]
    send("second secret text")
    send("third secret text")
    clock.now += 30
    push.tick()
    sender.run_due()
    assert len(notices()) == 1
    clock.now += 31
    push.tick()
    sender.run_due()
    notice = notices()[-1]
    assert notice["count"] == 2 and [e["event"] for e in notice["events"]] == ["mail.received"] * 2
    raw = sender.post.calls[-1][3].decode()
    assert "secret text" not in raw and "orbit" not in raw
    assert notice["instance"] == client.get("/.well-known/agent-helper.json").json()["instance_id"]
    assert len(raw) <= 1024
    hdr = sender.post.calls[-1][2]
    mac = hmac.new(
        secret.encode(),
        f"{hdr['X-Agent-Helper-Timestamp']}.{hdr['X-Agent-Helper-Event-Id']}.".encode() + raw.encode(),
        hashlib.sha256,
    )
    assert hdr["X-Agent-Helper-Signature"] == "sha256=" + mac.hexdigest()


def test_operator_reply_and_referral_notify(pushing, admin_headers):
    client, push, sender, clock = pushing
    created = client.post("/v1/requests", json={"message": "help", "handle": "nova"}).json()
    _verify(client, push, sender, created["handle_token"])
    client.post(
        f"/admin/v1/requests/{created['id']}/replies",
        json={"message": "done", "status": "answered"},
        headers=admin_headers,
    )
    other = client.post("/v1/requests", json={"message": "need nova"}).json()
    client.post(
        f"/admin/v1/requests/{other['id']}/referrals", json={"to": "nova", "note": "ask nova"}, headers=admin_headers
    )
    clock.now += 61
    push.tick()
    sender.run_due()
    notice = json.loads(sender.post.calls[-1][3])
    assert [e["event"] for e in notice["events"]] == ["request.reply", "referral.received"]
    assert notice["events"][0]["id"] == created["id"]


def test_only_chosen_events_and_only_active_subscriptions(pushing):
    client, push, sender, clock = pushing
    token = _handle(client)
    other = _handle(client, "orbit")
    auth = {"Authorization": f"Bearer {token}"}
    client.put(
        "/v1/handles/nova/push", json={"url": "https://hooks.example.com/in", "events": ["request.reply"]}, headers=auth
    )
    client.post("/v1/messages", json={"sender": "orbit", "to": "nova", "message": "x", "handle_token": other})
    clock.now += 61
    push.tick()
    sender.run_due()
    assert [json.loads(c[3])["type"] for c in sender.post.calls] == ["push.verify"]


def test_subscription_api_refusals(pushing):
    client, push, sender, _ = pushing
    token = _handle(client)
    auth = {"Authorization": f"Bearer {token}"}
    wrong = {"Authorization": "Bearer nope"}
    url = {"url": "https://hooks.example.com/in"}
    assert client.put("/v1/handles/nova/push", json=url, headers=wrong).status_code == 404
    for bad in ("http://hooks.example.com/", "https://10.0.0.1/", "https://testserver/x", "https://a.testserver/x"):
        response = client.put("/v1/handles/nova/push", json={"url": bad}, headers=auth)
        assert response.status_code == 422, bad
    assert client.put("/v1/handles/nova/push", json=url, headers=auth).json()["status"] == "pending"
    assert "secret" not in client.get("/v1/handles/nova/push", headers=auth).json()["subscription"]
    # Five wrong codes and the code is gone; renew sends a new one.
    push.tick()
    sender.run_due()
    for _ in range(5):
        assert client.post("/v1/handles/nova/push/verify", json={"code": "guess"}, headers=auth).status_code == 422
    code = json.loads(sender.post.calls[-1][3])["code"]
    assert client.post("/v1/handles/nova/push/verify", json={"code": code}, headers=auth).status_code == 409
    assert client.post("/v1/handles/nova/push/renew", headers=auth).json()["status"] == "pending"
    assert client.delete("/v1/handles/nova/push", headers=auth).status_code == 204
    assert client.get("/v1/handles/nova/push", headers=auth).json() == {"subscription": None}


def test_allowlist_mode(make_client):
    client = make_client(
        push_mode="allowlist",
        push_allowed_domains="*.agents.example",
        relay_url="https://relay.example.net",
        relay_secret=RELAY_SECRET,
    )
    client.app.state.push.close()
    auth = {"Authorization": f"Bearer {_handle(client)}"}
    assert (
        client.put("/v1/handles/nova/push", json={"url": "https://hooks.example.com/"}, headers=auth).status_code == 422
    )
    assert (
        client.put("/v1/handles/nova/push", json={"url": "https://a.agents.example/"}, headers=auth).status_code == 200
    )


def _mailbox(client, token: str) -> list[dict]:
    return client.get("/v1/mailbox/nova", headers={"Authorization": f"Bearer {token}"}).json()["messages"]


def test_failures_suspend_with_a_mailbox_note_and_renew_reverifies(pushing):
    client, push, sender, clock = pushing
    token = _handle(client)
    _verify(client, push, sender, token)
    sub_id = client.app.state.store.list_push_admin()[0]["id"]
    for _ in range(19):
        client.app.state.store.push_outcome(sub_id, "notice", "failed", push.limits)
    assert client.app.state.store.list_push_admin()[0]["status"] == "active"
    client.app.state.store.push_outcome(sub_id, "notice", "failed", push.limits)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/handles/nova/push", headers=auth).json()["subscription"]["status"] == "suspended"
    notes = _mailbox(client, token)
    assert len(notes) == 1 and "paused" in notes[0]["body"] and "fail" not in notes[0]["body"].lower()
    assert client.post("/v1/handles/nova/push/renew", headers=auth).json()["status"] == "pending"
    before = len(sender.post.calls)
    push.tick()
    sender.run_due()
    assert len(sender.post.calls) == before  # one verification per host per hour, even for the same handle
    clock_relay = sender.clock
    clock_relay.now += 3600
    client.post("/v1/handles/nova/push/renew", headers=auth)
    push.tick()
    sender.run_due()
    assert json.loads(sender.post.calls[-1][3])["type"] == "push.verify"


def test_tls_failures_suspend_fast_and_opt_out_suspends(pushing):
    client, push, sender, _ = pushing
    store = client.app.state.store
    token = _handle(client)
    _verify(client, push, sender, token)
    sub_id = store.list_push_admin()[0]["id"]
    store.push_outcome(sub_id, "notice", "tls_failure", push.limits)
    assert store.list_push_admin()[0]["status"] == "active"
    store.push_outcome(sub_id, "notice", "tls_failure", push.limits)
    assert store.list_push_admin()[0]["status"] == "suspended"

    token2 = _handle(client, "orbit")
    auth = {"Authorization": f"Bearer {token2}"}
    client.put("/v1/handles/orbit/push", json={"url": "https://other.example.org/in"}, headers=auth)
    sender.post.results = [Attempt(status=410)]
    store = client.app.state.store
    push.tick()
    sender.run_due()
    push.tick()  # collects the outcome
    # While pending, nothing about the URL's behaviour shows; an unconfirmed subscription just expires.
    assert client.get("/v1/handles/orbit/push", headers=auth).json()["subscription"]["status"] == "pending"
    store._db.execute("UPDATE push_subscriptions SET verify_expires = 1 WHERE handle = 'orbit'")
    push._last_sweep = -1e9
    push.tick()
    assert client.get("/v1/handles/orbit/push", headers=auth).json()["subscription"]["status"] == "expired"


def test_pending_outcomes_reveal_nothing(pushing):
    client, push, sender, _ = pushing
    store = client.app.state.store
    auth = {"Authorization": f"Bearer {_handle(client)}"}
    client.put("/v1/handles/nova/push", json={"url": "https://hooks.example.com/in"}, headers=auth)
    sub_id = store.list_push_admin()[0]["id"]
    for outcome in ("refused", "tls_failure", "failed", "capped", "opted_out"):
        store.push_outcome(sub_id, "verify", outcome, push.limits)
        assert client.get("/v1/handles/nova/push", headers=auth).json()["subscription"]["status"] == "pending"
    assert _mailbox(client, auth["Authorization"][7:]) == []


def test_operator_suspension_survives_a_new_registration(pushing, admin_headers):
    client, push, sender, _ = pushing
    token = _handle(client)
    _verify(client, push, sender, token)
    sub_id = client.get("/admin/v1/push", headers=admin_headers).json()[0]["id"]
    client.post(f"/admin/v1/push/{sub_id}/suspend", headers=admin_headers)
    auth = {"Authorization": f"Bearer {token}"}
    again = client.put("/v1/handles/nova/push", json={"url": "https://elsewhere.example.org/"}, headers=auth)
    assert again.status_code == 409


def test_url_rules_are_not_shown_to_strangers(pushing):
    client, _, _, _ = pushing
    _handle(client)
    response = client.put(
        "/v1/handles/nova/push", json={"url": "https://10.0.0.1/"}, headers={"Authorization": "Bearer x"}
    )
    assert response.status_code == 404


def test_policy_refusals_look_the_same():
    messages = set()
    for url, kw in (
        ("https://own.example/x", {"deny_domains": ["own.example"]}),
        ("https://x.example/x", {"allow_domains": ["agents.example"]}),
        ("https://printer.local/x", {}),
    ):
        with pytest.raises(DestinationRefused) as exc:
            check_url(url, **kw)
        messages.add(str(exc.value))
    assert len(messages) == 1


@pytest.mark.parametrize("dot", ["。", "．", "｡"])
def test_unicode_trailing_dots_do_not_bypass_deny_lists(dot):
    with pytest.raises(DestinationRefused):
        check_url(f"https://own.example{dot}/x", deny_domains=["*.own.example"])
    with pytest.raises(DestinationRefused):
        check_url(f"https://printer.local{dot}/x")
    with pytest.raises(DestinationRefused):
        check_url(f"https://1.2.3.4{dot}/x")


def test_relay_outage_keeps_jobs_briefly(pushing):
    client, push, sender, clock = pushing
    token = _handle(client)
    push.transport.up = False
    client.put(
        "/v1/handles/nova/push",
        json={"url": "https://hooks.example.com/in"},
        headers={"Authorization": f"Bearer {token}"},
    )
    push.tick()
    assert sender.post.calls == []
    push.transport.up = True
    clock.now += 60
    push.tick()
    sender.run_due()
    assert json.loads(sender.post.calls[-1][3])["type"] == "push.verify"


def test_expiry_reminder_and_expiry(pushing):
    client, push, sender, clock = pushing
    store = client.app.state.store
    _verify(client, push, sender, _handle(client))
    soon = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store._db.execute("UPDATE push_subscriptions SET expires_at = ?", (soon,))
    clock.now += 700
    push.tick()
    sender.run_due()
    assert json.loads(sender.post.calls[-1][3])["type"] == "subscription.expiring"
    store._db.execute("UPDATE push_subscriptions SET expires_at = '2000-01-01T00:00:00Z'")
    clock.now += 700
    push.tick()
    assert store.list_push_admin()[0]["status"] == "expired"


def test_admin_can_list_suspend_and_delete(pushing, admin_headers):
    client, push, sender, _ = pushing
    token = _handle(client)
    _verify(client, push, sender, token)
    listed = client.get("/admin/v1/push", headers=admin_headers).json()
    assert listed[0]["host"] == "hooks.example.com"
    sub_id = listed[0]["id"]
    assert client.post(f"/admin/v1/push/{sub_id}/suspend", headers=admin_headers).json() == {"suspended": True}
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/handles/nova/push/renew", headers=auth).status_code == 409
    assert client.delete(f"/admin/v1/push/{sub_id}", headers=admin_headers).status_code == 204
    assert client.get("/admin/v1/push").status_code == 401
