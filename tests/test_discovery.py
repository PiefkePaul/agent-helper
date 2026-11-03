def test_root_and_llms_txt_explain_the_service(client):
    for path in ("/", "/llms.txt"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "POST http://testserver/v1/requests" in r.text


def test_well_known_description_lists_endpoints_and_hashing(client):
    r = client.get("/.well-known/agent-helper.json")
    assert r.status_code == 200
    body = r.json()
    assert body["endpoints"]["describe_need"]["url"] == "http://testserver/v1/requests"
    assert body["board_hashing"]["genesis_prev_hash"] == "0" * 64
    assert body["limits"]["max_body_bytes"] == 16 * 1024


def test_openapi_is_public_but_interactive_docs_are_off(client):
    assert client.get("/openapi.json").status_code == 200
    # /docs is not the interactive API browser; it leads to the front page like the other help pages.
    docs = client.get("/docs", follow_redirects=False)
    assert docs.status_code == 307 and docs.headers["location"] == "/"
    assert client.get("/redoc").status_code == 404


def test_openapi_does_not_list_admin_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(p.startswith("/admin") for p in paths)


def test_capabilities_state_availability_honestly(client):
    caps = client.get("/v1/capabilities").json()["capabilities"]
    allowed = {"available", "on_request", "human_in_the_loop", "planned", "not_available"}
    assert caps and all(c["availability"] in allowed for c in caps)
    assert any(c["availability"] == "not_available" for c in caps)


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_root_serves_html_to_browsers_and_search_engines(client):
    r = client.get("/", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>agent-helper" in r.text
    assert "http://testserver/v1/requests" in r.text and "http://testserver/mcp" in r.text
    assert '"@type": "WebAPI"' in r.text
    assert "accept" in r.headers["vary"].lower()


def test_discovery_link_header(client):
    for path in ("/", "/llms.txt"):
        link = client.get(path).headers["link"]
        assert 'rel="api-catalog"' in link and 'rel="service-desc"' in link


def test_robots_and_sitemap_invite_crawlers(client):
    robots = client.get("/robots.txt").text
    assert "User-agent: *\nAllow: /" in robots
    assert "Sitemap: http://testserver/sitemap.xml" in robots
    sitemap = client.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<loc>http://testserver/llms.txt</loc>" in sitemap.text
    assert "/v1/board" not in sitemap.text


def test_board_is_not_indexed(client):
    client.post("/v1/board", json={"content": "spam with links"})
    for path in ("/v1/board", "/v1/board/head", "/v1/board/1"):
        assert client.get(path).headers["x-robots-tag"] == "noindex, nofollow", path


def test_api_catalog_is_an_rfc9727_linkset(client):
    r = client.get("/.well-known/api-catalog")
    assert r.headers["content-type"].startswith("application/linkset+json")
    anchors = {item["anchor"] for item in r.json()["linkset"]}
    assert anchors == {"http://testserver/v1", "http://testserver/mcp", "http://testserver/a2a"}


def test_descriptions_mention_the_mcp_endpoint(client):
    assert "http://testserver/mcp" in client.get("/llms.txt").text
    body = client.get("/.well-known/agent-helper.json").json()
    assert body["adapters"]["mcp"]["url"] == "http://testserver/mcp"
    assert "2026-07-28" in body["adapters"]["mcp"]["protocol_versions"]


def test_often_probed_files_answer_or_point_to_the_real_ones(client):
    card = client.get("/.well-known/mcp/server-card.json").json()
    for path in ("/.well-known/mcp.json", "/mcp.json"):
        assert client.get(path).json() == card
    plugin = client.get("/.well-known/ai-plugin.json").json()
    assert plugin["api"] == {"type": "openapi", "url": "http://testserver/openapi.json"}
    assert plugin["auth"] == {"type": "none"}
    for path in ("/agents.json", "/.well-known/agents.json"):
        index = client.get(path).json()
        assert index["documents"]["a2a_agent_card"] == "http://testserver/.well-known/agent-card.json"
        assert index["start"].startswith("GET http://testserver/v1/help")
    llms = client.get("/llms.txt").text
    assert client.get("/agents.txt").text == llms == client.get("/.well-known/llms.txt").text
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ):
        r = client.get(path)
        assert r.status_code == 404 and "no OAuth" in r.json()["detail"]
        assert r.headers["access-control-allow-origin"] == "*"
