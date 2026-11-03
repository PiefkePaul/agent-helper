"""The URLs agents and tools try, answered or pointed to (docs/decisions/0028)."""

import pytest
from test_a2a import message03, user_message
from test_mcp import modern

from agent_helper import discovery

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def test_head_works_wherever_get_does(client):
    for path in ("/", "/llms.txt", "/.well-known/agent-card.json", "/mcp", "/a2a"):
        r = client.head(path)
        assert r.status_code == 200, path
        assert r.content == b""


@pytest.mark.parametrize("path", discovery.MCP_ALIASES)
def test_mcp_aliases_reach_the_same_endpoint(client, path):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN_META}}
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}
    assert client.post(path, json=body, headers=headers).json() == modern(client, "tools/list").json()
    assert client.get(path).status_code == 200
    sse = client.get(path, headers={"Accept": "text/event-stream"})
    assert sse.status_code == 200 and sse.headers["content-type"].startswith("text/event-stream")


def test_a2a_info_health_and_card_next_to_the_endpoint(client):
    assert "POST JSON-RPC" in client.get("/a2a").json()["how"]
    assert client.get("/a2a/health").json() == {"status": "ok"}
    card = client.get("/.well-known/agent-card.json").json()
    assert client.get("/a2a/.well-known/agent-card.json").json() == card


@pytest.mark.parametrize("path", ["/a2a/v1/message:send", "/.well-known/agent-card.json/v1/message:send"])
def test_a2a_rest_binding_1_0(client, path):
    r = client.post(path, json=user_message("I need a scan."), headers={"A2A-Version": "1.0"})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    token = task["metadata"]["followUpToken"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    auth = {"A2A-Version": "1.0", "Authorization": f"Bearer {token}"}
    got = client.get(f"/a2a/v1/tasks/{task['id']}?historyLength=1", headers=auth).json()
    assert got["id"] == task["id"] and len(got["history"]) == 1
    canceled = client.post(f"/a2a/v1/tasks/{task['id']}:cancel", headers=auth).json()
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
    again = client.post(f"/a2a/v1/tasks/{task['id']}:cancel", headers=auth)
    assert again.status_code == 409 and again.json()["error"]["status"] == "TASK_NOT_CANCELABLE"


def test_a2a_rest_binding_0_3_and_errors(client):
    r = client.post("/a2a/v1/message:send", json=message03("hello"))
    task = r.json()
    assert r.status_code == 200 and task["kind"] == "task" and task["status"]["state"] == "submitted"
    wrong = {"Authorization": "Bearer not-a-token"}
    missing = client.get("/a2a/v1/tasks/req_nope", headers=wrong)
    assert missing.status_code == 404 and missing.json()["error"]["status"] == "TASK_NOT_FOUND"
    bad = client.post("/a2a/v1/message:send", content=b"{nope", headers={"A2A-Version": "1.0"})
    assert bad.status_code == 400
    other = client.post("/a2a/v1/message:send", json=user_message("hi"), headers={"A2A-Version": "2.0"})
    assert other.status_code == 400 and other.json()["error"]["status"] == "VERSION_NOT_SUPPORTED"
    files = {"message": {"role": "ROLE_USER", "parts": [{"url": "https://x.example/f"}]}}
    unsupported = client.post("/a2a/v1/message:send", json=files, headers={"A2A-Version": "1.0"})
    assert unsupported.status_code == 415
    history = client.get("/a2a/v1/tasks/req_nope?historyLength=-1", headers=wrong)
    assert history.status_code == 400


@pytest.mark.parametrize("path", discovery.OAUTH_PATHS)
def test_oauth_paths_say_no_auth_is_needed(client, path):
    r = client.get(path)
    assert r.status_code == 404 and "no OAuth" in r.json()["detail"]


def test_no_wildcard_oauth_paths(client):
    assert client.get("/.well-known/oauth-protected-resource/anything/else").json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/mcp/server-card", "/mcp/server-card.json", "/.well-known/mcp"])
def test_server_card_aliases(client, path):
    assert client.get(path).json() == client.get("/.well-known/mcp/server-card.json").json()


@pytest.mark.parametrize("path", discovery.INDEX_PATHS)
def test_index_aliases(client, path):
    assert client.get(path).json() == client.get("/agents.json").json()


@pytest.mark.parametrize("path", discovery.PAYMENT_PATHS)
def test_payment_paths_say_free(client, path):
    body = client.get(path).json()
    assert body["payment_required"] is False and body["price"] == "free"


def test_favicon(client):
    assert client.get("/favicon.ico").status_code == 204


def test_glama_only_with_a_configured_maintainer(make_client):
    assert make_client().get("/.well-known/glama.json").status_code == 404
    with_mail = make_client(glama_maintainer_email="maintainer@example.org")
    body = with_mail.get("/.well-known/glama.json").json()
    assert body["maintainers"] == [{"email": "maintainer@example.org"}]
    assert body["$schema"].startswith("https://glama.ai/")


def test_aliases_and_new_paths_have_bounded_usage_names():
    from agent_helper import discovery
    from agent_helper.usage import MCP_ALIAS_PATHS, endpoint_of

    assert MCP_ALIAS_PATHS == set(discovery.MCP_ALIASES)

    def scope(path, method="GET"):
        return {"type": "http", "path": path, "method": method, "headers": []}

    assert endpoint_of(scope("/sse", "POST"), 200) == "mcp-alias:post"
    assert endpoint_of(scope("/a2a/v1/tasks/req_abc"), 200) == "a2a:rest:tasks:get"
    assert endpoint_of(scope("/a2a/v1/tasks/req_abc:cancel", "POST"), 200) == "a2a:rest:tasks:post"
    assert endpoint_of(scope("/.well-known/x402"), 200) == "compat:payment"


def test_rest_and_alias_posts_are_not_charged_twice(make_client):
    client = make_client(write_per_minute=2, global_write_per_minute=2)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN_META}}
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}
    for _ in range(5):  # reads through an alias cost no write budget
        assert client.post("/sse", json=body, headers=headers).status_code == 200
    for _ in range(2):  # two real writes fit the budget of two
        r = client.post("/a2a/v1/message:send", json=user_message("hi"), headers={"A2A-Version": "1.0"})
        assert r.status_code == 200, r.text
    third = client.post("/a2a/v1/message:send", json=user_message("hi"), headers={"A2A-Version": "1.0"})
    assert third.status_code == 429


def test_rest_rejects_odd_input_without_crashing(client):
    wrong = {"Authorization": "Bearer not-a-token", "A2A-Version": "1.0"}
    for value in ("%C2%B2", "1" * 5000, "-1", "1.5"):
        assert client.get(f"/a2a/v1/tasks/req_x?historyLength={value}", headers=wrong).status_code == 400
    lone = rb'{"message": {"role": "ROLE_USER", "taskId": "\ud800", "parts": [{"text": "hi"}]}}'
    r = client.post("/a2a/v1/message:send", content=lone, headers=wrong)
    assert r.status_code == 400
    long_id = client.get("/a2a/v1/tasks/" + "x" * 100, headers=wrong)
    assert long_id.status_code == 400 and "error" in long_id.json()
