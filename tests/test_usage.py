import pytest
from conftest import ADMIN_SECRET
from test_mcp import legacy, modern

from agent_helper.usage import KNOWN_MCP_CLIENTS, Usage, client_family, endpoint_of, normalize_client_name


def counts(client, admin_headers, days=1) -> dict[tuple[str, str], int]:
    r = client.get(f"/admin/v1/usage?days={days}", headers=admin_headers)
    assert r.status_code == 200, r.text
    return {(row["metric"], row["family"]): row["count"] for row in r.json()["rows"]}


def scope(path, method="GET", headers=None):
    return {"type": "http", "path": path, "method": method, "headers": headers or []}


# --- classification -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ua", "family"),
    [
        ("Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)", "ai_crawler"),
        ("Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)", "ai_crawler"),
        ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0", "ai_agent"),
        ("Claude-User/1.0", "ai_agent"),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "search_crawler"),
        ("Mozilla/5.0 (compatible; bingbot/2.0)", "search_crawler"),
        ("Uptime-Kuma/1.23", "monitor"),
        ("python-httpx/0.27.0", "http_library"),
        ("curl/8.5.0", "http_library"),
        ("node", "http_library"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/131.0", "browser"),
        ("SomethingCrawler/0.1", "other_bot"),
        ("weird", "other_bot"),
        ("", "none"),
        ("   ", "none"),
    ],
)  # fmt: skip
def test_client_family(ua, family):
    assert client_family(ua) == family


def test_endpoint_of():
    assert endpoint_of(scope("/admin/console"), 200) is None
    assert endpoint_of(scope("/admin"), 404) is None
    assert endpoint_of(scope("/healthz"), 200) is None
    assert endpoint_of(scope("/.well-known/ai-plugin.json"), 404) == "probe:ai-plugin.json"
    assert endpoint_of(scope("/wp-login.php"), 404) == "not_found"
    assert endpoint_of(scope("/", headers=[(b"accept", b"text/html")]), 200) == "landing:html"
    assert endpoint_of(scope("/"), 200) == "landing:text"
    assert endpoint_of(scope("/llms.txt"), 200) == "llms.txt"
    assert endpoint_of(scope("/mcp", "POST"), 200) == "mcp:post"
    assert endpoint_of(scope("/a2a", "POST"), 400) == "a2a:post"
    known = frozenset({"requests"})
    assert endpoint_of(scope("/v1/requests/abc", "POST"), 201, known) == "v1:requests:post"
    # Only GET/HEAD 404s are probes.
    assert endpoint_of(scope("/v1/requests/abc", "POST"), 404, known) == "v1:requests:post"
    assert endpoint_of(scope("/v1/zz0", "POST"), 404, known) == "v1:other"
    assert endpoint_of(scope("/v1/requests", "FROBNICATE"), 405, known) == "v1:requests:other"
    assert endpoint_of(scope("/v1/Weird!", "GET"), 200, known) == "v1:other"
    assert endpoint_of(scope("/mcp", "FROBNICATE"), 405) == "mcp:other"
    assert endpoint_of(scope("/whatever", "POST"), 405) == "other"


def test_only_known_client_names_are_kept():
    assert normalize_client_name("Claude Desktop") == "claude-desktop"
    assert normalize_client_name("claude-code/2.1") == "claude-code"
    assert normalize_client_name("Cursor") == "cursor"
    assert normalize_client_name("my-agent-handle-1234") == "other"
    assert normalize_client_name("cursorish") == "other"
    assert normalize_client_name("---") is None
    assert normalize_client_name(42) is None
    assert normalize_client_name(None) is None


def test_client_names_stay_bounded():
    usage = Usage()
    for i in range(500):
        usage.record_client_name("mcp", f"client-{i}")
    usage.record_client_name("mcp", "Claude Desktop")
    rows = usage.take()
    names = {family for _, metric, family, _ in rows if metric == "mcp:client"}
    assert names == {"other", "claude-desktop"}
    assert all(normalize_client_name(k) == k for k in KNOWN_MCP_CLIENTS)
    assert usage.take() == []


def test_callers_cannot_invent_metrics(client, admin_headers):
    for i in range(30):
        client.post(f"/v1/zz{i}", json={})
    client.request("FROBNICATE", "/v1/board")
    client.request("FROBNICATE", "/mcp")
    metrics = {metric for metric, _ in counts(client, admin_headers)}
    assert not any("zz" in m or "frobnicate" in m for m in metrics)
    assert "v1:other" in metrics


def test_disabled_usage_counts_nothing():
    usage = Usage(enabled=False)
    usage.count("x")
    usage.record_http(scope("/llms.txt"), 200)
    usage.record_client_name("mcp", "client")
    assert usage.take() == []


# --- through the app -------------------------------------------------------------------------------


def test_requests_are_counted_by_kind_only(client, admin_headers):
    client.get("/llms.txt", headers={"User-Agent": "GPTBot/1.2"})
    client.get("/llms.txt", headers={"User-Agent": "GPTBot/1.2"})
    client.get("/.well-known/security.txt", headers={"User-Agent": "python-httpx/0.27"})
    client.get("/", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 Firefox/131.0"})
    client.get("/healthz")
    c = counts(client, admin_headers)
    assert c[("llms.txt", "ai_crawler")] == 2
    assert c[("probe:security.txt", "http_library")] == 1
    assert c[("landing:html", "browser")] == 1
    assert not any(metric.startswith(("admin", "/admin")) or metric == "healthz" for metric, _ in c)
    # Nothing that identifies a client is stored: no address, no full User-Agent.
    store = client.app.state.store
    dump = "\n".join(store._db.iterdump())
    assert "GPTBot/1.2" not in dump and "Firefox/131.0" not in dump


def test_usage_survives_a_restart(make_client, admin_headers, tmp_path):
    client = make_client()
    client.get("/robots.txt", headers={"User-Agent": "bingbot/2.0"})
    client.__exit__(None, None, None)  # shutdown flushes
    again = make_client()
    assert counts(again, admin_headers)[("robots.txt", "search_crawler")] == 1


def test_mcp_client_names_and_tools_are_counted(client, admin_headers):
    modern(client, "server/discover")
    legacy(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Some Client", "version": "1"}},
    )
    modern(client, "tools/call", {"name": "describe_need", "arguments": {}})
    modern(client, "tools/call", {"name": "no_such_tool", "arguments": {}})
    c = counts(client, admin_headers)
    assert c[("mcp:client", "other")] == 2  # "test" and "Some Client" are not known client names
    assert c[("mcp:tool:describe_need", "")] == 1
    assert not any(metric == "mcp:tool:no_such_tool" for metric, _ in c)


def test_a2a_methods_and_version_refusals_are_counted(client, admin_headers):
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}
    client.post("/a2a", json=body, headers={"A2A-Version": "1.0"})
    client.post("/a2a", json={**body, "method": "tasks/get"})  # A2A 0.3, served
    client.post("/a2a", json=body)  # a 1.0 method without the 1.0 header: served as 1.0, counted as mixed
    client.post("/a2a", json=body, headers={"A2A-Version": "2.0"})
    c = counts(client, admin_headers)
    assert c[("a2a:method:GetTask", "")] == 3
    assert c[("a2a:dialect:0.3", "")] == 1
    assert c[("a2a:dialect:mixed", "")] == 1
    assert c[("a2a:version-refused:other", "")] == 1
    assert c[("a2a:task-not-found:no-token", "")] == 3


def test_usage_stats_off(make_client, admin_headers):
    client = make_client(usage_stats=False)
    client.get("/llms.txt")
    assert counts(client, admin_headers) == {}


def test_usage_needs_admin(client):
    assert client.get("/admin/v1/usage").status_code == 401
    assert client.get("/admin/v1/usage?days=0", headers={"Authorization": f"Bearer {ADMIN_SECRET}"}).status_code == 422


def test_console_usage_page(make_client):
    client = make_client(admin_cookie_secure=False)
    client.post("/admin/login", data={"secret": ADMIN_SECRET}, follow_redirects=False)
    client.get("/llms.txt", headers={"User-Agent": "ClaudeBot/1.0"})
    legacy(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Cursor", "version": "1"}},
    )
    page = client.get("/admin/console/usage?days=7")
    assert page.status_code == 200
    assert "llms.txt" in page.text and "ai_crawler" in page.text and "cursor" in page.text


# --- IndexNow ---------------------------------------------------------------------------------------


def test_indexnow_key_file(make_client):
    assert make_client().get("/abcdef0123456789.txt").status_code == 404
    client = make_client(indexnow_key="abcdef0123456789")
    r = client.get("/abcdef0123456789.txt")
    assert r.status_code == 200 and r.text == "abcdef0123456789"
    assert r.headers["content-type"].startswith("text/plain")


def test_indexnow_key_is_validated(monkeypatch):
    from agent_helper.config import Settings

    monkeypatch.setenv("INDEXNOW_KEY", "short")
    with pytest.raises(SystemExit):
        Settings.from_env()
    monkeypatch.setenv("INDEXNOW_KEY", "../../etc/passwd-xxxx")
    with pytest.raises(SystemExit):
        Settings.from_env()
    monkeypatch.setenv("INDEXNOW_KEY", " abcdef0123456789 ")
    assert Settings.from_env().indexnow_key == "abcdef0123456789"


# --- refused calls (docs/decisions/0025) -----------------------------------------------------------


def test_refused_calls_are_counted_by_protocol_method_and_reason(client, admin_headers):
    client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}},
        headers={"A2A-Version": "9.9"},
    )  # a version not served
    client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}},
        headers={"A2A-Version": "1.0"},
    )  # no token: task not found
    client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "made-up/thing <script>", "params": {}},
        headers={"A2A-Version": "9.9"},
    )
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    client.post("/v1/requests", json={"message": ""})  # 422
    client.get("/v1/requests/req_x", headers={"Authorization": "Bearer wrong"})  # 404
    c = counts(client, admin_headers)
    assert c[("refused:a2a:GetTask:version_not_supported", "")] == 1
    assert c[("refused:a2a:GetTask:task_not_found", "")] == 1
    assert c[("refused:a2a:unknown:version_not_supported", "")] == 1  # an invented method name is not kept
    assert any(m.startswith("refused:mcp:tools/call:") for m, _ in c)
    assert sum(n for (m, _), n in c.items() if m == "refused:http:v1:requests:post:422") == 1
    assert any(m.startswith("refused:http:v1:requests:get:404") for m, _ in c)
    assert not any("script" in m for m, _ in c)


def test_daily_refusal_summary_goes_out_once_and_only_if_needed(make_client, admin_headers):
    import json

    from test_notify import WEBHOOK, Recorder

    client = make_client(notify_webhook_url=WEBHOOK)
    notifier = client.app.state.notifier
    rec = Recorder()
    notifier.transport = rec
    store = client.app.state.store
    yesterday = __import__("datetime").datetime.now(__import__("datetime").UTC) - __import__("datetime").timedelta(
        days=1
    )
    day = yesterday.strftime("%Y-%m-%d")
    store.add_usage(
        [
            (day, "refused:a2a:SendMessage:version_not_supported", "", 37),
            (day, "refused:http:v1:requests:post:422", "http_library", 2),
            (day, "llms.txt", "ai_crawler", 5),
        ]
    )
    client.app.state.send_refusal_digest()
    client.app.state.send_refusal_digest()  # the same day again: nothing
    assert notifier.flush()
    (event,) = rec.events
    assert event["event"] == "refusals.daily" and event["total"] == 39 and event["day"] == day
    assert event["text"].startswith(f"39 call(s) were refused on {day}: 37x a2a:SendMessage:version_not_supported")
    assert all("llms" not in json.dumps(t) for t in event["top"])


def test_no_summary_without_refusals_or_when_turned_off(make_client):
    from test_notify import WEBHOOK, Recorder

    for overrides in ({}, {"notify_refusal_digest": False}):
        client = make_client(notify_webhook_url=WEBHOOK, **overrides)
        rec = Recorder()
        client.app.state.notifier.transport = rec
        client.app.state.send_refusal_digest()
        assert client.app.state.notifier.flush()
        assert rec.events == []
