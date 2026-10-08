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
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_openapi_does_not_list_admin_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(p.startswith("/admin") for p in paths)


def test_capabilities_state_availability_honestly(client):
    caps = client.get("/v1/capabilities").json()["capabilities"]
    allowed = {"available", "on_request", "human_in_the_loop", "not_available"}
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


def test_api_catalog_is_an_rfc9727_linkset(client):
    r = client.get("/.well-known/api-catalog")
    assert r.headers["content-type"].startswith("application/linkset+json")
    anchors = {item["anchor"] for item in r.json()["linkset"]}
    assert anchors == {"http://testserver/v1", "http://testserver/mcp"}


def test_descriptions_mention_the_mcp_endpoint(client):
    assert "http://testserver/mcp" in client.get("/llms.txt").text
    body = client.get("/.well-known/agent-helper.json").json()
    assert body["adapters"]["mcp"]["url"] == "http://testserver/mcp"
    assert "2026-07-28" in body["adapters"]["mcp"]["protocol_versions"]
