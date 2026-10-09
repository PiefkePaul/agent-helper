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


def test_head_reads_do_not_take_the_write_lock_when_nothing_is_due(make_client):
    client = make_client(board_checkpoint_seconds=3600)
    _post(client, "x")  # records the first checkpoint
    store = client.app.state.store
    calls = []
    real = store._tx
    store._tx = lambda: calls.append(1) or real()
    client.get("/v1/board/head")
    client.get("/v1/board/checkpoints")
    assert calls == []


def test_checkpoint_times_never_go_backwards(make_client, monkeypatch, caplog):
    import time as time_module

    client = make_client(board_checkpoint_seconds=0)
    _post(client, "one")
    store = client.app.state.store
    real = time_module.time
    monkeypatch.setattr(store_module.time, "time", lambda: real() - 7200)  # the clock jumps back two hours
    _post(client, "two")
    assert [c["seq"] for c in client.get("/v1/board/checkpoints").json()["checkpoints"]] == [1]
    assert store.clock_behind
    assert "earlier than the last board checkpoint" in caplog.text
    monkeypatch.setattr(store_module.time, "time", real)
    _post(client, "three")
    assert [c["seq"] for c in client.get("/v1/board/checkpoints").json()["checkpoints"]] == [1, 3]
    assert not store.clock_behind


def test_signing_key_from_a_file(make_client, tmp_path):
    first = make_client(board_checkpoint_seconds=0)
    _post(first, "signed with the database key")
    db_key = first.app.state.store.public_key
    key_file = tmp_path / "instance.key"
    private = keys.new_private_key()
    key_file.write_text(private + "\n")
    second = make_client(board_checkpoint_seconds=0, instance_signing_key_file=key_file)
    store = second.app.state.store
    assert store.public_key == keys.public_key_of(private) != db_key
    # The database key lies in every backup, so moving to a key file revokes it.
    db_id = keys.key_id(db_key)
    assert list(store.other_keys) == [db_id]
    assert store.other_keys[db_id]["status"] == "revoked" and store.other_keys[db_id]["public_key"] == db_key
    described = second.get("/.well-known/agent-helper.json").json()["instance_key"]
    assert described["public_key"] == store.public_key and described["previous_keys"] == store.other_keys
    _post(second, "signed with the file key")
    cps = second.get("/v1/board/checkpoints").json()["checkpoints"]
    args = (_all_entries(second), cps, store.instance, store.public_key, store.other_keys)
    assert board.verify_checkpoints(*args) == []
    assert board.checkpoint_notes(*args) == [f"checkpoint #1: signed with a revoked key ({db_id}), ignored"]
    # A checkpoint with the revoked key never counts against the chain, whatever it says...
    lying = [dict(cps[0], entry_hash="0" * 64)]
    assert board.verify_checkpoints(args[0], lying, *args[2:]) == []
    # ...while one claiming the current key must verify.
    forged = [dict(cps[1], signature=keys.sign(keys.new_private_key(), b"x"))]
    assert board.verify_checkpoints(args[0], forged, *args[2:]) == ["checkpoint #2: signature does not verify"]


def test_an_unreadable_key_file_stops_the_start(make_client, tmp_path):
    bad = tmp_path / "bad.key"
    bad.write_text("not hex")
    with pytest.raises(SystemExit):
        make_client(instance_signing_key_file=bad)
    with pytest.raises(SystemExit):
        make_client(instance_signing_key_file=tmp_path / "missing.key")


def test_a_fresh_database_with_a_key_file_has_no_unused_key(make_client, tmp_path):
    key_file = tmp_path / "instance.key"
    key_file.write_text(keys.new_private_key())
    client = make_client(instance_signing_key_file=key_file)
    store = client.app.state.store
    assert store.other_keys == {}
    assert store._meta_value("signing_key") is None


def test_a_checkpoint_far_in_the_future_does_not_block_new_ones(make_client, monkeypatch, caplog):
    import time as time_module

    client = make_client(board_checkpoint_seconds=3600)
    store = client.app.state.store
    real = time_module.time
    monkeypatch.setattr(store_module.time, "time", lambda: real() + 30 * 86400)
    monkeypatch.setattr(store_module, "now", lambda: "2099-01-01T00:00:00Z")
    _post(client, "made while the clock was a month ahead")
    monkeypatch.setattr(store_module.time, "time", real)
    monkeypatch.undo()
    _post(client, "made after the clock was fixed")
    cps = client.get("/v1/board/checkpoints").json()["checkpoints"]
    assert [c["seq"] for c in cps] == [1, 2]
    assert "clock error" in caplog.text
    args = (_all_entries(client), cps, store.instance, store.public_key, store.other_keys)
    assert board.verify_checkpoints(*args) == []
    assert board.checkpoint_notes(*args) == ["checkpoint #2: its time is earlier than the one before (a clock error)"]


def test_rotated_keys_still_count(make_client):
    client = make_client(board_checkpoint_seconds=0)
    _post(client, "x")
    store = client.app.state.store
    old = keys.new_private_key()
    registry = {}
    kid = keys.add_previous_key(registry, old, "rotated", "2026-10-01T00:00:00Z", "test")
    statement = keys.checkpoint_statement(
        store.instance, 1, _all_entries(client)[0]["entry_hash"], "2026-10-01T00:00:00Z"
    )
    cp = {"seq": 1, "entry_hash": _all_entries(client)[0]["entry_hash"], "time": "2026-10-01T00:00:00Z"}
    cp |= {"key_id": kid, "signature": keys.sign(old, statement)}
    args = (_all_entries(client), [cp], store.instance, store.public_key, registry)
    assert board.verify_checkpoints(*args) == []
    assert board.checkpoint_notes(*args) == [f"checkpoint #1: made with another key ({kid}, rotated)"]
    # A rotated key does vouch: a checkpoint made with it that disagrees with the chain is an error.
    assert board.verify_checkpoints(args[0], [dict(cp, entry_hash="0" * 64)], *args[2:]) != []
    # Revocation is one-way.
    keys.add_previous_key(registry, old, "revoked", "2026-10-02T00:00:00Z", "test")
    keys.add_previous_key(registry, old, "rotated", "2026-10-03T00:00:00Z", "test")
    assert registry[kid]["status"] == "revoked"


def test_configured_revocations_survive_a_restore(make_client, tmp_path):
    import shutil

    first = make_client(board_checkpoint_seconds=0)
    _post(first, "signed with the first key")
    old_id, old_key = first.app.state.store.key_id, first.app.state.store.public_key
    first.__exit__(None, None, None)
    backup = tmp_path / "backup.db"
    shutil.copy(tmp_path / "agent-helper.db", backup)

    key_file = tmp_path / "instance.key"
    key_file.write_text(keys.new_private_key())
    # The database is restored from the backup, which knows nothing of any revocation; the configured
    # list still applies.
    shutil.copy(backup, tmp_path / "agent-helper.db")
    client = make_client(
        board_checkpoint_seconds=0, instance_signing_key_file=key_file, revoked_key_ids=((old_id, None),)
    )
    store = client.app.state.store
    assert store.other_keys[old_id]["status"] == "revoked" and store.other_keys[old_id]["public_key"] == old_key
    unknown = "0123456789abcdef"
    other = make_client(instance_signing_key_file=key_file, revoked_key_ids=((old_id, None), (unknown, None)))
    entry = other.app.state.store.other_keys[unknown]
    assert entry["status"] == "revoked" and entry["public_key"] == ""
    cp = {"seq": 1, "entry_hash": "0" * 64, "time": "2026-01-01T00:00:00Z", "key_id": unknown, "signature": "x"}
    args = (_all_entries(other), [cp], other.app.state.store.instance, other.app.state.store.public_key)
    assert board.verify_checkpoints(*args, other.app.state.store.other_keys) == []


def test_the_current_key_cannot_be_listed_as_revoked(make_client):
    client = make_client()
    current = client.app.state.store.key_id
    with pytest.raises(SystemExit):
        make_client(revoked_key_ids=((current, None),))


def test_malformed_revoked_key_ids_stop_the_start(monkeypatch):
    from agent_helper.config import Settings

    monkeypatch.setenv("REVOKED_KEY_IDS", "0123456789abcdef, not-a-key")
    with pytest.raises(SystemExit):
        Settings.from_env()
    monkeypatch.setenv("REVOKED_KEY_IDS", " 0123456789ABCDEF ,")
    assert Settings.from_env().revoked_key_ids == (("0123456789abcdef", None),)
    monkeypatch.setenv("REVOKED_KEY_IDS", "0123456789abcdef@2026-10-09T12:00:00Z")
    assert Settings.from_env().revoked_key_ids == (("0123456789abcdef", "2026-10-09T12:00:00Z"),)
    monkeypatch.setenv("REVOKED_KEY_IDS", "0123456789abcdef@yesterday")
    with pytest.raises(SystemExit):
        Settings.from_env()


def test_revocation_times_are_never_moved_later(make_client, tmp_path):
    key_file = tmp_path / "instance.key"
    key_file.write_text(keys.new_private_key())
    kid = "0123456789abcdef"
    first = make_client(instance_signing_key_file=key_file, revoked_key_ids=((kid, "2026-10-01T00:00:00Z"),))
    assert first.app.state.store.other_keys[kid]["since"] == "2026-10-01T00:00:00Z"
    first.__exit__(None, None, None)
    later = make_client(instance_signing_key_file=key_file, revoked_key_ids=((kid, "2026-10-05T00:00:00Z"),))
    assert later.app.state.store.other_keys[kid]["since"] == "2026-10-01T00:00:00Z"
    later.__exit__(None, None, None)
    undated = make_client(instance_signing_key_file=key_file, revoked_key_ids=((kid, None),))
    assert undated.app.state.store.other_keys[kid]["since"] == "2026-10-01T00:00:00Z"
    undated.__exit__(None, None, None)
    earlier = make_client(instance_signing_key_file=key_file, revoked_key_ids=((kid, "2026-09-01T00:00:00Z"),))
    assert earlier.app.state.store.other_keys[kid]["since"] == "2026-09-01T00:00:00Z"
    fresh = "fedcba9876543210"
    undated_new = make_client(instance_signing_key_file=key_file, revoked_key_ids=((fresh, None),))
    assert "without a date" in undated_new.app.state.store.other_keys[fresh]["reason"]
