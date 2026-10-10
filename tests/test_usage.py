import pytest
from conftest import ADMIN_SECRET
from test_mcp import legacy, modern

from agent_helper.usage import MAX_CLIENT_NAMES_PER_FLUSH, Usage, client_family, endpoint_of, normalize_client_name


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
    assert endpoint_of(scope("/v1/requests/abc", "POST"), 201) == "v1:requests:post"
    assert endpoint_of(scope("/v1/requests/abc", "POST"), 404) == "v1:requests:post"  # only GET/HEAD 404s are probes
    assert endpoint_of(scope("/whatever", "POST"), 405) == "other"


def test_client_names_are_cut_down():
    assert normalize_client_name("  Claude Desktop <script> ") == "claude-desktop-script"
    assert normalize_client_name("x" * 100) == "x" * 32
    assert normalize_client_name("---") is None
    assert normalize_client_name(42) is None
    assert normalize_client_name(None) is None


def test_self_reported_client_names_are_capped():
    usage = Usage()
    for i in range(MAX_CLIENT_NAMES_PER_FLUSH + 20):
        usage.record_client_name("mcp", f"client-{i}")
    rows = usage.take()
    names = {family for _, metric, family, _ in rows if metric == "mcp:client"}
    assert len(names) == MAX_CLIENT_NAMES_PER_FLUSH + 1 and "other" in names
    assert sum(n for *_, n in rows) == MAX_CLIENT_NAMES_PER_FLUSH + 20
    assert usage.take() == []


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
    client.get("/.well-known/ai-plugin.json", headers={"User-Agent": "python-httpx/0.27"})
    client.get("/", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 Firefox/131.0"})
    client.get("/healthz")
    c = counts(client, admin_headers)
    assert c[("llms.txt", "ai_crawler")] == 2
    assert c[("probe:ai-plugin.json", "http_library")] == 1
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
    assert c[("mcp:client", "test")] == 1
    assert c[("mcp:client", "some-client")] == 1
    assert c[("mcp:tool:describe_need", "")] == 1
    assert not any(metric == "mcp:tool:no_such_tool" for metric, _ in c)


def test_a2a_methods_and_version_refusals_are_counted(client, admin_headers):
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}
    client.post("/a2a", json=body, headers={"A2A-Version": "1.0"})
    client.post("/a2a", json={**body, "method": "message/send"})
    client.post("/a2a", json=body, headers={"A2A-Version": "0.2"})
    c = counts(client, admin_headers)
    assert c[("a2a:method:GetTask", "")] == 1
    assert c[("a2a:version-refused:0.3", "")] == 1
    assert c[("a2a:version-refused:other", "")] == 1


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
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Odd Name", "version": "1"}},
    )
    page = client.get("/admin/console/usage?days=7")
    assert page.status_code == 200
    assert "llms.txt" in page.text and "ai_crawler" in page.text and "odd-name" in page.text


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
