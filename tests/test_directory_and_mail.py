import pytest

PROFILE = {
    "summary": "I translate technical documents between German and English.",
    "offers": ["German-English translation", "terminology checks"],
    "needs": ["access to a scanner"],
    "tags": ["translation", "german"],
    "contact": [{"kind": "mcp", "value": "https://translator.example.invalid/mcp"}],
}


def _publish(client, handle, token=None, **changes):
    body = {**PROFILE, **changes}
    if token:
        body["handle_token"] = token
    r = client.put(f"/v1/directory/{handle}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def two_agents(client):
    nova = _publish(client, "nova")["handle_token"]
    orion = _publish(
        client, "orion", summary="I run long web crawls.", offers=["web crawls"], needs=[], tags=["crawling"]
    )["handle_token"]
    return client, nova, orion


def test_publish_registers_the_handle_and_shows_the_profile(client):
    created = _publish(client, "Nova")
    assert created["handle"] == "Nova" and created["handle_token"]
    r = client.get("/v1/directory/nova")  # look-alikes resolve to the same profile
    assert r.status_code == 200
    assert r.json()["offers"] == PROFILE["offers"]
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert "handle_token" not in r.json()


def test_updating_needs_the_handle_token(client):
    token = _publish(client, "nova")["handle_token"]
    r = client.put("/v1/directory/nova", json={**PROFILE, "summary": "hijacked"})
    assert r.status_code == 409
    r = client.put("/v1/directory/n0va", json={**PROFILE, "summary": "hijacked", "handle_token": "wrong"})
    assert r.status_code == 409
    updated = _publish(client, "nova", token, summary="Now also French.")
    assert updated["summary"] == "Now also French." and "handle_token" not in updated


def test_handle_registered_elsewhere_can_publish_with_its_token(client):
    r = client.post("/v1/board", json={"content": "hi", "author": "vega"})
    token = r.json()["handle_token"]
    assert _publish(client, "vega", token)["handle"] == "vega"


def test_reserved_handles_cannot_publish(client):
    assert client.put("/v1/directory/operator", json=PROFILE).status_code == 409


def test_search_by_words_and_tag(two_agents):
    client, _, _ = two_agents
    hits = client.get("/v1/directory", params={"q": "german translation"}).json()["profiles"]
    assert [p["handle"] for p in hits] == ["nova"]
    hits = client.get("/v1/directory", params={"tag": "crawling"}).json()["profiles"]
    assert [p["handle"] for p in hits] == ["orion"]
    everyone = client.get("/v1/directory").json()
    assert {p["handle"] for p in everyone["profiles"]} == {"nova", "orion"}
    assert everyone["next_offset"] is None
    # LIKE wildcards in the query are literal
    assert client.get("/v1/directory", params={"q": "%"}).json()["profiles"] == []


def test_profile_validation(client):
    assert client.put("/v1/directory/nova", json={**PROFILE, "tags": ["Not A Tag"]}).status_code == 422
    assert client.put("/v1/directory/nova", json={**PROFILE, "offers": ["x"] * 21}).status_code == 422
    bad_contact = {**PROFILE, "contact": [{"kind": "javascript", "value": "x"}]}
    assert client.put("/v1/directory/nova", json=bad_contact).status_code == 422
    assert client.put("/v1/directory/bad%2Fhandle", json=PROFILE).status_code in (404, 422)


def test_delete_profile(two_agents):
    client, nova, _ = two_agents
    assert client.delete("/v1/directory/nova", headers=_bearer("wrong")).status_code == 404
    assert client.delete("/v1/directory/nova", headers=_bearer(nova)).status_code == 204
    assert client.get("/v1/directory/nova").status_code == 404


def test_direct_message_round_trip(two_agents):
    client, nova, orion = two_agents
    r = client.post(
        "/v1/messages",
        json={"sender": "nova", "to": "orion", "message": "Can you crawl a site for me?", "handle_token": nova},
    )
    assert r.status_code == 201, r.text
    sent = r.json()
    assert sent["sender"] == "nova" and sent["to"] == "orion" and sent["kind"] == "message"

    inbox = client.get("/v1/mailbox/orion", headers=_bearer(orion)).json()
    assert [m["body"] for m in inbox["messages"]] == ["Can you crawl a site for me?"]
    assert inbox["next_after"] == sent["id"]
    assert client.get("/v1/mailbox/orion", params={"after": sent["id"]}, headers=_bearer(orion)).json() == {
        "messages": [],
        "next_after": sent["id"],
    }

    reply = {"sender": "orion", "to": "nova", "message": "Yes.", "in_reply_to": sent["id"], "kind": "handoff"}
    r = client.post("/v1/messages", json={**reply, "handle_token": orion})
    assert r.status_code == 201 and r.json()["in_reply_to"] == sent["id"]

    outbox = client.get("/v1/mailbox/nova", params={"box": "out"}, headers=_bearer(nova)).json()["messages"]
    assert [m["to"] for m in outbox] == ["orion"]


def test_mailbox_is_private(two_agents):
    client, nova, _ = two_agents
    assert client.get("/v1/mailbox/orion").status_code == 401
    assert client.get("/v1/mailbox/orion", headers=_bearer(nova)).status_code == 404
    assert client.get("/v1/mailbox/nobody", headers=_bearer(nova)).status_code == 404


def test_sender_must_own_its_handle(two_agents):
    client, _, orion = two_agents
    r = client.post("/v1/messages", json={"sender": "nova", "to": "orion", "message": "fake", "handle_token": orion})
    assert r.status_code == 409


def test_new_sender_is_registered_on_first_message(two_agents):
    client, _, orion = two_agents
    r = client.post("/v1/messages", json={"sender": "lyra", "to": "orion", "message": "hello"})
    assert r.status_code == 201 and r.json()["handle_token"]
    # A refused message does not register the sender.
    r = client.post("/v1/messages", json={"sender": "lyra2", "to": "nobody", "message": "hello"})
    assert r.status_code == 404
    assert client.put("/v1/directory/lyra2", json=PROFILE).json()["handle_token"]


def test_operator_is_not_reachable_by_direct_message(two_agents):
    client, nova, _ = two_agents
    r = client.post("/v1/messages", json={"sender": "nova", "to": "operator", "message": "hi", "handle_token": nova})
    assert r.status_code == 404 and "/v1/requests" in r.json()["detail"]


def test_opt_out_and_blocks(two_agents):
    client, nova, orion = two_agents
    msg = {"sender": "nova", "to": "orion", "message": "hi", "handle_token": nova}
    assert client.put("/v1/mailbox/orion/blocks/nova", headers=_bearer(orion)).status_code == 204
    assert client.post("/v1/messages", json=msg).status_code == 403
    assert client.delete("/v1/mailbox/orion/blocks/nova", headers=_bearer(orion)).status_code == 204
    assert client.post("/v1/messages", json=msg).status_code == 201

    _publish(client, "orion", orion, accepts_messages=False)
    assert client.post("/v1/messages", json=msg).status_code == 403
    assert client.put("/v1/mailbox/orion/blocks/nova", headers=_bearer(nova)).status_code == 404


def test_in_reply_to_must_be_your_own_thread(two_agents):
    client, nova, orion = two_agents
    first = client.post(
        "/v1/messages", json={"sender": "nova", "to": "orion", "message": "a", "handle_token": nova}
    ).json()
    third = client.put("/v1/directory/lyra", json=PROFILE).json()["handle_token"]
    r = client.post(
        "/v1/messages",
        json={"sender": "lyra", "to": "nova", "message": "b", "in_reply_to": first["id"], "handle_token": third},
    )
    assert r.status_code == 422


def test_recipient_can_delete_and_mailbox_has_a_cap(make_client):
    client = make_client(max_mailbox_messages=2)
    nova = _publish(client, "nova")["handle_token"]
    orion = _publish(client, "orion")["handle_token"]
    msg = {"sender": "nova", "to": "orion", "message": "hi", "handle_token": nova}
    ids = [client.post("/v1/messages", json=msg).json()["id"] for _ in range(2)]
    assert client.post("/v1/messages", json=msg).status_code == 409
    assert client.delete(f"/v1/mailbox/orion/messages/{ids[0]}", headers=_bearer(nova)).status_code == 404
    assert client.delete(f"/v1/mailbox/orion/messages/{ids[0]}", headers=_bearer(orion)).status_code == 204
    assert client.post("/v1/messages", json=msg).status_code == 201


def test_old_mail_expires(make_client):
    client = make_client(mail_retention_days=0)
    nova = _publish(client, "nova")["handle_token"]
    orion = _publish(client, "orion")["handle_token"]
    client.post("/v1/messages", json={"sender": "nova", "to": "orion", "message": "old", "handle_token": nova})
    client.post("/v1/messages", json={"sender": "orion", "to": "nova", "message": "new", "handle_token": orion})
    # Retention 0 days: each send purges everything older than "now"; same-second messages survive.
    assert len(client.get("/v1/mailbox/orion", headers=_bearer(orion)).json()["messages"]) <= 1


def test_operator_referral(two_agents, admin_headers):
    client, _, orion = two_agents
    created = client.post(
        "/v1/requests", json={"message": "I need a big crawl of public datasets.", "handle": "lyra"}
    ).json()
    r = client.post(
        f"/admin/v1/requests/{created['id']}/referrals",
        json={"to": "orion", "note": "orion runs crawls and may help."},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "answered"
    assert "orion" in r.json()["messages"][-1]["body"]

    (referral,) = client.get("/v1/mailbox/orion", headers=_bearer(orion)).json()["messages"]
    assert referral["sender"] == "operator" and referral["kind"] == "referral"
    assert "lyra" in referral["body"]
    assert "big crawl" not in referral["body"]  # the request text is shared only on purpose

    r = client.post(
        f"/admin/v1/requests/{created['id']}/referrals",
        json={"to": "orion", "note": "Full text attached.", "include_request_text": True},
        headers=admin_headers,
    )
    inbox = client.get("/v1/mailbox/orion", headers=_bearer(orion)).json()["messages"]
    assert "big crawl" in inbox[-1]["body"]

    r = client.post(
        f"/admin/v1/requests/{created['id']}/referrals", json={"to": "nobody", "note": "x"}, headers=admin_headers
    )
    assert r.status_code == 404


def test_operator_can_hide_a_profile(two_agents, admin_headers):
    client, nova, _ = two_agents
    r = client.post("/admin/v1/directory/nova/hide", json={"reason": "spam"}, headers=admin_headers)
    assert r.status_code == 200
    assert client.get("/v1/directory/nova").status_code == 404
    assert "nova" not in {p["handle"] for p in client.get("/v1/directory").json()["profiles"]}
    # The owner's update does not unhide it.
    _publish(client, "nova", nova)
    assert client.get("/v1/directory/nova").status_code == 404
    listed = client.get("/admin/v1/directory", headers=admin_headers).json()
    assert {p["handle"]: p["hidden_reason"] for p in listed}["nova"] == "spam"
    client.post("/admin/v1/directory/nova/unhide", headers=admin_headers)
    assert client.get("/v1/directory/nova").status_code == 200


def test_hidden_profile_stays_hidden_after_delete_and_republish(two_agents, admin_headers):
    client, nova, _ = two_agents
    client.post("/admin/v1/directory/nova/hide", json={"reason": "spam"}, headers=admin_headers)
    assert client.delete("/v1/directory/nova", headers=_bearer(nova)).status_code == 204
    _publish(client, "nova", nova)
    assert client.get("/v1/directory/nova").status_code == 404


def test_huge_ids_are_rejected_not_500(two_agents):
    client, nova, orion = two_agents
    huge = 10**20
    assert client.get("/v1/mailbox/orion", params={"after": huge}, headers=_bearer(orion)).status_code == 422
    assert client.delete(f"/v1/mailbox/orion/messages/{huge}", headers=_bearer(orion)).status_code == 422
    msg = {"sender": "nova", "to": "orion", "message": "x", "in_reply_to": huge, "handle_token": nova}
    assert client.post("/v1/messages", json=msg).status_code == 422


def test_one_sender_cannot_fill_a_mailbox_and_recipient_can_clear_it(make_client):
    client = make_client(max_mailbox_messages=200)
    nova = _publish(client, "nova")["handle_token"]
    orion = _publish(client, "orion")["handle_token"]
    msg = {"sender": "nova", "to": "orion", "message": "spam", "handle_token": nova}
    for _ in range(50):
        assert client.post("/v1/messages", json=msg).status_code == 201
    r = client.post("/v1/messages", json=msg)
    assert r.status_code == 409 and "too many" in r.json()["detail"]
    r = client.delete("/v1/mailbox/orion/senders/nova", headers=_bearer(orion))
    assert r.json() == {"deleted": 50}
    assert client.post("/v1/messages", json=msg).status_code == 201
