from agent_helper.maintenance import main, new_instance_id


def test_new_instance_id_for_a_staging_copy(make_client, tmp_path, monkeypatch, capsys):
    client = make_client()
    old = client.app.state.store.instance
    client.__exit__(None, None, None)

    db = tmp_path / "agent-helper.db"
    assert new_instance_id(db) != old
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["new-instance-id"]) == 2  # needs --yes
    assert main(["new-instance-id", "--yes"]) == 0
    assert "new instance id" in capsys.readouterr().out

    copy = make_client()
    assert copy.app.state.store.instance not in (old, "")
    assert copy.app.state.store.instance.startswith("ah-")
