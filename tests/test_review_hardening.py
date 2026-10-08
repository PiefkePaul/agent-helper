import logging

from agent_helper.limits import TokenBucket, client_key


def _scope(ip: str, xff: str | None = None) -> dict:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return {"type": "http", "client": (ip, 1234), "headers": headers}


def test_lone_surrogate_is_rejected_not_500(client):
    raw = rb'{"content": "a\ud800b"}'
    r = client.post("/v1/board", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422
    r = client.post("/v1/requests", content=rb'{"message": "\udfff"}', headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_c1_and_bidi_controls_are_rejected(client):
    for bad in ("\x9b31m", "abc‮evil", "zero​width", "﻿bom"):
        assert client.post("/v1/board", json={"content": bad}).status_code == 422, repr(bad)
    assert client.post("/v1/board", json={"content": "Umlaute äöü, emoji 🙂, 中文"}).status_code == 201


def test_ipv6_clients_share_a_bucket_per_64_prefix():
    assert client_key(_scope("2001:db8:1:2:aaaa::1"), False) == client_key(_scope("2001:db8:1:2:bbbb::9"), False)
    assert client_key(_scope("2001:db8:1:3::1"), False) != client_key(_scope("2001:db8:1:2::1"), False)
    assert client_key(_scope("203.0.113.7"), False) == "203.0.113.7"
    assert client_key(_scope("127.0.0.1", "2001:db8::1, 2001:db8:0:0:ffff::2"), True) == client_key(
        _scope("2001:db8::5"), False
    )


def test_ipv6_rotation_does_not_bypass_write_limit(make_client):
    client = make_client(write_per_minute=1, trust_proxy_headers=True)
    assert (
        client.post("/v1/board", json={"content": "a"}, headers={"X-Forwarded-For": "2001:db8::1"}).status_code == 201
    )
    r = client.post("/v1/board", json={"content": "b"}, headers={"X-Forwarded-For": "2001:db8::2"})
    assert r.status_code == 429


def test_global_write_budget_caps_distributed_writes(make_client):
    client = make_client(write_per_minute=100, global_write_per_minute=3, trust_proxy_headers=True)
    codes = [
        client.post("/v1/board", json={"content": "x"}, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
        for i in range(5)
    ]
    assert codes == [201, 201, 201, 429, 429]


def test_bucket_overflow_does_not_refill_active_clients():
    bucket = TokenBucket(per_minute=1, max_keys=10)
    assert bucket.take("victim-of-own-abuse", now=0.0) == 0
    for i in range(50):
        bucket.take(f"spray-{i}", now=0.0)
    assert bucket.take("victim-of-own-abuse", now=0.0) > 0


def test_access_log_escapes_control_characters_in_path(client, caplog):
    with caplog.at_level(logging.INFO, logger="agent_helper.access"):
        client.get("/v1/board/1%0AFAKE%20200%20OK")
    lines = [r.getMessage() for r in caplog.records if r.name == "agent_helper.access"]
    assert lines and all("\n" not in line for line in lines)


def test_conversation_length_is_capped(make_client):
    client = make_client(max_messages_per_request=3)
    r = client.post("/v1/requests", json={"message": "first"}).json()
    auth = {"Authorization": f"Bearer {r['follow_up_token']}"}
    assert client.post(f"/v1/requests/{r['id']}/messages", json={"message": "2"}, headers=auth).status_code == 200
    assert client.post(f"/v1/requests/{r['id']}/messages", json={"message": "3"}, headers=auth).status_code == 200
    assert client.post(f"/v1/requests/{r['id']}/messages", json={"message": "4"}, headers=auth).status_code == 409


def test_admin_status_filter_is_validated(client, admin_headers):
    assert client.get("/admin/v1/requests?status=bogus", headers=admin_headers).status_code == 422
    assert client.get("/admin/v1/reports?status=open", headers=admin_headers).status_code == 422
