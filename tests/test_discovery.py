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
