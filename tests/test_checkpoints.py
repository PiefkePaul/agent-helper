"""Signed checkpoints of the board head (docs/decisions/0022)."""

import sqlite3

import pytest

from agent_helper import board, keys
from agent_helper import store as store_module


def _post(client, content):
    r = client.post("/v1/board", json={"content": content})
    assert r.status_code == 201, r.text
    return r.json()


def _all_entries(client):
    return client.get("/v1/board", params={"limit": 200}).json()


def test_head_is_signed_by_the_published_instance_key(client):
    _post(client, "first")
    head = client.get("/v1/board/head").json()
    described = client.get("/.well-known/agent-helper.json").json()
    key = described["instance_key"]
    assert key["key_id"] == keys.key_id(key["public_key"]) == head["key_id"]
    statement = keys.checkpoint_statement(described["instance_id"], head["seq"], head["entry_hash"], head["time"])
    assert keys.verify(key["public_key"], head["signature"], statement)
    # A statement about another head does not verify with this signature.
    other = keys.checkpoint_statement(described["instance_id"], head["seq"], "0" * 64, head["time"])
    assert not keys.verify(key["public_key"], head["signature"], other)


def test_empty_board_head_is_signed_too(client):
    head = client.get("/v1/board/head").json()
    assert head["seq"] == 0 and head["entry_hash"] == board.GENESIS_HASH and head["signature"]
    assert client.get("/v1/board/checkpoints").json()["checkpoints"] == []


def test_checkpoints_are_recorded_at_most_once_per_interval(make_client):
    client = make_client(board_checkpoint_seconds=3600)
    _post(client, "one")
    _post(client, "two")
    listed = client.get("/v1/board/checkpoints").json()
    assert [c["seq"] for c in listed["checkpoints"]] == [1]  # the second post came within the hour
    assert listed["public_key"] == client.app.state.store.public_key
    assert listed["next_after"] == 1


def test_a_checkpoint_follows_the_head_after_the_interval(make_client):
    client = make_client(board_checkpoint_seconds=0)
    for text in ("one", "two", "three"):
        _post(client, text)
    cps = client.get("/v1/board/checkpoints").json()["checkpoints"]
    assert [c["seq"] for c in cps] == [1, 2, 3]
    page = client.get("/v1/board/checkpoints", params={"after": 1, "limit": 1}).json()
    assert [c["seq"] for c in page["checkpoints"]] == [2] and page["next_after"] == 2
    instance = client.app.state.store.instance
    public_key = client.app.state.store.public_key
    assert board.verify_checkpoints(_all_entries(client), cps, instance, public_key) == []


def test_a_rewritten_chain_contradicts_its_checkpoints(make_client):
    client = make_client(board_checkpoint_seconds=0)
    _post(client, "original")
    cps = client.get("/v1/board/checkpoints").json()["checkpoints"]
    store = client.app.state.store
    entries = _all_entries(client)
    forged = [dict(entries[0], entry_hash="f" * 64)]
    problems = board.verify_checkpoints(forged, cps, store.instance, store.public_key)
    assert problems == ["checkpoint #1: the chain differs from what was signed"]
    tampered = [dict(cps[0], signature=keys.sign(keys.new_private_key(), b"x"))]
    assert (
        "signature does not verify" in board.verify_checkpoints(entries, tampered, store.instance, store.public_key)[0]
    )
    assert "no such entry" in board.verify_checkpoints([], cps, store.instance, store.public_key)[0]


def test_checkpoints_cannot_be_changed_or_deleted(make_client):
    client = make_client(board_checkpoint_seconds=0)
    _post(client, "x")
    db = client.app.state.store._db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE board_checkpoints SET entry_hash = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM board_checkpoints")


def test_the_key_survives_a_restart(make_client, tmp_path):
    first = make_client()
    key = first.app.state.store.public_key
    second = make_client()  # same data directory
    assert second.app.state.store.public_key == key


def test_hiding_and_purging_keep_checkpoints_valid(make_client, admin_headers):
    client = make_client(board_checkpoint_seconds=0)
    _post(client, "will be hidden")
    _post(client, "will be purged")
    client.post("/admin/v1/board/1/hide", json={"reason": "spam"}, headers=admin_headers)
    client.post("/admin/v1/board/2/purge", json={"reason": "legal", "confirm": "PURGE 2"}, headers=admin_headers)
    store = client.app.state.store
    cps = client.get("/v1/board/checkpoints").json()["checkpoints"]
    assert board.verify_checkpoints(_all_entries(client), cps, store.instance, store.public_key) == []


def test_expiry_keeps_checkpoints_valid(make_client, monkeypatch):
    client = make_client(board_checkpoint_seconds=0)
    client.post("/v1/board", json={"content": "short-lived", "expires_in_days": 1})
    monkeypatch.setattr(store_module, "now", lambda: "2099-01-01T00:00:00Z")
    _post(client, "later")
    store = client.app.state.store
    cps = client.get("/v1/board/checkpoints").json()["checkpoints"]
    assert board.verify_checkpoints(_all_entries(client), cps, store.instance, store.public_key) == []


def test_llms_txt_mentions_checkpoints(client):
    assert "/v1/board/checkpoints" in client.get("/llms.txt").text
