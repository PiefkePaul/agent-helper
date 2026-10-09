import re

import pytest
from conftest import ADMIN_SECRET


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def console(client):
    """A client logged in to the console. Returns (client, csrf)."""
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


def test_failed_login_does_not_block_other_requests(client):
    import threading
    import time

    started = time.monotonic()
    threads = [threading.Thread(target=lambda: client.post("/admin/login", data={"secret": "wrong"})) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    assert client.get("/robots.txt").status_code == 200
    assert time.monotonic() - t0 < 0.4
    for t in threads:
        t.join()
    assert time.monotonic() - started < 2


def test_flash_messages_expire(console, monkeypatch):
    import agent_helper.console as console_module

    client, csrf = console
    r = client.post("/admin/console/notify-test", data={"csrf": csrf}, follow_redirects=False)
    link = r.headers["location"]
    assert "Not delivered" in client.get(link).text
    real = console_module.time.time
    monkeypatch.setattr(console_module.time, "time", lambda: real() + console_module.FLASH_SECONDS + 5)
    assert "Not delivered" not in client.get(link).text


def test_admin_is_limited_to_allowed_networks(make_client):
    client = make_client(admin_allowed_nets="10.0.0.0/8", trust_proxy_headers=True)
    auth = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    outside = {"X-Forwarded-For": "203.0.113.7"}
    inside = {"X-Forwarded-For": "10.1.2.3"}
    assert client.get("/admin/v1/requests", headers={**auth, **outside}).status_code == 404
    assert client.get("/admin/login", headers=outside).status_code == 404
    assert client.get("/admin/v1/requests", headers={**auth, **inside}).status_code == 200
    assert client.get("/admin/login", headers=inside).status_code == 200
    # Public routes are unaffected.
    assert client.get("/llms.txt", headers=outside).status_code == 200


def test_default_admin_nets_are_private_only():
    from agent_helper.config import DEFAULT_ADMIN_NETS, parse_networks
    from agent_helper.limits import address_allowed

    nets = parse_networks(DEFAULT_ADMIN_NETS)
    assert address_allowed("127.0.0.1", nets) and address_allowed("192.168.1.5", nets)
    assert address_allowed("::ffff:10.0.0.1", nets) and address_allowed("fd00::1", nets)
    assert not address_allowed("203.0.113.7", nets) and not address_allowed("2001:db8::1", nets)
    assert not address_allowed("testclient", nets)
    assert address_allowed("anything", parse_networks("any"))
