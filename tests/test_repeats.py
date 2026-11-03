"""A question that was answered before gets the latest such answer at once (docs/decisions/0030)."""

import sqlite3

import pytest
from conftest import ADMIN_SECRET
from test_a2a import rpc, user_message
from test_console import console  # noqa: F401 (fixture)
from test_directory_and_mail import _publish
from test_mcp import call
from test_notify import WEBHOOK, Recorder

from agent_helper.autoreply import question_key
from agent_helper.config import Settings
from agent_helper.store import AutoReplySettings, Store

ADMIN = {"Authorization": f"Bearer {ADMIN_SECRET}"}
QUESTION = "Where can I find a free OCR service for scanned PDFs?"
ANSWER = "Try the public OCR endpoint listed in /v1/capabilities; it is free for small files."


def _ask(client, message=QUESTION, **extra):
    created = client.post("/v1/requests", json={"message": message, **extra}).json()
    return created, _view(client, created)


def _view(client, created):
    return client.get(
        f"/v1/requests/{created['id']}", headers={"Authorization": f"Bearer {created['follow_up_token']}"}
    ).json()


def _answer(client, req_id, message=ANSWER, status="answered"):
    r = client.post(f"/admin/v1/requests/{req_id}/replies", json={"message": message, "status": status}, headers=ADMIN)
    assert r.status_code == 200, r.text


@pytest.fixture
def client(make_client):
    """Most tests: one earlier occurrence is enough, so each case needs only one template."""
    return make_client(auto_reply_repeat_after=1)


def _notified(make_client, **overrides):
    client = make_client(notify_webhook_url=WEBHOOK, **({"auto_reply_repeat_after": 1} | overrides))
    rec = Recorder()
    client.app.state.notifier.transport = rec
    client.app.state.notifier.retry_delays = (0.0, 0.0)
    return client, rec


def test_only_whitespace_at_the_ends_is_ignored():
    assert question_key(f"  {QUESTION}" + chr(10)) == question_key(QUESTION)


@pytest.mark.parametrize(
    "other",
    [
        QUESTION.lower(),
        QUESTION.rstrip("?"),
        QUESTION + "?",
        QUESTION.replace(" OCR", "  OCR"),
        "Ｗ" + QUESTION[1:],  # fullwidth W
        "Where can I find a free OCR service for scanned PDF?",
        "Which flags for C?",
    ],
)
def test_any_other_text_is_another_question(other):
    assert question_key(other) != question_key(QUESTION)


def test_nothing_left_has_no_key():
    assert question_key(" " + chr(10) + chr(9) + " ") is None


def test_repeat_gets_the_earlier_answer_and_the_operator_is_told(make_client):
    client, rec = _notified(make_client)
    first, _ = _ask(client)
    _answer(client, first["id"])
    second, view = _ask(client, handle="lyra")
    assert view["status"] == "answered"
    assert [m["sender"] for m in view["messages"]] == ["agent", "operator"]
    assert view["messages"][1]["body"] == ANSWER and view["messages"][1]["automatic"] is True
    assert view["messages"][0]["automatic"] is False

    assert client.app.state.notifier.flush()
    events = [e["event"] for e in rec.events]
    assert events == ["request.created", "request.auto_answered"]  # no "New request" for the repeat
    auto = rec.events[1]
    assert auto["id"] == second["id"] and auto["source"] == first["id"] and auto["handle"] == "lyra"
    assert "answered automatically" in auto["text"] and "untrusted_preview" not in auto
    client.app.state.flush_usage()
    rows = {r["metric"]: r["count"] for r in client.app.state.store.usage(1)}
    assert rows["autoreply:repeat"] == 1

    admin = client.get(f"/admin/v1/requests/{second['id']}", headers=ADMIN).json()
    assert admin["messages"][1]["automatic"] is True


def test_auto_answers_are_not_templates_but_a_correction_is(client):
    first, _ = _ask(client)
    _answer(client, first["id"], "old answer")
    second, view = _ask(client)
    assert view["messages"][1]["body"] == "old answer"
    # The operator corrects the repeated answer: that correction is used from now on.
    _answer(client, second["id"], "corrected answer")
    _, view = _ask(client)
    assert view["messages"][1]["body"] == "corrected answer"


def test_status_is_copied(client):
    fresh, _ = _ask(client, "A brand new question nobody asked before")
    _answer(client, fresh["id"], "new answer, closed", status="closed")
    _, view = _ask(client, "A brand new question nobody asked before")
    assert view["status"] == "closed" and view["messages"][1]["body"] == "new answer, closed"


def test_an_agent_follow_up_after_a_repeat_stops_it_being_a_template(client):
    first, _ = _ask(client)
    _answer(client, first["id"], "old answer")
    second, _ = _ask(client)
    client.post(
        f"/v1/requests/{second['id']}/messages",
        json={"message": "That is not enough, I need German too."},
        headers={"Authorization": f"Bearer {second['follow_up_token']}"},
    )
    _answer(client, second["id"], "answer about German")
    _, view = _ask(client)
    assert view["messages"][1]["body"] == "old answer"


def test_only_a_reply_right_after_the_first_message_counts(client):
    first, _ = _ask(client)
    client.post(
        f"/v1/requests/{first['id']}/messages",
        json={"message": "Also, it has to handle German."},
        headers={"Authorization": f"Bearer {first['follow_up_token']}"},
    )
    _answer(client, first["id"])  # answers the follow-up too, so it is not a template
    _, view = _ask(client)
    assert view["status"] == "open" and len(view["messages"]) == 1


def test_holding_replies_and_referrals_are_not_repeated(two_agents):
    client, _, _ = two_agents
    first, _ = _ask(client)
    _answer(client, first["id"], "Looking into it, back soon.", status="open")
    _, view = _ask(client)
    assert view["status"] == "open" and len(view["messages"]) == 1

    asked, _ = _ask(client, "I need a big crawl of public datasets.")
    r = client.post(
        f"/admin/v1/requests/{asked['id']}/referrals", json={"to": "orion", "note": "orion can help."}, headers=ADMIN
    )
    assert r.status_code == 200
    _, view = _ask(client, "I need a big crawl of public datasets.")
    assert len(view["messages"]) == 1


def test_the_agent_can_continue_as_usual(make_client):
    client, rec = _notified(make_client)
    first, _ = _ask(client)
    _answer(client, first["id"], status="closed")
    second, view = _ask(client)
    assert view["status"] == "closed"
    r = client.post(
        f"/v1/requests/{second['id']}/messages",
        json={"message": "That did not work for me."},
        headers={"Authorization": f"Bearer {second['follow_up_token']}"},
    )
    assert r.json()["status"] == "open"
    assert client.app.state.notifier.flush()
    assert [e["event"] for e in rec.events][-1] == "request.message"


def test_repeats_over_mcp_and_a2a(client):
    first, _ = _ask(client)
    _answer(client, first["id"])
    task = rpc(client, "SendMessage", user_message(QUESTION)).json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["status"]["message"]["parts"] == [{"text": ANSWER}]
    result = call(client, "describe_need", {"message": QUESTION})["structuredContent"]
    read = call(client, "read_request", {"id": result["id"], "follow_up_token": result["follow_up_token"]})
    assert read["structuredContent"]["messages"][1] | {"created_at": None} == {
        "sender": "operator",
        "body": ANSWER,
        "automatic": True,
        "author": None,
        "created_at": None,
    }


def test_repeats_can_be_turned_off(make_client):
    client, rec = _notified(make_client, auto_reply_repeats=False)
    first, _ = _ask(client)
    _answer(client, first["id"])
    _, view = _ask(client)
    assert view["status"] == "open" and len(view["messages"]) == 1
    assert client.app.state.notifier.flush()
    assert [e["event"] for e in rec.events] == ["request.created", "request.created"]


def test_connectivity_checks_are_marked_and_never_templates(client):
    _, view = _ask(client, "ping")
    assert view["messages"][1]["automatic"] is True
    _, view = _ask(client, "ping")
    assert view["messages"][1]["body"].startswith("pong.")  # still the check answer, not a repeat


def test_console_marks_the_repeat(console):  # noqa: F811
    client, _ = console
    client.app.state.store.set_auto_reply_settings(repeat_after=1)
    first, _ = _ask(client)
    _answer(client, first["id"])
    second, _ = _ask(client)
    page = client.get(f"/admin/console/requests/{second['id']}").text
    assert f'automatic: your earlier answer to <a href="/admin/console/requests/{first["id"]}">' in page
    assert "automatic:" in client.get("/admin/console/requests").text


def test_older_databases_get_keys_once(tmp_path):
    path = tmp_path / "old.db"
    store = Store(path)  # as before: no question keys
    req_id, _, _ = store.create_request(QUESTION, None, None)
    store.add_operator_reply(req_id, ANSWER, "answered")
    with sqlite3.connect(path) as db:  # a reply written before reply_status existed
        db.execute("UPDATE request_messages SET reply_status = NULL")
    store = Store(path, question_key=question_key, auto_reply_defaults=AutoReplySettings(True, True, 1))
    new_id, token, _ = store.create_request(QUESTION, None, None)
    view = store.get_request(new_id, token)
    assert view["messages"][1]["body"] == ANSWER and view["messages"][1]["reused_from"] == req_id


def test_setting_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_REPLY_REPEATS", "false")
    assert Settings.from_env().auto_reply_repeats is False
    monkeypatch.delenv("AUTO_REPLY_REPEATS")
    assert Settings.from_env().auto_reply_repeats is True
    assert Settings.from_env().auto_reply_repeat_after == 3
    monkeypatch.setenv("AUTO_REPLY_REPEAT_AFTER", "5")
    assert Settings.from_env().auto_reply_repeat_after == 5


@pytest.fixture
def two_agents(client):
    _publish(client, "orion", summary="I run long web crawls.", offers=["web crawls"], needs=[], tags=["crawling"])
    return client, None, None


def test_the_answer_after_a_holding_reply_is_a_template(client):
    first, _ = _ask(client)
    _answer(client, first["id"], "Looking into it.", status="open")
    _answer(client, first["id"], "Use the OCR endpoint.")
    _, view = _ask(client)
    assert view["messages"][1]["body"] == "Use the OCR endpoint."


def test_a2a_metadata_marks_the_repeat(client):
    first, _ = _ask(client)
    _answer(client, first["id"])
    task = rpc(client, "SendMessage", user_message(QUESTION)).json()["result"]["task"]
    assert [m["metadata"]["automatic"] for m in task["history"]] == [False, True]


def test_by_default_only_the_fourth_identical_request_is_answered(make_client):
    client = make_client()
    first, _ = _ask(client, "Hello! In one or two sentences, what can you help with?")
    _answer(client, first["id"], "I connect agents with tools and a human operator.")
    for _ in range(2):  # the 2nd and 3rd time: still for the operator
        _, view = _ask(client, "Hello! In one or two sentences, what can you help with?")
        assert view["status"] == "open" and len(view["messages"]) == 1
    _, view = _ask(client, "Hello! In one or two sentences, what can you help with?")
    assert view["messages"][1]["body"] == "I connect agents with tools and a human operator."


BOT_TOKEN = "bot-token-" + "y" * 32


def test_the_bot_can_read_and_change_the_settings(make_client):
    client = make_client(operator_bot_token=BOT_TOKEN)
    bot = {"Authorization": f"Bearer {BOT_TOKEN}"}
    url = "/admin/v1/settings/auto-replies"
    expected = {"checks": True, "repeats": True, "repeat_after": 3, "assistant": False}
    assert client.get(url, headers=bot).json() == expected
    r = client.patch(url, json={"repeat_after": 1}, headers=bot)
    assert r.json() == {"checks": True, "repeats": True, "repeat_after": 1, "assistant": False}
    first, _ = _ask(client)
    _answer(client, first["id"])
    _, view = _ask(client)
    assert view["messages"][1]["automatic"] is True

    assert client.patch(url, json={"repeats": False, "checks": False}, headers=bot).json()["repeats"] is False
    _, view = _ask(client)
    assert len(view["messages"]) == 1
    _, view = _ask(client, "ping")
    assert len(view["messages"]) == 1  # connectivity checks off too
    assert client.patch(url, json={"repeat_after": 0}, headers=bot).status_code == 422
    assert client.patch(url, json={"repeat_after": 1}).status_code == 401


def test_settings_survive_a_restart_and_win_over_the_environment(tmp_path):
    path = tmp_path / "s.db"
    Store(path).set_auto_reply_settings(repeats=False, repeat_after=7)
    store = Store(path, auto_reply_defaults=AutoReplySettings(checks=False, repeats=True, repeat_after=3))
    assert store.auto_reply_settings() == AutoReplySettings(checks=False, repeats=False, repeat_after=7)


def test_settings_changes_must_name_known_fields(client):
    url = "/admin/v1/settings/auto-replies"
    assert client.patch(url, json={}, headers=ADMIN).status_code == 422
    assert client.patch(url, json={"repeat_afer": 2}, headers=ADMIN).status_code == 422
