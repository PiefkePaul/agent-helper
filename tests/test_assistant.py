"""Replies a language model wrote are labelled, switchable and never repeated (docs/decisions/0032)."""

import logging

import pytest
from conftest import ADMIN_SECRET
from test_a2a import rpc, start
from test_mcp import call

from agent_helper.store import ASSISTANT_LABELS

ADMIN = {"Authorization": f"Bearer {ADMIN_SECRET}"}
BOT = "bot-token-" + "a" * 40
QUESTION = "How do I read the replies to my request?"
ANSWER = "Send GET /v1/requests/<id> with your follow_up_token as a bearer header."


def _ask(client, message=QUESTION):
    return client.post("/v1/requests", json={"message": message}).json()


def _view(client, created):
    return client.get(
        f"/v1/requests/{created['id']}", headers={"Authorization": f"Bearer {created['follow_up_token']}"}
    ).json()


def _reply(client, req_id, author, headers=ADMIN, message=ANSWER):
    body = {"message": message, "status": "answered"} | ({"author": author} if author else {})
    return client.post(f"/admin/v1/requests/{req_id}/replies", json=body, headers=headers)


@pytest.fixture
def client(make_client):
    return make_client(auto_reply_assistant=True, operator_bot_token=BOT, auto_reply_repeat_after=1)


def test_unchecked_answer_is_labelled_counted_and_logged(client, caplog):
    created = _ask(client)
    with caplog.at_level(logging.INFO, logger="agent_helper.audit"):
        r = _reply(client, created["id"], "assistant", headers={"Authorization": f"Bearer {BOT}"})
    assert r.status_code == 200, r.text
    message = _view(client, created)["messages"][1]
    assert message["author"] == "assistant" and message["automatic"] is True
    assert message["body"] == f"{ASSISTANT_LABELS['assistant']}\n\n{ANSWER}"
    assert "by assistant) via telegram" in caplog.text
    client.app.state.flush_usage()
    rows = {r["metric"]: r["count"] for r in client.app.state.store.usage(1)}
    assert rows["autoreply:assistant"] == 1


def test_reviewed_draft_is_labelled_but_not_automatic(client):
    created = _ask(client)
    assert _reply(client, created["id"], "assistant_reviewed").status_code == 200
    message = _view(client, created)["messages"][1]
    assert message["author"] == "assistant_reviewed" and message["automatic"] is False
    assert message["body"].startswith(ASSISTANT_LABELS["assistant_reviewed"])


def test_the_operators_own_reply_has_no_author(client):
    created = _ask(client)
    assert _reply(client, created["id"], None).status_code == 200
    message = _view(client, created)["messages"][1]
    assert message["author"] is None and message["body"] == ANSWER


def test_unknown_author_is_refused(client):
    assert _reply(client, _ask(client)["id"], "operator").status_code == 422


def test_off_by_default_and_switchable_at_run_time(make_client):
    client = make_client(operator_bot_token=BOT)
    created = _ask(client)
    assert _reply(client, created["id"], "assistant").status_code == 409
    assert _reply(client, created["id"], "assistant_reviewed").status_code == 200  # the operator approved it
    bot = {"Authorization": f"Bearer {BOT}"}
    assert client.get("/admin/v1/settings/auto-replies", headers=bot).json()["assistant"] is False
    r = client.patch("/admin/v1/settings/auto-replies", json={"assistant": True}, headers=bot)
    assert r.status_code == 200 and r.json()["assistant"] is True
    assert _reply(client, created["id"], "assistant").status_code == 200
    client.patch("/admin/v1/settings/auto-replies", json={"assistant": False}, headers=bot)
    assert _reply(client, created["id"], "assistant").status_code == 409
    assert len(_view(client, created)["messages"]) == 3  # the refused reply left nothing behind


@pytest.mark.parametrize("author", ["assistant", "assistant_reviewed"])
def test_language_model_answers_are_never_repeated(client, author):
    first = _ask(client)
    _reply(client, first["id"], author)
    second = _view(client, _ask(client))
    assert second["status"] == "open" and len(second["messages"]) == 1


def test_label_reaches_mcp_a2a_and_console(client):
    created = call(client, "describe_need", {"message": "Something only a model would answer."})["structuredContent"]
    _reply(client, created["id"], "assistant")
    read = call(client, "read_request", {"id": created["id"], "follow_up_token": created["follow_up_token"]})
    assert read["structuredContent"]["messages"][1]["author"] == "assistant"

    task = start(client)
    _reply(client, task["id"], "assistant")
    seen = rpc(client, "GetTask", {"id": task["id"]}, token=task["metadata"]["followUpToken"]).json()["result"]
    assert seen["history"][1]["metadata"]["author"] == "assistant"
    assert "author" not in seen["history"][0]["metadata"]


def test_agents_are_told_when_a_model_reads_requests(make_client):
    client = make_client(assistant_provider="Example Labs")
    text = client.get("/llms.txt").text
    assert "language model hosted by Example Labs" in text
    notes = client.get("/.well-known/agent-helper.json").json()["trust_notes"]
    assert any("Example Labs" in n for n in notes)
    plain = make_client()
    assert "language model" not in plain.get("/llms.txt").text
