import sqlite3

import agent_helper.store as store_module
from agent_helper.board import verify_chain

FUTURE = "2999-01-01T00:00:00Z"


def _post(client, **body):
    r = client.post("/v1/board", json={"content": "x", **body})
    assert r.status_code == 201, r.text
    return r.json()


def _all(client):
    return client.get("/v1/board", params={"limit": 200}).json()


def test_tagged_note_uses_scheme_2_and_plain_post_stays_1(client):
    plain = _post(client, content="hello")
    note = _post(client, content="The XYZ API allows 10 calls a minute.", tags=["api", "rate-limits"], topic="XYZ")
    assert plain["v"] == 1 and plain["tags"] is None and plain["expires_at"] is None
    assert note["v"] == 2 and note["tags"] == ["api", "rate-limits"]
    assert verify_chain(_all(client)).ok


def test_expiring_note_is_withheld_and_purged_but_the_chain_still_verifies(client, monkeypatch):
    note = _post(client, content="Temporary outage at ABC until Friday.", tags=["outage"], expires_in_days=3)
    assert note["v"] == 2 and note["expires_at"] > note["created_at"] and note["expired"] is False
    _post(client, content="permanent")
    head_before = client.get("/v1/board/head").json()

    monkeypatch.setattr(store_module, "now", lambda: FUTURE)
    seen = client.get(f"/v1/board/{note['seq']}").json()
    assert seen["expired"] is True and seen["content"] is None and seen["tags"] is None
    assert seen["hidden"] is False and seen["expires_at"] == note["expires_at"]

    _post(client, content="a later post purges expired payloads")
    store = client.app.state.store
    with store._lock:
        row = store._db.execute("SELECT * FROM board_payloads WHERE seq = ?", (note["seq"],)).fetchone()
    assert row is None
    purged = client.get(f"/v1/board/{note['seq']}").json()
    assert purged["expired"] is True and purged["hidden"] is False and purged["expires_at"] == note["expires_at"]

    entries = _all(client)
    assert verify_chain(entries).ok
    # The head from before the purge is still part of the chain.
    assert entries[head_before["seq"] - 1]["entry_hash"] == head_before["entry_hash"]


def test_search_notes(client, monkeypatch):
    _post(client, content="Use batching for the XYZ API.", tags=["api"], author="nova", topic="XYZ tips")
    _post(client, content="Nothing about apis here.", author="orion")
    _post(client, content="Short-lived api tip.", tags=["api"], expires_in_days=1)

    hits = client.get("/v1/board/search", params={"q": "xyz batching"}).json()["entries"]
    assert [h["author"] for h in hits] == ["nova"]
    tagged = client.get("/v1/board/search", params={"tag": "api"}).json()["entries"]
    assert len(tagged) == 2 and tagged[0]["content"] == "Short-lived api tip."  # newest first
    by_author = client.get("/v1/board/search", params={"author": "orion"}).json()["entries"]
    assert [h["content"] for h in by_author] == ["Nothing about apis here."]
    assert client.get("/v1/board/search", params={"q": "%"}).json()["entries"] == []
    assert client.get("/v1/board/search").headers["x-robots-tag"] == "noindex, nofollow"

    monkeypatch.setattr(store_module, "now", lambda: FUTURE)
    assert len(client.get("/v1/board/search", params={"tag": "api"}).json()["entries"]) == 1


def test_hidden_notes_are_not_found(client, admin_headers):
    note = _post(client, content="spam spam", tags=["spam"])
    client.post(f"/admin/v1/board/{note['seq']}/hide", json={"reason": "spam"}, headers=admin_headers)
    assert client.get("/v1/board/search", params={"tag": "spam"}).json()["entries"] == []


def test_note_validation(client):
    for bad in ({"tags": ["Not Valid"]}, {"tags": ["a"] * 11}, {"expires_in_days": 0}, {"expires_in_days": 3651}):
        assert client.post("/v1/board", json={"content": "x", **bad}).status_code == 422


def test_operator_notes_can_have_tags(client, admin_headers):
    r = client.post("/admin/v1/board", json={"content": "Welcome.", "tags": ["welcome"]}, headers=admin_headers)
    assert r.json()["author"] == "operator" and r.json()["tags"] == ["welcome"]


def test_existing_v01_database_is_migrated(tmp_path, make_client):
    db = sqlite3.connect(tmp_path / "agent-helper.db")
    db.executescript(
        """
        CREATE TABLE board_chain (seq INTEGER PRIMARY KEY, created_at TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
            prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL UNIQUE);
        CREATE TABLE board_payloads (seq INTEGER PRIMARY KEY, author TEXT, topic TEXT, content TEXT NOT NULL);
        """
    )
    from agent_helper import board

    p = board.payload_hash(None, None, "old post")
    e = board.entry_hash(1, "2026-10-01T00:00:00Z", p, board.GENESIS_HASH)
    db.execute("INSERT INTO board_chain VALUES (1, '2026-10-01T00:00:00Z', ?, ?, ?)", (p, board.GENESIS_HASH, e))
    db.execute("INSERT INTO board_payloads VALUES (1, NULL, NULL, 'old post')")
    db.commit()
    db.close()

    client = make_client()
    _post(client, content="new note", tags=["new"])
    entries = _all(client)
    assert [x["v"] for x in entries] == [1, 2] and entries[0]["content"] == "old post"
    assert verify_chain(entries).ok


def test_mcp_notes(client):
    from test_mcp import call

    posted = call(client, "post_board", {"content": "Tip: cache results.", "tags": ["tips"], "expires_in_days": 30})
    assert posted["structuredContent"]["v"] == 2
    found = call(client, "search_board", {"tag": "tips"})["structuredContent"]["entries"]
    assert [f["content"] for f in found] == ["Tip: cache results."]
