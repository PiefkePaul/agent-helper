import pytest

from agent_helper import handles


def _board(client, author, token=None):
    body = {"content": "hello", "author": author}
    if token:
        body["handle_token"] = token
    return client.post("/v1/board", json=body)


def test_first_use_registers_handle_and_returns_token_once(client):
    r = _board(client, "Nova")
    assert r.status_code == 201
    token = r.json()["handle_token"]
    assert r.headers["cache-control"] == "no-store"

    again = _board(client, "Nova", token)
    assert again.status_code == 201 and "handle_token" not in again.json()
    assert client.get("/v1/board").json()[1]["author"] == "Nova"


def test_registered_handle_cannot_be_used_by_others(client):
    token = _board(client, "Nova").json()["handle_token"]
    assert _board(client, "Nova").status_code == 409
    assert _board(client, "Nova", "wrong-token").status_code == 409
    for lookalike in ("nova", "NOVA", "N0va", "no-va", "n.o.v.a"):
        assert _board(client, lookalike).status_code == 409, lookalike
    # the owner may use any spelling with the same skeleton
    assert _board(client, "n0va", token).status_code == 201


def test_handle_registry_is_shared_by_board_and_requests(client):
    created = client.post("/v1/requests", json={"message": "hi", "handle": "Orion"})
    assert created.status_code == 201
    token = created.json()["handle_token"]
    assert _board(client, "orion").status_code == 409
    assert _board(client, "Orion", token).status_code == 201
    assert client.post("/v1/requests", json={"message": "hi", "handle": "0rion"}).status_code == 409


@pytest.mark.parametrize(
    "name", ["operator", "Operator", "0perator", "the-operator", "admin", "Agent Helper", "agent_he1per", "system"]
)
def test_reserved_handles_are_refused(client, name):
    assert _board(client, name).status_code == 409
    assert client.post("/v1/requests", json={"message": "hi", "handle": name}).status_code == 409


@pytest.mark.parametrize("name", ["оperator", "Nоva", "a​b", "-x", "x-", "a" * 65, "name\nnext"])
def test_handles_outside_the_allowed_alphabet_are_rejected(client, name):
    # the first two contain Cyrillic letters that look like Latin ones
    assert _board(client, name).status_code == 422


def test_operator_posts_through_admin_api(client, admin_headers):
    r = client.post("/admin/v1/board", json={"content": "Official note", "topic": "news"}, headers=admin_headers)
    assert r.status_code == 201 and r.json()["author"] == handles.OPERATOR_HANDLE
    assert client.post("/admin/v1/board", json={"content": "x"}).status_code == 401


def test_posting_without_handle_needs_no_token(client):
    r = client.post("/v1/board", json={"content": "anonymous"})
    assert r.status_code == 201 and "handle_token" not in r.json()
