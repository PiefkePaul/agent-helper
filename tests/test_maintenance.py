import sqlite3

import pytest

from agent_helper.maintenance import LOCKED, MaintenanceError, main, new_instance_id, restore_instance_id, summary


@pytest.fixture
def stopped_db(make_client, tmp_path, monkeypatch):
    """A database the service has created, with the service stopped. Returns (path, instance id)."""
    client = make_client()
    instance = client.app.state.store.instance
    client.post("/v1/board", json={"content": "x"})
    client.__exit__(None, None, None)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path / "agent-helper.db", instance


def test_without_confirmation_only_the_summary_is_shown(stopped_db, capsys):
    db, instance = stopped_db
    assert main(["new-instance-id"]) == 2
    out = capsys.readouterr().out
    assert instance in out and "0 board entries, 0 messages" in out and f"--confirm {instance}" in out
    assert summary(db)["instance_id"] == instance  # nothing changed


def test_wrong_confirmation_changes_nothing(stopped_db, capsys):
    db, instance = stopped_db
    assert main(["new-instance-id", "--confirm", "ah-wrong"]) == 1
    assert "does not match" in capsys.readouterr().err
    assert summary(db)["instance_id"] == instance


def test_new_id_keeps_the_old_one_and_can_be_undone(stopped_db, make_client, capsys):
    db, instance = stopped_db
    assert main(["new-instance-id", "--confirm", instance]) == 0
    out = capsys.readouterr().out
    assert instance in out
    info = summary(db)
    new = info["instance_id"]
    assert new != instance and info["previous_instance_id"] == instance

    copy = make_client()
    assert copy.app.state.store.instance == new
    copy.__exit__(None, None, None)

    assert main(["restore-instance-id", "--confirm", "ah-wrong"]) == 1
    assert main(["restore-instance-id", "--confirm", new]) == 0
    assert summary(db)["instance_id"] == instance
    restored = make_client()
    assert restored.app.state.store.instance == instance


def test_restore_without_a_previous_id(stopped_db):
    db, instance = stopped_db
    with pytest.raises(MaintenanceError, match="no previous"):
        restore_instance_id(db, instance)


def test_locked_database_gives_a_clear_message(stopped_db, capsys):
    db, instance = stopped_db
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(MaintenanceError, match="locked"):
            new_instance_id(db, instance)
        assert main(["new-instance-id", "--confirm", instance]) == 1
        assert LOCKED in capsys.readouterr().err
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert summary(db)["instance_id"] == instance


def test_new_id_also_rotates_the_checkpoint_key(make_client, tmp_path, monkeypatch):
    client = make_client()
    instance, key = client.app.state.store.instance, client.app.state.store.public_key
    client.__exit__(None, None, None)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["new-instance-id", "--confirm", instance]) == 0
    copy = make_client()
    rotated = copy.app.state.store.public_key
    assert rotated != key
    copy.__exit__(None, None, None)
    assert main(["restore-instance-id", "--confirm", copy.app.state.store.instance]) == 0
    assert make_client().app.state.store.public_key == key
