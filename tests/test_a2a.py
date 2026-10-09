import pytest


def rpc(client, method, params=None, *, token=None, headers=None, msg_id=1):
    hdrs = {"A2A-Version": "1.0", **(headers or {})}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    return client.post("/a2a", json=body, headers=hdrs)


def user_message(text, **extra):
    return {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}], **extra}}


def start(client, text="I need a human to scan a paper form.", **extra):
    r = rpc(client, "SendMessage", user_message(text, **extra))
    assert r.status_code == 200, r.text
    return r.json()["result"]["task"]


def test_agent_card(client):
    card = client.get("/.well-known/agent-card.json").json()
    for key in (
        "name",
        "description",
        "supportedInterfaces",
        "version",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ):
        assert key in card
    (iface,) = card["supportedInterfaces"]
    assert iface == {"url": "http://testserver/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert card["capabilities"]["streaming"] is False and card["capabilities"]["pushNotifications"] is False
    assert "followUpToken" in card["securitySchemes"]
    assert all({"id", "name", "description", "tags"} <= set(s) for s in card["skills"])


def test_start_read_and_answer_a_task(client, admin_headers):
    task = start(client)
    token = task["metadata"]["followUpToken"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED" and task["contextId"] == task["id"]
    assert task["history"][0]["role"] == "ROLE_USER"

    # Reading needs the token, sent as a bearer header.
    assert rpc(client, "GetTask", {"id": task["id"]}).json()["error"]["code"] == -32001
    assert rpc(client, "GetTask", {"id": task["id"]}, token="wrong").json()["error"]["code"] == -32001  # noqa: S106
    seen = rpc(client, "GetTask", {"id": task["id"]}, token=token).json()["result"]
    assert "metadata" not in seen  # the token is shown only once

    client.post(f"/admin/v1/requests/{task['id']}/replies", json={"message": "Can do Friday."}, headers=admin_headers)
    seen = rpc(client, "GetTask", {"id": task["id"]}, token=token).json()["result"]
    assert seen["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert seen["status"]["message"]["parts"] == [{"text": "Can do Friday."}]
    assert [m["role"] for m in seen["history"]] == ["ROLE_USER", "ROLE_AGENT"]
    assert (
        len(rpc(client, "GetTask", {"id": task["id"], "historyLength": 1}, token=token).json()["result"]["history"])
        == 1
    )

    r = rpc(client, "SendMessage", user_message("Friday works.", taskId=task["id"]), token=token)
    assert r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert rpc(client, "SendMessage", user_message("x", taskId=task["id"])).json()["error"]["code"] == -32001

    closed = rpc(client, "CancelTask", {"id": task["id"]}, token=token).json()["result"]
    assert closed["status"]["state"] == "TASK_STATE_CANCELED"
    # A canceled task stays canceled: no second cancel, no new messages.
    assert rpc(client, "CancelTask", {"id": task["id"]}, token=token).json()["error"]["code"] == -32002
    again = rpc(client, "SendMessage", user_message("one more", taskId=task["id"]), token=token)
    assert again.json()["error"]["code"] == -32004
    assert rpc(client, "GetTask", {"id": task["id"]}, token=token).json()["result"]["status"]["state"] == (
        "TASK_STATE_CANCELED"
    )
    # The same conversation is visible over the plain HTTP API.
    r = client.get(f"/v1/requests/{task['id']}", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["status"] == "closed" and len(r.json()["messages"]) == 3


def test_handle_via_metadata(client):
    task = start(client, metadata={"handle": "nova", "contactHint": "nova@example.invalid"})
    assert task["metadata"]["handleToken"]
    r = rpc(client, "SendMessage", user_message("again", metadata={"handle": "n0va"}))
    assert r.json()["error"]["code"] == -32602  # taken; needs handleToken


def test_only_text_parts_and_user_role(client):
    r = rpc(client, "SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"url": "http://x"}]}})
    assert r.json()["error"]["code"] == -32005
    r = rpc(client, "SendMessage", {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "x"}]}})
    assert r.json()["error"]["code"] == -32602
    r = rpc(client, "SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER", "parts": []}})
    assert r.json()["error"]["code"] == -32602
    r = rpc(client, "SendMessage", user_message("x" * 9000))
    assert r.json()["error"]["code"] == -32602
    r = rpc(client, "SendMessage", user_message("bidi ‮ trick"))
    assert r.json()["error"]["code"] == -32602


@pytest.mark.parametrize(
    "method",
    [
        "SendStreamingMessage",
        "ListTasks",
        "SubscribeToTask",
        "GetExtendedAgentCard",
    ],
)
def test_unsupported_operations(client, method):
    assert rpc(client, method).json()["error"]["code"] == -32004


def test_push_is_not_supported(client):
    assert rpc(client, "CreateTaskPushNotificationConfig").json()["error"]["code"] == -32003


def test_protocol_errors(client):
    assert rpc(client, "Nope").json()["error"]["code"] == -32601
    assert rpc(client, "GetTask", headers={"A2A-Version": "0.3"}).json()["error"]["code"] == -32009
    assert client.post("/a2a", content=b"{not json").json()["error"]["code"] == -32700
    assert client.post("/a2a", json=[1, 2]).status_code == 400
    r = client.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask"}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    # A missing or empty version means 0.3, which is not served.
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": user_message("hi")})
    assert r.json()["error"]["code"] == -32009
    assert rpc(client, "GetTask", headers={"A2A-Version": ""}).json()["error"]["code"] == -32009
    # The version may also come as a query parameter.
    r = client.post(
        "/a2a?A2A-Version=1.0", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": user_message("hi")}
    )
    assert "result" in r.json()
    # Errors without a usable id carry "id": null; notifications get no answer.
    assert client.post("/a2a", content=b"{bad").json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "parse error: send one JSON-RPC 2.0 request"},
    }
    r = client.post("/a2a", json={"jsonrpc": "2.0", "method": "SendMessage"}, headers={"A2A-Version": "1.0"})
    assert r.status_code == 202 and r.content == b""
    # The same origin with an explicit default port is fine.
    r = rpc(client, "GetTask", {"id": "x"}, headers={"Origin": "http://testserver:80"})
    assert r.status_code == 200


def test_writes_spend_the_write_budget(make_client):
    client = make_client(write_per_minute=2, global_write_per_minute=100)
    # Rejected calls do not spend it.
    for _ in range(3):
        bad = {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"url": "x"}]}}
        assert rpc(client, "SendMessage", bad).json()["error"]["code"] == -32005
    start(client)
    start(client)
    r = rpc(client, "SendMessage", user_message("third"))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert "rate limit" in r.json()["error"]["message"]
    # Reads are not charged as writes.
    assert rpc(client, "GetTask", {"id": "req_x"}, token="t").json()["error"]["code"] == -32001  # noqa: S106


def test_token_never_in_urls_and_responses_not_cached(client):
    r = rpc(client, "SendMessage", user_message("hi"))
    assert r.headers["cache-control"] == "no-store"
    task = r.json()["result"]["task"]
    assert task["metadata"]["followUpToken"] not in str(r.url)


def test_discovery_mentions_a2a(client):
    desc = client.get("/.well-known/agent-helper.json").json()
    assert desc["adapters"]["a2a"]["agent_card"] == "http://testserver/.well-known/agent-card.json"
    assert "/a2a" in client.get("/llms.txt").text


def test_more_validation(client):
    r = rpc(client, "SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": ""}]}})
    assert r.json()["error"]["code"] == -32602
    both = {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x", "raw": "eA=="}]}}
    assert rpc(client, "SendMessage", both).json()["error"]["code"] == -32005
    task = start(client)
    token = task["metadata"]["followUpToken"]
    mismatch = user_message("x", taskId=task["id"], contextId="req_other")
    assert rpc(client, "SendMessage", mismatch, token=token).json()["error"]["code"] == -32602
    images_only = {**user_message("x"), "configuration": {"acceptedOutputModes": ["image/png"]}}
    assert rpc(client, "SendMessage", images_only).json()["error"]["code"] == -32005


def test_agent_close_notifies_the_operator(make_client):
    from agent_helper.notify import Delivery

    client = make_client(notify_webhook_url="https://hooks.example.invalid/x")
    events = []
    client.app.state.notifier.transport = lambda url, body, headers: (
        events.append(__import__("json").loads(body)["event"]) or Delivery(ok=True, status=200)
    )
    task = start(client)
    rpc(client, "CancelTask", {"id": task["id"]}, token=task["metadata"]["followUpToken"])
    assert client.app.state.notifier.flush()
    assert events == ["request.created", "request.closed"]
