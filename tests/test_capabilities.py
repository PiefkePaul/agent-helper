import json

import pytest

from tests.test_notify import WEBHOOK, Recorder


def test_catalog_entries_are_structured(client):
    body = client.get("/v1/capabilities").json()
    meanings = {"available", "human_in_the_loop", "on_request", "planned", "not_available"}
    assert set(body["availability_meaning"]) == meanings
    for cap in body["capabilities"]:
        assert {"id", "title", "summary", "category", "availability", "access", "tags", "source"} <= set(cap)
    ids = {c["id"] for c in body["capabilities"]}
    assert {"free-text-request", "agent-directory", "capability-requests"} <= ids


def test_catalog_filters(client):
    only = client.get("/v1/capabilities", params={"availability": "not_available"}).json()["capabilities"]
    assert [c["id"] for c in only] == ["code-execution"]
    comm = client.get("/v1/capabilities", params={"category": "communication"}).json()["capabilities"]
    assert comm and all(c["category"] == "communication" for c in comm)
    found = client.get("/v1/capabilities", params={"q": "hash chain"}).json()["capabilities"]
    assert [c["id"] for c in found] == ["message-board"]
    assert client.get("/v1/capabilities/message-board").json()["availability"] == "available"
    assert client.get("/v1/capabilities/nope").status_code == 404


def test_operator_can_add_and_override_entries(client, admin_headers):
    entry = {
        "title": "OCR for scanned documents",
        "summary": "Send a scan, get text back.",
        "category": "tool",
        "availability": "planned",
        "tags": ["ocr"],
    }
    r = client.put("/admin/v1/capabilities/ocr", json=entry, headers=admin_headers)
    assert r.status_code == 200 and r.json()["source"] == "operator"
    assert client.get("/v1/capabilities/ocr").json()["availability"] == "planned"

    override = {**entry, "title": "Code execution", "category": "compute", "availability": "on_request"}
    client.put("/admin/v1/capabilities/code-execution", json=override, headers=admin_headers)
    assert client.get("/v1/capabilities/code-execution").json()["availability"] == "on_request"
    assert client.delete("/admin/v1/capabilities/code-execution", headers=admin_headers).status_code == 204
    assert client.get("/v1/capabilities/code-execution").json()["availability"] == "not_available"

    assert client.delete("/admin/v1/capabilities/message-board", headers=admin_headers).status_code == 404
    assert client.put("/admin/v1/capabilities/ocr", json=entry).status_code == 401
    bad = {**entry, "availability": "maybe"}
    assert client.put("/admin/v1/capabilities/ocr", json=bad, headers=admin_headers).status_code == 422
    assert client.put("/admin/v1/capabilities/Bad_Id", json=entry, headers=admin_headers).status_code == 422


def test_invalid_entries_in_a_catalog_file_are_skipped(make_client, tmp_path):
    path = tmp_path / "caps.json"
    good = {"id": "x", "title": "X", "summary": "x", "category": "tool", "availability": "available"}
    path.write_text(json.dumps({"capabilities": [good, {"id": "broken"}]}), encoding="utf-8")
    client = make_client(capabilities_file=path)
    assert [c["id"] for c in client.get("/v1/capabilities").json()["capabilities"]] == ["x"]


def _ask(client, title="OCR for scanned PDFs", **extra):
    body = {"title": title, "description": "I receive scanned invoices.", "tags": ["ocr"], **extra}
    r = client.post("/v1/capability-requests", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_capability_request_lifecycle(client, admin_headers):
    first = _ask(client, handle="nova")
    assert first["votes"] == 1 and first["status"] == "open" and first["requested_by"] == "nova"
    nova = first["handle_token"]

    second = _ask(client, title="OCR for scanned PDF files")
    assert [s["id"] for s in second["similar"]] == [first["id"]]
    assert second["votes"] == 0

    vote = {"handle": "orion"}
    r = client.post(f"/v1/capability-requests/{first['id']}/votes", json=vote)
    assert r.json()["votes"] == 2
    orion = r.json()["handle_token"]
    # Voting twice changes nothing; voting as someone else needs their token.
    r = client.post(f"/v1/capability-requests/{first['id']}/votes", json={**vote, "handle_token": orion})
    assert r.json()["votes"] == 2
    assert client.post(f"/v1/capability-requests/{first['id']}/votes", json=vote).status_code == 409

    listed = client.get("/v1/capability-requests").json()["requests"]
    assert listed[0]["id"] == first["id"]
    newest = client.get("/v1/capability-requests", params={"sort": "new"}).json()["requests"]
    assert newest[0]["id"] == second["id"]
    assert client.get(f"/v1/capability-requests/{first['id']}").headers["x-robots-tag"] == "noindex, nofollow"

    withdraw = {"handle": "nova", "handle_token": nova}
    r = client.post(f"/v1/capability-requests/{first['id']}/votes/withdraw", json=withdraw)
    assert r.json()["votes"] == 1

    decision = {"status": "planned", "note": "Building it.", "capability_id": "new-tool"}
    r = client.post(f"/admin/v1/capability-requests/{first['id']}/decision", json=decision, headers=admin_headers)
    assert r.json()["status"] == "planned"
    assert client.get(f"/v1/capability-requests/{first['id']}").json()["operator_note"] == "Building it."
    planned = client.get("/v1/capability-requests", params={"status": "planned"}).json()["requests"]
    assert [p["id"] for p in planned] == [first["id"]]

    hide = {"reason": "spam"}
    client.post(f"/admin/v1/capability-requests/{second['id']}/hide", json=hide, headers=admin_headers)
    assert client.get(f"/v1/capability-requests/{second['id']}").status_code == 404
    assert client.post(f"/v1/capability-requests/{second['id']}/votes", json=vote).status_code == 404
    admin_list = client.get("/admin/v1/capability-requests", headers=admin_headers).json()
    assert {r["id"]: r["hidden_reason"] for r in admin_list}[second["id"]] == "spam"


@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "description": "x"},
        {"title": "x" * 121, "description": "x"},
        {"title": "x", "description": "x", "tags": ["Bad Tag"]},
        {"title": "x", "description": "x", "handle": "operator"},
    ],
)
def test_capability_request_validation(client, body):
    assert client.post("/v1/capability-requests", json=body).status_code in (409, 422)


def test_capability_request_notifies_operator(make_client):
    client = make_client(notify_webhook_url=WEBHOOK)
    rec = Recorder()
    client.app.state.notifier.transport = rec
    _ask(client)
    assert client.app.state.notifier.flush()
    assert rec.events[0]["event"] == "capability.requested"


def test_decision_changes_only_sent_fields_and_never_unhides(client, admin_headers):
    item = _ask(client)
    url = f"/admin/v1/capability-requests/{item['id']}"
    client.put(
        "/admin/v1/capabilities/ocr",
        json={"title": "OCR", "summary": "x", "category": "tool", "availability": "planned"},
        headers=admin_headers,
    )
    client.post(
        f"{url}/decision", json={"status": "planned", "note": "soon", "capability_id": "ocr"}, headers=admin_headers
    )
    client.post(f"{url}/hide", json={"reason": "spam"}, headers=admin_headers)
    r = client.post(f"{url}/decision", json={"status": "duplicate"}, headers=admin_headers)
    assert r.json()["hidden_reason"] == "spam"
    assert r.json()["operator_note"] == "soon" and r.json()["capability_id"] == "ocr"
    assert client.get(f"/v1/capability-requests/{item['id']}").status_code == 404
    client.post(f"{url}/unhide", headers=admin_headers)
    assert client.get(f"/v1/capability-requests/{item['id']}").status_code == 200

    assert client.post(f"{url}/hide", json={"reason": ""}, headers=admin_headers).status_code == 422
    bad_link = client.post(f"{url}/decision", json={"capability_id": "nope"}, headers=admin_headers)
    assert bad_link.status_code == 422
    assert client.post(f"{url}/decision", json={"capability_id": "../x y"}, headers=admin_headers).status_code == 422
    assert client.post(f"{url}/decision", json={"status": None}, headers=admin_headers).status_code == 422


def test_broken_operator_entry_does_not_break_the_catalog(client, admin_headers):
    import json as _json

    entry = {"title": "OCR", "summary": "x", "category": "tool", "availability": "planned"}
    client.put("/admin/v1/capabilities/ocr", json=entry, headers=admin_headers)
    store = client.app.state.store
    with store._lock:
        store._db.execute(
            "UPDATE operator_capabilities SET data = ? WHERE id = 'ocr'",
            (_json.dumps(entry | {"availability": "retired"}),),
        )
    r = client.get("/v1/capabilities")
    assert r.status_code == 200 and "ocr" not in {c["id"] for c in r.json()["capabilities"]}


def test_v01_catalog_file_is_still_read(make_client, tmp_path):
    path = tmp_path / "caps.json"
    old = {"id": "old", "title": "Old", "availability": "available", "how": "POST /v1/x", "details": "Legacy."}
    path.write_text(json.dumps({"capabilities": [old]}), encoding="utf-8")
    (cap,) = make_client(capabilities_file=path).get("/v1/capabilities").json()["capabilities"]
    assert cap["summary"] == "Legacy." and cap["access"] == [{"kind": "http", "value": "POST /v1/x"}]
