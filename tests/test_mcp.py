import base64

MODERN = "2026-07-28"


def modern(client, method, params=None, *, msg_id=1, headers=None, name=None):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    hdrs = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if method == "tools/call":
        hdrs["Mcp-Name"] = name if name is not None else params["name"]
    hdrs |= headers or {}
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}, headers=hdrs)


def legacy(client, method, params=None, *, msg_id=1, version="2025-11-25"):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={"MCP-Protocol-Version": version})


def call(client, tool, arguments=None, era=modern):
    params = {"name": tool, "arguments": arguments or {}}
    r = era(client, "tools/call", params)
    assert r.status_code == 200, r.text
    return r.json()["result"]


# --- protocol -------------------------------------------------------------------------------------


def test_modern_discover_lists_versions_and_tools_capability(client):
    r = modern(client, "server/discover")
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["resultType"] == "complete"
    assert MODERN in result["supportedVersions"] and "2025-11-25" in result["supportedVersions"]
    assert "tools" in result["capabilities"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "agent-helper"
    assert "describe_need" in result["instructions"]


def test_legacy_initialize_without_session(client):
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "agent-helper"
    assert "mcp-session-id" not in r.headers


def test_legacy_initialize_with_unknown_version_offers_latest_legacy(client):
    r = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "x"}}
    )
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_notification_is_accepted_without_body(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert r.content == b""


def test_get_and_delete_are_not_allowed(client):
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405


def test_tools_list_in_both_eras(client):
    for r in (modern(client, "tools/list"), legacy(client, "tools/list")):
        assert r.status_code == 200
        result = r.json()["result"]
        names = {t["name"] for t in result["tools"]}
        assert {"describe_need", "read_request", "add_message", "read_board", "post_board"} <= names
        assert result["cacheScope"] == "public" and result["ttlMs"] > 0
        assert all(t["inputSchema"]["type"] == "object" for t in result["tools"])


def test_unsupported_modern_version_is_a_recognisable_error(client):
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}},
        },
        headers={"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list"},
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == -32022
    assert MODERN in err["data"]["supported"]


def test_unsupported_legacy_header_version_is_rejected(client):
    r = legacy(client, "tools/list", version="1999-01-01")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32022


def test_modern_request_needs_matching_headers(client):
    r = modern(client, "tools/list", headers={"Mcp-Method": "tools/call"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    r = modern(client, "tools/list", headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    r = modern(client, "tools/call", {"name": "list_capabilities"}, name="read_board")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020


def test_base64_encoded_tool_name_header_is_decoded(client):
    encoded = "=?base64?" + base64.b64encode(b"list_capabilities").decode() + "?="
    r = modern(client, "tools/call", {"name": "list_capabilities", "arguments": {}}, name=encoded)
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is False


def test_modern_header_without_meta_is_a_mismatch(client):
    r = legacy(client, "tools/list", version=MODERN)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_unknown_method(client):
    assert modern(client, "resources/list").status_code == 404
    r = legacy(client, "resources/list")
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601


def test_bad_bodies(client):
    r = client.post("/mcp", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600


def test_foreign_origin_is_forbidden(client):
    r = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 200


def test_ping_for_legacy_clients(client):
    assert legacy(client, "ping").json()["result"]["resultType"] == "complete"


# --- tools ----------------------------------------------------------------------------------------


def test_describe_need_then_read_and_continue(client, admin_headers):
    created = call(client, "describe_need", {"message": "I need a phone number verified", "handle": "scout"})
    assert created["isError"] is False
    data = created["structuredContent"]
    assert data["id"].startswith("req") and data["follow_up_token"] and data["handle_token"]

    client.post(f"/admin/v1/requests/{data['id']}/replies", json={"message": "Which number?"}, headers=admin_headers)

    read = call(client, "read_request", {"id": data["id"], "follow_up_token": data["follow_up_token"]}, era=legacy)
    assert [m["sender"] for m in read["structuredContent"]["messages"]] == ["agent", "operator"]

    added = call(
        client, "add_message", {"id": data["id"], "follow_up_token": data["follow_up_token"], "message": "+49"}
    )
    assert len(added["structuredContent"]["messages"]) == 3

    # The same request is visible over plain HTTP with the same token.
    r = client.get(f"/v1/requests/{data['id']}", headers={"Authorization": f"Bearer {data['follow_up_token']}"})
    assert r.status_code == 200


def test_wrong_token_is_a_tool_error(client):
    data = call(client, "describe_need", {"message": "hello"})["structuredContent"]
    result = call(client, "read_request", {"id": data["id"], "follow_up_token": "wrong"})
    assert result["isError"] is True
    assert "not found" in result["content"][0]["text"]


def test_invalid_arguments_are_tool_errors_not_crashes(client):
    assert call(client, "describe_need", {})["isError"] is True
    assert call(client, "describe_need", {"message": "x" * 9000})["isError"] is True
    assert call(client, "describe_need", {"message": "hi‮evil"})["isError"] is True
    assert call(client, "read_board", {"limit": 0})["isError"] is True
    assert call(client, "read_request", {"id": 5})["isError"] is True


def test_unknown_tool_is_a_protocol_error(client):
    r = modern(client, "tools/call", {"name": "rm_rf"})
    assert r.json()["error"]["code"] == -32602


def test_board_round_trip_and_handle_protection(client):
    first = call(client, "post_board", {"content": "hello future agents", "author": "scout"})
    assert first["structuredContent"]["seq"] == 1 and first["structuredContent"]["handle_token"]
    hijack = call(client, "post_board", {"content": "I am scout too", "author": "scout"})
    assert hijack["isError"] is True
    board = call(client, "read_board", {"after": 0, "limit": 10})["structuredContent"]
    assert [e["content"] for e in board["entries"]] == ["hello future agents"]
    assert board["head"]["seq"] == 1
    assert client.get("/v1/board").json()[0]["entry_hash"] == board["entries"][0]["entry_hash"]


def test_report_is_quarantined(client):
    data = call(client, "report_issue", {"kind": "feature", "text": "please add A2A"})["structuredContent"]
    r = client.get(f"/v1/reports/{data['id']}", headers={"Authorization": f"Bearer {data['follow_up_token']}"})
    assert r.json()["status"] == "quarantined"


def test_list_capabilities_matches_http(client):
    result = call(client, "list_capabilities")["structuredContent"]
    assert result == client.get("/v1/capabilities").json()


def test_responses_are_not_cached(client):
    assert modern(client, "tools/list").headers["cache-control"] == "no-store"


# --- rate limits ----------------------------------------------------------------------------------


def test_reading_tools_do_not_spend_the_write_budget(make_client):
    client = make_client(write_per_minute=2, global_write_per_minute=2)
    for _ in range(5):
        assert modern(client, "tools/list").status_code == 200
        assert call(client, "read_board")["isError"] is False
    assert call(client, "describe_need", {"message": "one"})["isError"] is False
    assert call(client, "describe_need", {"message": "two"})["isError"] is False
    limited = call(client, "describe_need", {"message": "three"})
    assert limited["isError"] is True and "rate limit" in limited["content"][0]["text"]
    # The budget is shared with the plain HTTP API.
    assert client.post("/v1/requests", json={"message": "four"}).status_code == 429


def test_body_size_limit_still_applies(make_client):
    client = make_client(max_body_bytes=200)
    r = modern(client, "tools/call", {"name": "describe_need", "arguments": {"message": "x" * 500}})
    assert r.status_code == 413


# --- directory and mailboxes ----------------------------------------------------------------------


def test_directory_and_mailbox_tools(client):
    nova = call(client, "publish_profile", {"handle": "nova", "summary": "Translator", "tags": ["translation"]})
    assert nova["isError"] is False
    nova_token = nova["structuredContent"]["handle_token"]
    orion = call(client, "publish_profile", {"handle": "orion", "summary": "Crawler", "offers": ["web crawls"]})
    orion_token = orion["structuredContent"]["handle_token"]

    found = call(client, "search_directory", {"query": "crawls"})["structuredContent"]["profiles"]
    assert [p["handle"] for p in found] == ["orion"]

    sent = call(
        client,
        "send_message",
        {"sender": "nova", "to": "orion", "message": "Can you crawl a site?", "handle_token": nova_token},
    )
    assert sent["isError"] is False

    inbox = call(client, "read_mailbox", {"handle": "orion", "handle_token": orion_token})["structuredContent"]
    assert [m["body"] for m in inbox["messages"]] == ["Can you crawl a site?"]

    wrong = call(client, "read_mailbox", {"handle": "orion", "handle_token": nova_token})
    assert wrong["isError"] is True
    refused = call(
        client, "send_message", {"sender": "nova", "to": "nobody", "message": "x", "handle_token": nova_token}
    )
    assert refused["isError"] is True and "no such handle" in refused["content"][0]["text"]
    bad = call(client, "publish_profile", {"handle": "bad/handle", "summary": "x"})
    assert bad["isError"] is True


def test_manage_mailbox_tool(client):
    nova = call(client, "publish_profile", {"handle": "nova", "summary": "a"})["structuredContent"]["handle_token"]
    orion = call(client, "publish_profile", {"handle": "orion", "summary": "b"})["structuredContent"]["handle_token"]
    msg = {"sender": "nova", "to": "orion", "message": "spam", "handle_token": nova}
    call(client, "send_message", msg)
    base = {"handle": "orion", "handle_token": orion, "other": "nova"}
    assert call(client, "manage_mailbox", {**base, "action": "delete_from"})["structuredContent"] == {"deleted": 1}
    assert call(client, "manage_mailbox", {**base, "action": "block"})["isError"] is False
    assert call(client, "send_message", msg)["isError"] is True
    assert call(client, "manage_mailbox", {**base, "action": "nope"})["isError"] is True
    cleared = call(client, "manage_mailbox", {"handle": "orion", "handle_token": orion, "action": "delete_all"})
    assert cleared["structuredContent"] == {"deleted": 0}
