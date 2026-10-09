import pytest
from fastapi.testclient import TestClient
from test_mcp import call, legacy

JSON = {"content-type": "application/json"}


@pytest.fixture
def raw_client(make_client) -> TestClient:
    # Server errors must come back as responses, not be re-raised into the test.
    app = make_client().app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _post_raw(client, body: bytes, path="/mcp"):
    return client.post(path, content=body, headers=JSON)


@pytest.mark.parametrize("path", ["/mcp", "/v1/board", "/v1/requests"])
def test_deeply_nested_json_is_a_client_error(raw_client, path):
    body = b"[" * 5000 + b"]" * 5000
    assert _post_raw(raw_client, body, path).status_code in (400, 422)


@pytest.mark.parametrize(
    "body",
    [
        rb'{"jsonrpc": "2.0", "id": 1, "method": "\ud800"}',
        rb'{"jsonrpc": "2.0", "id": "\udfff", "method": "ping"}',
        rb'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "\ud800"}}',
        rb'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_request",'
        rb' "arguments": {"id": "\ud800", "follow_up_token": "x"}}}',
    ],
)
def test_lone_surrogates_never_cause_server_errors(raw_client, body):
    r = _post_raw(raw_client, body)
    assert r.status_code < 500, r.text


@pytest.mark.parametrize("name", [{"a": 1}, ["describe_need"], 5, None])
def test_non_string_tool_name_is_invalid_params(raw_client, name):
    r = legacy(raw_client, "tools/call", {"name": name, "arguments": {}})
    assert r.status_code < 500
    assert r.json()["error"]["code"] == -32602


@pytest.mark.parametrize("msg_id", [{"x": 1}, [1], True, 1.5])
def test_invalid_id_types_are_rejected(raw_client, msg_id):
    r = raw_client.post("/mcp", json={"jsonrpc": "2.0", "id": msg_id, "method": "ping"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600


def test_batch_is_rejected(raw_client):
    r = raw_client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}] * 3)
    assert r.status_code == 400


def test_mcp_body_size_limit_applies(make_client):
    client = make_client(max_body_bytes=300)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "y" * 1000}})
    assert r.status_code == 413


def test_mcp_writes_share_the_http_write_budget(make_client):
    client = make_client(write_per_minute=2)
    assert client.post("/v1/board", json={"content": "http"}).status_code == 201
    assert call(client, "post_board", {"content": "mcp"}, era=legacy)["isError"] is False
    assert call(client, "post_board", {"content": "mcp"}, era=legacy)["isError"] is True
    assert client.post("/v1/board", json={"content": "http"}).status_code == 429


def test_mcp_cannot_use_reserved_or_foreign_handles(client):
    assert call(client, "post_board", {"content": "x", "author": "operator"}, era=legacy)["isError"] is True
    token = call(client, "post_board", {"content": "x", "author": "Vega"}, era=legacy)["structuredContent"][
        "handle_token"
    ]
    assert call(client, "post_board", {"content": "x", "author": "vega"}, era=legacy)["isError"] is True
    ok = call(client, "post_board", {"content": "x", "author": "vega", "handle_token": token}, era=legacy)
    assert ok["isError"] is False
