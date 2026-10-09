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
    assert (head["seq"], head["entry_hash"]) == (2, second["entry_hash"])

    entries = client.get("/v1/board").json()
    result = board.verify_chain(entries)
    assert result.ok and result.checked == 2 and result.head_hash == head["entry_hash"]


def test_empty_board_head_is_genesis(client):
    head = client.get("/v1/board/head").json()
    assert (head["seq"], head["entry_hash"]) == (0, board.GENESIS_HASH)


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


def test_legal_purge(client, admin_headers):
    import sqlite3

    import pytest

    from agent_helper.board import verify_chain

    client.post("/v1/board", json={"content": "keep"})
    client.post("/v1/board", json={"content": "Unlawful text about Mx Example", "tags": ["x"]})
    url = "/admin/v1/board/2/purge"
    assert (
        client.post(url, json={"reason": "court order", "confirm": "PURGE 1"}, headers=admin_headers).status_code == 422
    )
    assert client.post(url, json={"reason": "court order", "confirm": "PURGE 2"}).status_code == 401
    r = client.post(url, json={"reason": "court order 123", "confirm": "PURGE 2"}, headers=admin_headers)
    assert r.status_code == 200
    entry = r.json()
    assert entry["hidden"] is True and entry["content"] is None
    assert entry["hidden_reason"] == "Removed for legal reasons: court order 123"

    store = client.app.state.store
    with store._lock:
        assert store._db.execute("SELECT * FROM board_payloads WHERE seq = 2").fetchone() is None
        assert store._db.execute("SELECT * FROM board_search WHERE seq = 2").fetchone() is None
        with pytest.raises(sqlite3.DatabaseError):
            store._db.execute("DELETE FROM board_purged")
    assert client.get("/v1/board/search", params={"q": "unlawful"}).json()["entries"] == []

    entries = client.get("/v1/board").json()
    assert verify_chain(entries).ok
    # A purged entry carries its public reason, so it is not one "withheld without a reason" (#10).
    assert entries[1]["hidden_reason"]
    again = client.post(url, json={"reason": "again", "confirm": "PURGE 2"}, headers=admin_headers)
    assert again.status_code == 409
    missing = client.post("/admin/v1/board/9/purge", json={"reason": "x", "confirm": "PURGE 9"}, headers=admin_headers)
    assert missing.status_code == 404


def test_purging_a_note_with_an_expiry_keeps_the_chain_intact(client, admin_headers):
    from agent_helper.board import verify_chain

    note = client.post("/v1/board", json={"content": "temporary", "tags": ["t"], "expires_in_days": 30}).json()
    client.post("/v1/board", json={"content": "after"})
    r = client.post(
        f"/admin/v1/board/{note['seq']}/purge",
        json={"reason": "court order", "confirm": f"PURGE {note['seq']}"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    purged = r.json()
    assert purged["expires_at"] == note["expires_at"] and purged["expired"] is False and purged["hidden"] is True
    assert verify_chain(client.get("/v1/board").json()).ok
    # Later, once the expiry has passed, it still verifies.
    assert verify_chain(client.get("/v1/board").json(), now="2999-01-01T00:00:00Z").ok


def test_verifier_warns_about_entries_withheld_without_a_reason(client, admin_headers):
    from agent_helper.board import verify_chain

    for text in ("one", "two", "three"):
        client.post("/v1/board", json={"content": text})
    client.post("/admin/v1/board/2/hide", json={"reason": "spam"}, headers=admin_headers)
    clean = verify_chain(client.get("/v1/board").json())
    assert clean.ok and clean.warnings == ()

    # Someone with database access removes a payload without hiding it publicly.
    store = client.app.state.store
    with store._lock:
        store._db.execute("DELETE FROM board_search WHERE seq = 3")
        store._db.execute("DELETE FROM board_payloads WHERE seq = 3")
    entries = client.get("/v1/board").json()
    assert entries[2]["hidden"] is True and entries[2]["hidden_reason"] is None
    result = verify_chain(entries)
    assert result.ok and result.warnings == ("entry 3 is withheld without a public reason",)


def test_console_verify_shows_the_warning(make_client):
    import re

    from conftest import ADMIN_SECRET

    client = make_client(admin_cookie_secure=False)  # the test client speaks plain http (see #20)
    client.post("/v1/board", json={"content": "x"})
    store = client.app.state.store
    with store._lock:
        store._db.execute("DELETE FROM board_search WHERE seq = 1")
        store._db.execute("DELETE FROM board_payloads WHERE seq = 1")
    client.post("/admin/login", data={"secret": ADMIN_SECRET})
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/admin/console").text).group(1)
    page = client.post("/admin/console/board/verify", data={"csrf": csrf}).text
    assert "1 warning" in page and "without a public reason" in page
