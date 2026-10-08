import sqlite3

import pytest

from agent_helper import board


def _post(client, content, **extra):
    r = client.post("/v1/board", json={"content": content, **extra})
    assert r.status_code == 201
    return r.json()


def test_chain_links_and_verifies(client):
    first = _post(client, "Hello future agents.", author="a1", topic="intro")
    second = _post(client, "Grüße — non-ASCII must hash the same everywhere. 🛰️")
    assert first["seq"] == 1 and first["prev_hash"] == board.GENESIS_HASH
    assert second["prev_hash"] == first["entry_hash"]

    head = client.get("/v1/board/head").json()
    assert head == {"seq": 2, "entry_hash": second["entry_hash"]}

    entries = client.get("/v1/board").json()
    result = board.verify_chain(entries)
    assert result.ok and result.checked == 2 and result.head_hash == head["entry_hash"]


def test_empty_board_head_is_genesis(client):
    assert client.get("/v1/board/head").json() == {"seq": 0, "entry_hash": board.GENESIS_HASH}


def test_verifier_detects_tampered_content(client):
    _post(client, "original")
    _post(client, "second")
    entries = client.get("/v1/board").json()
    entries[0]["content"] = "altered"
    result = board.verify_chain(entries)
    assert not result.ok and result.failed_seq == 1


def test_verifier_detects_removed_entry(client):
    for i in range(3):
        _post(client, f"m{i}")
    entries = client.get("/v1/board").json()
    del entries[1]
    assert not board.verify_chain(entries).ok


def test_storage_rejects_updates_and_deletes(client, tmp_path):
    _post(client, "immutable")
    db = sqlite3.connect(tmp_path / "agent-helper.db")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE board_chain SET entry_hash = 'x' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM board_chain WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE board_payloads SET content = 'changed' WHERE seq = 1")
    db.close()


def test_hidden_entry_withholds_payload_but_chain_still_verifies(client, admin_headers):
    _post(client, "fine")
    bad = _post(client, "abusive content", author="troll")
    _post(client, "after")

    r = client.post(f"/admin/v1/board/{bad['seq']}/hide", json={"reason": "abuse"}, headers=admin_headers)
    assert r.status_code == 200

    entry = client.get(f"/v1/board/{bad['seq']}").json()
    assert entry["hidden"] and entry["content"] is None and entry["author"] is None
    assert entry["hidden_reason"] == "abuse"
    assert entry["entry_hash"] == bad["entry_hash"]

    assert board.verify_chain(client.get("/v1/board").json()).ok


def test_board_pagination(client):
    for i in range(5):
        _post(client, f"m{i}")
    page = client.get("/v1/board?after=2&limit=2").json()
    assert [e["seq"] for e in page] == [3, 4]


def test_board_content_limit(client):
    assert client.post("/v1/board", json={"content": "x" * 4001}).status_code == 422
