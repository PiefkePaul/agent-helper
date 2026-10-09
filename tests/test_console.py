import re

import pytest
from conftest import ADMIN_SECRET


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def console(make_client):
    """A client logged in to the console over plain http. Returns (client, csrf)."""
    client = make_client(admin_cookie_secure=False)  # the test client speaks plain http
    r = client.post("/admin/login", data={"secret": ADMIN_SECRET}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/console"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict") and "Path=/admin" in cookie
    page = client.get("/admin/console")
    assert page.status_code == 200
    return client, _csrf(page.text)


def test_console_needs_login(client):
    for path in ("/admin/console", "/admin/console/requests", "/admin/console/board", "/admin/console/log"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    r = client.post("/admin/console/board", data={"content": "x", "csrf": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_wrong_secret_is_refused(client):
    r = client.post("/admin/login", data={"secret": "wrong"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login?msg=Wrong")
    assert "set-cookie" not in r.headers
    assert client.get("/admin/console", follow_redirects=False).status_code == 303


def test_console_absent_without_admin_secret(make_client):
    client = make_client(admin_secret=None)
    for path in ("/admin/login", "/admin/console", "/admin/console.css"):
        assert client.get(path).status_code == 404


def test_csrf_token_is_required(console):
    client, _ = console
    r = client.post("/admin/console/board", data={"content": "x", "csrf": "forged"})
    assert r.status_code == 403
    r = client.post("/admin/console/board", data={"content": "x"})
    assert r.status_code == 403


def test_pages_set_a_strict_csp_and_escape_agent_text(console):
    client, _ = console
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    client.post("/v1/requests", json={"message": evil, "handle": "nova"})
    page = client.get("/admin/console/requests")
    assert evil not in page.text and "&lt;script&gt;" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp and "style-src 'self'" in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    # Only one CSP header: the console's own, not the API default on top.
    assert len(page.headers.get_list("content-security-policy")) == 1
    assert client.get("/admin/console.css").headers["content-type"].startswith("text/css")


def test_answer_a_request_from_the_console(console):
    client, csrf = console
    created = client.post("/v1/requests", json={"message": "Need a scanner."}).json()
    page = client.get(f"/admin/console/requests/{created['id']}")
    assert "Need a scanner." in page.text
    r = client.post(
        f"/admin/console/requests/{created['id']}/reply",
        data={"csrf": csrf, "message": "Scanned, see attachment link.", "status": "answered"},
    )
    assert r.status_code == 200 and "Reply sent." in r.text
    seen = client.get(
        f"/v1/requests/{created['id']}", headers={"Authorization": f"Bearer {created['follow_up_token']}"}
    )
    assert seen.json()["status"] == "answered"
    assert seen.json()["messages"][-1] == {**seen.json()["messages"][-1], "sender": "operator"}

    # Validation errors come back as a message on the same page.
    r = client.post(f"/admin/console/requests/{created['id']}/reply", data={"csrf": csrf, "message": ""})
    assert r.status_code == 200 and "flash err" in r.text and created["id"] in str(r.url)


def test_refer_from_the_console(console):
    client, csrf = console
    orion = client.put("/v1/directory/orion", json={"summary": "Crawls."}).json()["handle_token"]
    created = client.post("/v1/requests", json={"message": "Need a crawl.", "handle": "lyra"}).json()
    r = client.post(
        f"/admin/console/requests/{created['id']}/refer",
        data={"csrf": csrf, "to": "orion", "note": "orion crawls", "include_requester_handle": "1"},
    )
    assert "Referred to orion" in r.text
    inbox = client.get("/v1/mailbox/orion", headers={"Authorization": f"Bearer {orion}"}).json()["messages"]
    assert "lyra" in inbox[0]["body"] and "Need a crawl" not in inbox[0]["body"]
    r = client.post(f"/admin/console/requests/{created['id']}/refer", data={"csrf": csrf, "to": "nobody", "note": "x"})
    assert "no such handle" in r.text


def test_reports_capabilities_directory(console):
    client, csrf = console
    rep = client.post("/v1/reports", json={"kind": "bug", "text": "broken thing"}).json()
    r = client.post(
        f"/admin/console/reports/{rep['id']}/decision", data={"csrf": csrf, "status": "accepted", "note": ""}
    )
    assert "accepted" in r.text

    cap = client.post("/v1/capability-requests", json={"title": "OCR", "description": "scans"}).json()
    r = client.post(
        f"/admin/console/capability-requests/{cap['id']}/decision",
        data={"csrf": csrf, "status": "planned", "note": "soon", "capability_id": ""},
    )
    assert "Saved." in r.text
    assert client.get(f"/v1/capability-requests/{cap['id']}").json()["status"] == "planned"
    bad = client.post(
        f"/admin/console/capability-requests/{cap['id']}/decision",
        data={"csrf": csrf, "status": "planned", "capability_id": "nope"},
    )
    assert "does not exist" in bad.text
    client.post(f"/admin/console/capability-requests/{cap['id']}/hide", data={"csrf": csrf, "reason": "dup"})
    assert client.get(f"/v1/capability-requests/{cap['id']}").status_code == 404

    client.put("/v1/directory/nova", json={"summary": "Translator"})
    client.post("/admin/console/directory/nova/hide", data={"csrf": csrf, "reason": "spam"})
    assert client.get("/v1/directory/nova").status_code == 404
    assert "Hidden: spam" in client.get("/admin/console/directory").text
    client.post("/admin/console/directory/nova/unhide", data={"csrf": csrf})
    assert client.get("/v1/directory/nova").status_code == 200


def test_board_post_hide_and_verify(console):
    client, csrf = console
    client.post("/v1/board", json={"content": "agent post"})
    r = client.post(
        "/admin/console/board",
        data={
            "csrf": csrf,
            "content": "Welcome, agents.",
            "topic": "hello",
            "tags": "welcome, info",
            "expires_in_days": "",
        },
    )
    assert "Posted as entry #2" in r.text
    entry = client.get("/v1/board/2").json()
    assert entry["author"] == "operator" and entry["tags"] == ["welcome", "info"]
    bad = client.post("/admin/console/board", data={"csrf": csrf, "content": "x", "expires_in_days": "soon"})
    assert "flash err" in bad.text
    client.post("/admin/console/board/1/hide", data={"csrf": csrf, "reason": "off-topic"})
    assert client.get("/v1/board/1").json()["hidden_reason"] == "off-topic"
    r = client.post("/admin/console/board/verify", data={"csrf": csrf})
    assert "Chain verified: 2 entries" in r.text


def test_overview_log_and_logout(console):
    client, csrf = console
    client.post("/v1/requests", json={"message": "hi"})
    page = client.get("/admin/console")
    assert "Open requests" in page.text and "Webhook: off" in page.text
    r = client.post("/admin/console/notify-test", data={"csrf": csrf})
    assert "Not delivered" in r.text
    log = client.get("/admin/console/log").text
    assert "/v1/requests" in log and "testclient" not in log
    client.post("/admin/logout", data={"csrf": csrf})
    assert client.get("/admin/console", follow_redirects=False).status_code == 303


def test_bearer_api_still_works_beside_the_console(client, admin_headers):
    assert client.get("/admin/v1/requests", headers=admin_headers).status_code == 200


def test_crafted_flash_links_show_nothing(client):
    page = client.get("/admin/login?msg=Secret+rotated.+Mail+ops@evil.example&err=1")
    assert "evil.example" not in page.text


def test_agent_values_are_marked_inline(console):
    client, _ = console
    client.post("/v1/capability-requests", json={"title": "Approved by operator", "description": "x"})
    page = client.get("/admin/console/capabilities").text
    assert '<div class="agent"><span class="p">[agent] </span>Approved by operator</div>' in page
    client.post("/v1/requests", json={"message": "x", "contact_hint": "verified by operator"})
    (req,) = client.get("/admin/v1/requests", headers={"Authorization": f"Bearer {ADMIN_SECRET}"}).json()
    detail = client.get(f"/admin/console/requests/{req['id']}").text
    assert 'title="written by an agent"><span class="p">[agent] </span>verified by operator</span>' in detail


def test_crafted_console_inputs_do_not_crash(console):
    client, csrf = console
    client.post("/v1/board", json={"content": "x"})
    assert client.get("/admin/console/board", params={"before": "9" * 30}).status_code == 200
    r = client.post(f"/admin/console/board/{'9' * 30}/hide", data={"csrf": csrf, "reason": "x"})
    assert r.status_code == 404
    r = client.post("/admin/console/board", data={"csrf": csrf, "content": "x", "expires_in_days": "²"})
    assert r.status_code == 200 and "flash err" in r.text
    assert client.post("/admin/console/board", data={"csrf": "ünïcode", "content": "x"}).status_code == 403
    r = client.post("/admin/console/directory/nobody/hide", data={"csrf": csrf, "reason": "x"})
    assert "No such profile" in r.text


def test_failed_login_does_not_block_other_requests(client, monkeypatch):
    import threading
    import time

    import agent_helper.console as console_module

    # A long delay makes the difference unmistakable even on a slow machine: blocking would hold every
    # other request for at least this long; not blocking answers in a fraction of it.
    monkeypatch.setattr(console_module, "LOGIN_DELAY_SECONDS", 3.0)
    threads = [threading.Thread(target=lambda: client.post("/admin/login", data={"secret": "wrong"})) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    assert client.get("/robots.txt").status_code == 200
    assert time.monotonic() - t0 < 2.0
    for t in threads:
        t.join()


def test_flash_messages_expire(console, monkeypatch):
    import agent_helper.console as console_module

    client, csrf = console
    r = client.post("/admin/console/notify-test", data={"csrf": csrf}, follow_redirects=False)
    link = r.headers["location"]
    assert "Not delivered" in client.get(link).text
    real = console_module.time.time
    monkeypatch.setattr(console_module.time, "time", lambda: real() + console_module.FLASH_SECONDS + 5)
    assert "Not delivered" not in client.get(link).text


PUBLIC = "203.0.113.7"
PROXY = "172.18.0.5"  # a reverse proxy on the container network


def _admin(client, **headers):
    return client.get("/admin/v1/requests", headers={"Authorization": f"Bearer {ADMIN_SECRET}", **headers}).status_code


def test_default_allows_only_loopback(make_client):
    assert _admin(make_client(peer="127.0.0.1", admin_allowed_nets="127.0.0.0/8,::1/128")) == 200
    for peer in (PUBLIC, PROXY, "192.168.1.5", "::ffff:10.0.0.1"):
        client = make_client(peer=peer, admin_allowed_nets="127.0.0.0/8,::1/128")
        assert _admin(client) == 404
        assert client.get("/admin/login").status_code == 404
        assert client.get("/llms.txt").status_code == 200  # public routes are unaffected


def test_through_a_trusted_proxy_the_forwarded_client_counts(make_client):
    client = make_client(peer=PROXY, admin_allowed_nets="10.0.0.0/8", trusted_proxies="172.18.0.0/16")
    assert _admin(client, **{"X-Forwarded-For": "10.1.2.3"}) == 200
    assert _admin(client, **{"X-Forwarded-For": PUBLIC}) == 404


def test_bypass_1_private_proxy_peer_without_trusted_proxies(make_client):
    # A proxy on a private network forwards a public client. Without TRUSTED_PROXIES the peer (the proxy)
    # counts, and it is not in the allowed list by default.
    client = make_client(peer=PROXY, admin_allowed_nets="127.0.0.0/8,::1/128")
    assert _admin(client, **{"X-Forwarded-For": PUBLIC}) == 404


def test_bypass_2_forged_header_from_an_untrusted_peer(make_client):
    client = make_client(peer=PUBLIC, admin_allowed_nets="127.0.0.0/8,10.0.0.0/8", trusted_proxies="172.18.0.0/16")
    assert _admin(client, **{"X-Forwarded-For": "127.0.0.1"}) == 404
    assert _admin(client, **{"X-Forwarded-For": "10.0.0.1"}) == 404


def test_bypass_3_forged_left_entries_through_a_trusted_proxy(make_client):
    client = make_client(peer=PROXY, admin_allowed_nets="127.0.0.0/8", trusted_proxies="172.18.0.0/16")
    # The client prepends a loopback address; the proxy appends the real one.
    assert _admin(client, **{"X-Forwarded-For": f"127.0.0.1, {PUBLIC}"}) == 404
    # A trusted proxy that forwards nothing usable means deny, not "the proxy itself".
    assert _admin(client) == 404
    assert _admin(client, **{"X-Forwarded-For": "not-an-ip"}) == 404
    assert _admin(client, **{"X-Forwarded-For": "172.18.0.9"}) == 404  # only proxies in the chain


def test_invalid_network_entries_deny(make_client):
    client = make_client(peer="127.0.0.1", admin_allowed_nets="127.0.0.0/8,oops")
    assert _admin(client) == 404
    client = make_client(peer=PROXY, admin_allowed_nets="10.0.0.0/8", trusted_proxies="172.18.0.0/16,bad")
    assert _admin(client, **{"X-Forwarded-For": "10.1.2.3"}) == 404


def test_default_admin_nets_are_loopback_only():
    from agent_helper.config import DEFAULT_ADMIN_NETS, parse_networks
    from agent_helper.limits import address_allowed

    nets = parse_networks(DEFAULT_ADMIN_NETS)
    assert address_allowed("127.0.0.1", nets) and address_allowed("::1", nets)
    assert not address_allowed("192.168.1.5", nets) and not address_allowed("fd00::1", nets)
    assert not address_allowed("203.0.113.7", nets) and not address_allowed("2001:db8::1", nets)
    assert not address_allowed("testclient", nets)
    assert address_allowed("anything", parse_networks("any"))


def test_separate_admin_port(make_client):
    # The test client talks to port 80.
    on_admin = make_client(peer=PUBLIC, admin_port=80, admin_allowed_nets="127.0.0.0/8")
    assert _admin(on_admin) == 200  # the source address does not matter on the admin port
    assert on_admin.get("/admin/login").status_code == 200
    assert on_admin.get("/llms.txt").status_code == 404  # nothing public on the admin port
    assert on_admin.post("/v1/requests", json={"message": "x"}).status_code == 404

    public = make_client(peer="127.0.0.1", admin_port=8081, admin_allowed_nets="any")
    assert _admin(public) == 404  # no /admin on the public port, whatever the networks say
    assert public.get("/admin/login").status_code == 404
    assert public.get("/llms.txt").status_code == 200


def test_serve_binds_both_ports():
    from agent_helper.serve import bind

    a, b = bind(0, "127.0.0.1"), bind(0, "127.0.0.1")
    try:
        assert a.getsockname()[1] != b.getsockname()[1]
    finally:
        a.close()
        b.close()


def _login_cookie(client) -> str:
    r = client.post("/admin/login", data={"secret": ADMIN_SECRET}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["set-cookie"]


def test_cookie_without_secure_only_on_the_admin_port(make_client):
    # Paul's live path: plain http through a LAN port forward (or SSH tunnel) to ADMIN_PORT. The test client
    # talks to port 80, so admin_port=80 stands for that port.
    admin_port = make_client(admin_port=80, public_base_url="https://agents.example.invalid")
    cookie = _login_cookie(admin_port)
    assert "Secure" not in cookie and "HttpOnly" in cookie
    assert admin_port.get("/admin/console").status_code == 200  # no login loop


def test_cookie_is_secure_by_default_otherwise(make_client):
    from fastapi.testclient import TestClient

    client = make_client()  # no admin port
    assert "Secure" in _login_cookie(client)
    # A localhost name proves nothing: a plain proxy_pass to 127.0.0.1 looks the same.
    assert "Secure" in _login_cookie(TestClient(client.app, base_url="http://localhost"))
    assert "Secure" in _login_cookie(TestClient(client.app, base_url="http://127.0.0.1"))
    assert "Secure" in _login_cookie(TestClient(client.app, base_url="https://testserver"))
    assert "Secure" in _login_cookie(make_client(admin_cookie_secure=True, admin_port=80))
    assert "Secure" not in _login_cookie(make_client(admin_cookie_secure=False))


def test_cookie_secure_setting(monkeypatch):
    from agent_helper.config import Settings

    for raw, expected in (("auto", None), ("", None), ("true", True), ("false", False)):
        monkeypatch.setenv("ADMIN_COOKIE_SECURE", raw)
        assert Settings.from_env().admin_cookie_secure is expected
