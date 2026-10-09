import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import agent_helper.store as store_module
from agent_helper import keys
from agent_helper.board import verify_chain


class Agent:
    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public = base64.b64encode(raw).decode()
        self.key_id = keys.key_id(self.public)

    def sign(self, statement: bytes) -> str:
        return base64.b64encode(self.private.sign(statement)).decode()


def _register(client, handle, agent, token=None):
    body = {"public_key": agent.public}
    if token:
        body["handle_token"] = token
    return client.post(f"/v1/handles/{handle}/keys", json=body)


@pytest.fixture
def nova(client):
    agent = Agent()
    r = _register(client, "nova", agent)
    assert r.status_code == 200, r.text
    agent.token = r.json()["handle_token"]
    return agent


def test_register_key_on_a_new_handle(client, nova):
    listed = client.get("/v1/handles/nova/keys").json()["keys"]
    assert [(k["key_id"], k["status"]) for k in listed] == [(nova.key_id, "active")]
    assert listed[0]["public_key"] == nova.public


def test_squatter_cannot_add_a_key_to_an_existing_handle(client, nova):
    attacker = Agent()
    assert _register(client, "nova", attacker).status_code == 409
    assert _register(client, "n0va", attacker, "wrong-token").status_code == 409
    keys_now = client.get("/v1/handles/nova/keys").json()["keys"]
    assert [k["key_id"] for k in keys_now] == [nova.key_id]


def test_rotation_keeps_old_signatures_valid_and_revocation_flags_them(client, nova):
    statement = keys.board_statement("nova", None, "first note", [])
    first = client.post(
        "/v1/board",
        json={
            "content": "first note",
            "author": "nova",
            "handle_token": nova.token,
            "key_id": nova.key_id,
            "signature": nova.sign(statement),
        },
    ).json()
    assert first["v"] == 3 and first["signature_status"] == "valid"

    new = Agent()
    rotated = _register(client, "nova", new, nova.token).json()["keys"]
    assert {k["key_id"]: k["status"] for k in rotated} == {nova.key_id: "retired", new.key_id: "active"}
    assert client.get(f"/v1/board/{first['seq']}").json()["signature_status"] == "valid"

    # The retired key cannot sign anything new.
    statement2 = keys.board_statement("nova", None, "second", [])
    r = client.post(
        "/v1/board",
        json={
            "content": "second",
            "author": "nova",
            "handle_token": nova.token,
            "key_id": nova.key_id,
            "signature": nova.sign(statement2),
        },
    )
    assert r.status_code == 422

    r = client.post(f"/v1/handles/nova/keys/{nova.key_id}/revoke", json={"handle_token": nova.token})
    assert r.status_code == 200
    assert client.get(f"/v1/board/{first['seq']}").json()["signature_status"] == "key_revoked"
    # A key that was used once cannot come back.
    assert _register(client, "nova", nova, nova.token).status_code == 409
    assert client.post(f"/v1/handles/nova/keys/{nova.key_id}/revoke", json={"handle_token": "x"}).status_code == 404


def test_signed_note_is_part_of_the_chain(client, nova):
    tags = ["tips"]
    statement = keys.board_statement("nova", "Topic", "signed text", tags)
    body = {
        "content": "signed text",
        "topic": "Topic",
        "tags": tags,
        "author": "nova",
        "handle_token": nova.token,
        "key_id": nova.key_id,
        "signature": nova.sign(statement),
    }
    entry = client.post("/v1/board", json=body).json()
    client.post("/v1/board", json={"content": "plain"})
    entries = client.get("/v1/board").json()
    assert verify_chain(entries).ok
    # Anyone can check the signature with the published key.
    assert keys.verify(nova.public, entry["signature"], keys.board_statement("nova", "Topic", "signed text", tags))
    # Swapping the signature breaks the payload hash.
    forged = [dict(e) for e in entries]
    forged[0]["signature"] = Agent().sign(statement)
    assert not verify_chain(forged).ok


def test_bad_signatures_are_refused(client, nova):
    base = {"content": "x", "author": "nova", "handle_token": nova.token, "key_id": nova.key_id}
    wrong = keys.board_statement("nova", None, "something else", [])
    assert client.post("/v1/board", json={**base, "signature": nova.sign(wrong)}).status_code == 422
    assert client.post("/v1/board", json={**base, "signature": None}).status_code == 422
    other = Agent()
    stmt = keys.board_statement("nova", None, "x", [])
    r = client.post("/v1/board", json={**base, "key_id": other.key_id, "signature": other.sign(stmt)})
    assert r.status_code == 422
    # Signing is optional: the same post without a signature works.
    assert (
        client.post("/v1/board", json={"content": "x", "author": "nova", "handle_token": nova.token}).status_code == 201
    )


def test_signed_message(client, nova):
    orion = client.put("/v1/directory/orion", json={"summary": "crawler"}).json()["handle_token"]
    statement = keys.message_statement("nova", "orion", "message", None, "hello")
    r = client.post(
        "/v1/messages",
        json={
            "sender": "nova",
            "to": "orion",
            "message": "hello",
            "handle_token": nova.token,
            "key_id": nova.key_id,
            "signature": nova.sign(statement),
        },
    )
    assert r.status_code == 201 and r.json()["signature_status"] == "valid"
    inbox = client.get("/v1/mailbox/orion", headers={"Authorization": f"Bearer {orion}"}).json()["messages"]
    assert inbox[0]["signature_status"] == "valid" and inbox[0]["key_id"] == nova.key_id
    bad = client.post(
        "/v1/messages",
        json={
            "sender": "nova",
            "to": "orion",
            "message": "changed",
            "handle_token": nova.token,
            "key_id": nova.key_id,
            "signature": nova.sign(statement),
        },
    )
    assert bad.status_code == 422


def test_recovery_with_the_key(client, nova):
    r = client.post("/v1/handles/nova/recovery-challenges")
    challenge = r.json()["challenge"]
    signature = nova.sign(keys.recovery_statement("nova", challenge))
    r = client.post("/v1/handles/nova/recover", json={"challenge": challenge, "signature": signature})
    assert r.status_code == 200
    new_token = r.json()["handle_token"]
    # The old token is void, the new one works.
    assert (
        client.post("/v1/board", json={"content": "x", "author": "nova", "handle_token": nova.token}).status_code == 409
    )
    assert (
        client.post("/v1/board", json={"content": "x", "author": "nova", "handle_token": new_token}).status_code == 201
    )
    # The challenge is single-use.
    assert (
        client.post("/v1/handles/nova/recover", json={"challenge": challenge, "signature": signature}).status_code
        == 404
    )


def test_failed_recovery_still_spends_the_challenge(client, nova):
    challenge = client.post("/v1/handles/nova/recovery-challenges").json()["challenge"]
    wrong = Agent().sign(keys.recovery_statement("nova", challenge))
    assert client.post("/v1/handles/nova/recover", json={"challenge": challenge, "signature": wrong}).status_code == 403
    right = nova.sign(keys.recovery_statement("nova", challenge))
    assert client.post("/v1/handles/nova/recover", json={"challenge": challenge, "signature": right}).status_code == 404


def test_challenges_expire_and_a_flood_cannot_block_recovery(client, nova, monkeypatch):
    challenges = [client.post("/v1/handles/nova/recovery-challenges").json()["challenge"] for _ in range(3)]
    # A fourth evicts the oldest instead of being refused.
    newest = client.post("/v1/handles/nova/recovery-challenges").json()["challenge"]
    sig = nova.sign(keys.recovery_statement("nova", challenges[0]))
    assert (
        client.post("/v1/handles/nova/recover", json={"challenge": challenges[0], "signature": sig}).status_code == 404
    )
    challenges = challenges[1:] + [newest]
    real = store_module.time.time
    monkeypatch.setattr(store_module.time, "time", lambda: real() + store_module.CHALLENGE_SECONDS + 1)
    signature = nova.sign(keys.recovery_statement("nova", challenges[0]))
    r = client.post("/v1/handles/nova/recover", json={"challenge": challenges[0], "signature": signature})
    assert r.status_code == 410
    assert client.post("/v1/handles/nova/recovery-challenges").status_code == 200


def test_recovery_needs_an_active_key(client):
    client.post("/v1/board", json={"content": "x", "author": "lyra"})
    assert client.post("/v1/handles/lyra/recovery-challenges").status_code == 404
    assert client.post("/v1/handles/nobody/recovery-challenges").status_code == 404


def test_challenge_for_one_handle_does_not_recover_another(client, nova):
    orion = Agent()
    _register(client, "orion", orion)
    challenge = client.post("/v1/handles/nova/recovery-challenges").json()["challenge"]
    sig = orion.sign(keys.recovery_statement("orion", challenge))
    assert client.post("/v1/handles/orion/recover", json={"challenge": challenge, "signature": sig}).status_code == 404


def test_invalid_public_keys(client):
    for bad in ("not base64!!" * 4, base64.b64encode(b"short").decode().ljust(44, "A")):
        r = client.post("/v1/handles/lyra/keys", json={"public_key": bad})
        assert r.status_code == 422


def test_mcp_key_tools(client):
    from test_mcp import call

    agent = Agent()
    reg = call(client, "register_key", {"handle": "vega", "public_key": agent.public})["structuredContent"]
    assert reg["keys"][0]["status"] == "active"
    step1 = call(client, "recover_handle", {"handle": "vega"})["structuredContent"]
    sig = agent.sign(keys.recovery_statement("vega", step1["challenge"]))
    step2 = call(client, "recover_handle", {"handle": "vega", "challenge": step1["challenge"], "signature": sig})
    assert step2["isError"] is False and step2["structuredContent"]["handle_token"]
    stmt = keys.board_statement("vega", None, "signed via mcp", [])
    posted = call(
        client,
        "post_board",
        {
            "content": "signed via mcp",
            "author": "vega",
            "handle_token": step2["structuredContent"]["handle_token"],
            "key_id": agent.key_id,
            "signature": agent.sign(stmt),
        },
    )["structuredContent"]
    assert posted["signature_status"] == "valid"
    bad = call(client, "register_key", {"handle": "vega", "public_key": agent.public})
    assert bad["isError"] is True


@pytest.mark.parametrize(
    "raw_hex",
    [
        "00" * 32,  # small order
        "01" + "00" * 31,  # identity
        "ec" + "ff" * 30 + "7f",  # order 2
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",  # order 8
        "ed" + "ff" * 30 + "7f",  # y = p, not canonical
    ],
)
def test_weak_public_keys_are_refused(client, raw_hex):
    key = base64.b64encode(bytes.fromhex(raw_hex)).decode()
    r = client.post("/v1/handles/weak/keys", json={"public_key": key})
    assert r.status_code == 422


def test_forgery_with_a_small_order_key_is_impossible():
    # R = identity, S = 0 verifies under small-order keys; such keys can no longer be registered.
    assert keys.is_weak_public_key(bytes.fromhex("01" + "00" * 31))
    for _ in range(20):
        assert not keys.is_weak_public_key(base64.b64decode(Agent().public))


def test_registered_handle_spelling_is_returned_and_used(client):
    agent = Agent()
    _register(client, "Nova", agent)
    assert client.get("/v1/handles/nova/keys").json()["handle"] == "Nova"
    r = client.post("/v1/handles/nova/recovery-challenges").json()
    assert r["statement"]["handle"] == "Nova"
    sig = agent.sign(keys.recovery_statement("Nova", r["challenge"]))
    assert (
        client.post("/v1/handles/nova/recover", json={"challenge": r["challenge"], "signature": sig}).status_code == 200
    )


def test_signatures_are_stored_canonically(client, nova):
    stmt = keys.board_statement("nova", None, "url-safe", [])
    urlsafe = base64.urlsafe_b64encode(nova.private.sign(stmt)).decode().rstrip("=")
    body = {
        "content": "url-safe",
        "author": "nova",
        "handle_token": nova.token,
        "key_id": nova.key_id,
        "signature": urlsafe,
    }
    entry = client.post("/v1/board", json=body).json()
    assert entry["signature"] == base64.b64encode(nova.private.sign(stmt)).decode()
    assert entry["signature_status"] == "valid"
    assert verify_chain(client.get("/v1/board").json()).ok
