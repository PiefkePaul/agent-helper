def _create(client, **body):
    r = client.post("/v1/requests", json={"message": "I need a human to check a physical mailbox.", **body})
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def test_request_conversation_round_trip(client, admin_headers):
    created = _create(client, handle="agent-7")
    auth = {"Authorization": f"Bearer {created['follow_up_token']}"}

    r = client.get(f"/v1/requests/{created['id']}", headers=auth)
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    assert r.json()["messages"][0]["sender"] == "agent"

    r = client.post(
        f"/admin/v1/requests/{created['id']}/replies", json={"message": "Can do on Friday."}, headers=admin_headers
    )
    assert r.status_code == 200

    r = client.get(f"/v1/requests/{created['id']}", headers=auth)
    assert r.json()["status"] == "answered"
    assert [m["sender"] for m in r.json()["messages"]] == ["agent", "operator"]

    r = client.post(f"/v1/requests/{created['id']}/messages", json={"message": "Thanks, Friday works."}, headers=auth)
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    assert len(r.json()["messages"]) == 3


def test_wrong_or_missing_token_is_rejected(client):
    created = _create(client)
    assert client.get(f"/v1/requests/{created['id']}").status_code == 401
    r = client.get(f"/v1/requests/{created['id']}", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 404
    r = client.get("/v1/requests/req_doesnotexist", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 404


def test_token_is_not_stored_in_plain_text(client, tmp_path):
    created = _create(client)
    raw = (tmp_path / "agent-helper.db").read_bytes()
    assert created["follow_up_token"].encode() not in raw


def test_message_is_required_and_bounded(client):
    assert client.post("/v1/requests", json={}).status_code == 422
    assert client.post("/v1/requests", json={"message": ""}).status_code == 422
    assert client.post("/v1/requests", json={"message": "x" * 8001}).status_code == 422


def test_control_characters_are_rejected_but_newlines_allowed(client):
    assert client.post("/v1/requests", json={"message": "bad\x00byte"}).status_code == 422
    assert client.post("/v1/requests", json={"message": "line one\nline two\ttabbed"}).status_code == 201


def test_report_is_quarantined_until_operator_decides(client, admin_headers):
    r = client.post("/v1/reports", json={"kind": "feature", "text": "Please add an MCP adapter."})
    assert r.status_code == 201
    rep = r.json()
    auth = {"Authorization": f"Bearer {rep['follow_up_token']}"}

    assert client.get(f"/v1/reports/{rep['id']}", headers=auth).json()["status"] == "quarantined"

    queue = client.get("/admin/v1/reports?status=quarantined", headers=admin_headers).json()
    assert [q["id"] for q in queue] == [rep["id"]]

    r = client.post(
        f"/admin/v1/reports/{rep['id']}/decision",
        json={"status": "accepted", "note": "Planned."},
        headers=admin_headers,
    )
    assert r.status_code == 200
    out = client.get(f"/v1/reports/{rep['id']}", headers=auth).json()
    assert out["status"] == "accepted"
    assert out["operator_note"] == "Planned."


def test_unknown_report_kind_rejected(client):
    assert client.post("/v1/reports", json={"kind": "exploit", "text": "x"}).status_code == 422
