import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import agent_helper.store as store_module
from agent_helper import keys
from agent_helper.board import verify_chain

INSTANCE = ""  # set per test from the store; see the instance fixture


class Agent:
    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public = base64.b64encode(raw).decode()
        self.key_id = keys.key_id(self.public)
        self.token: str | None = None

    def sign(self, statement: bytes) -> str:
        return base64.b64encode(self.private.sign(statement)).decode()

    def proof(self, handle: str, instance: str | None = None) -> str:
        return self.sign(keys.key_statement(instance or INSTANCE, handle, self.public))

    def note(self, author: str, content: str, topic: str | None = None, tags: list[str] | None = None) -> str:
        return self.sign(keys.board_statement(INSTANCE, author, topic, content, tags or []))

    def recover(self, handle: str, challenge: str) -> str:
        return self.sign(keys.recovery_statement(INSTANCE, handle, challenge))


def _register(client, handle, agent, token=None, proof=None):
    body = {"public_key": agent.public, "proof": proof or agent.proof(handle)}
    if token:
        body["handle_token"] = token
    return client.post(f"/v1/handles/{handle}/keys", json=body)


@pytest.fixture(autouse=True)
def instance(client):
    global INSTANCE
    INSTANCE = client.app.state.store.instance
    assert INSTANCE.startswith("ah-")
    return INSTANCE


@pytest.fixture
def nova(client):
    agent = Agent()
    r = _register(client, "nova", agent)
    assert r.status_code == 200, r.text
    agent.token = r.json()["handle_token"]
    return agent


def _challenge(client, handle="nova"):
    r = client.post(f"/v1/handles/{handle}/recovery-challenges")
    assert r.status_code == 200, r.text
    return r.json()["challenge"]


def _recover(client, handle, challenge, signature):
    return client.post(f"/v1/handles/{handle}/recover", json={"challenge": challenge, "signature": signature})


# --- registration --------------------------------------------------------------------------------------


def test_register_key_on_a_new_handle(client, nova):
    listed = client.get("/v1/handles/nova/keys").json()["keys"]
    assert [(k["key_id"], k["status"]) for k in listed] == [(nova.key_id, "active")]
    assert listed[0]["public_key"] == nova.public


def test_registration_needs_proof_of_possession(client):
    victim, attacker = Agent(), Agent()
    # Someone else's public key with the attacker's own "proof" is refused.
    r = _register(client, "lyra", victim, proof=attacker.proof("lyra"))
    assert r.status_code == 422 and "proof" in r.json()["detail"]
    # A proof for another handle or another instance does not count either.
    assert _register(client, "lyra", victim, proof=victim.proof("other")).status_code == 422
    assert _register(client, "lyra", victim, proof=victim.proof("lyra", "https://other.example")).status_code == 422
    # A refused registration does not register the handle.
    assert _register(client, "lyra", victim).status_code == 200


def test_squatter_cannot_add_a_key_to_an_existing_handle(client, nova):
    attacker = Agent()
    assert _register(client, "nova", attacker).status_code == 409
    assert _register(client, "n0va", attacker, "wrong-token").status_code == 409
    assert [k["key_id"] for k in client.get("/v1/handles/nova/keys").json()["keys"]] == [nova.key_id]


def test_rotation_keeps_old_signatures_valid_and_revocation_flags_them(client, nova):
    body = {"content": "first note", "author": "nova", "handle_token": nova.token, "key_id": nova.key_id}
    first = client.post("/v1/board", json={**body, "signature": nova.note("nova", "first note")}).json()
    assert first["v"] == 3 and first["signature_status"] == "valid"

    new = Agent()
    rotated = _register(client, "nova", new, nova.token).json()["keys"]
    assert {k["key_id"]: k["status"] for k in rotated} == {nova.key_id: "retired", new.key_id: "active"}
    assert client.get(f"/v1/board/{first['seq']}").json()["signature_status"] == "valid"

    second = {**body, "content": "second", "signature": nova.note("nova", "second")}
    assert client.post("/v1/board", json=second).status_code == 422  # retired keys cannot sign new things

    r = client.post(f"/v1/handles/nova/keys/{nova.key_id}/revoke", json={"handle_token": nova.token})
    assert r.status_code == 200
    assert client.get(f"/v1/board/{first['seq']}").json()["signature_status"] == "key_revoked"
    assert _register(client, "nova", nova, nova.token).status_code == 409  # a used key cannot come back
    assert client.post(f"/v1/handles/nova/keys/{nova.key_id}/revoke", json={"handle_token": "x"}).status_code == 404


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
    r = client.post("/v1/handles/weak/keys", json={"public_key": key, "proof": "A" * 88})
    assert r.status_code == 422


def test_small_order_detection():
    assert keys.is_weak_public_key(bytes.fromhex("01" + "00" * 31))
    for _ in range(20):
        assert not keys.is_weak_public_key(base64.b64decode(Agent().public))


def test_invalid_public_keys(client):
    for bad in ("not base64!!" * 4, base64.b64encode(b"short").decode().ljust(44, "A")):
        assert client.post("/v1/handles/lyra/keys", json={"public_key": bad, "proof": "A" * 88}).status_code == 422


# --- signatures ----------------------------------------------------------------------------------------


def test_signed_note_is_part_of_the_chain(client, nova):
    tags = ["tips"]
    body = {
        "content": "signed text",
        "topic": "Topic",
        "tags": tags,
        "author": "nova",
        "handle_token": nova.token,
        "key_id": nova.key_id,
        "signature": nova.note("nova", "signed text", "Topic", tags),
    }
    entry = client.post("/v1/board", json=body).json()
    client.post("/v1/board", json={"content": "plain"})
    entries = client.get("/v1/board").json()
    assert verify_chain(entries).ok
    statement = keys.board_statement(INSTANCE, "nova", "Topic", "signed text", tags)
    assert keys.verify(nova.public, entry["signature"], statement)
    forged = [dict(e) for e in entries]
    forged[0]["signature"] = Agent().sign(statement)
    assert not verify_chain(forged).ok


def test_bad_signatures_are_refused(client, nova):
    base = {"content": "x", "author": "nova", "handle_token": nova.token, "key_id": nova.key_id}
    assert client.post("/v1/board", json={**base, "signature": nova.note("nova", "something else")}).status_code == 422
    assert client.post("/v1/board", json={**base, "signature": None}).status_code == 422
    other = Agent()
    r = client.post("/v1/board", json={**base, "key_id": other.key_id, "signature": other.note("nova", "x")})
    assert r.status_code == 422
    # Signing is optional.
    assert (
        client.post("/v1/board", json={"content": "x", "author": "nova", "handle_token": nova.token}).status_code == 201
    )


def test_signatures_from_another_instance_are_refused(client, nova):
    foreign = nova.sign(keys.board_statement("https://other.example", "nova", None, "x", []))
    body = {"content": "x", "author": "nova", "handle_token": nova.token, "key_id": nova.key_id, "signature": foreign}
    assert client.post("/v1/board", json=body).status_code == 422
    assert client.put("/v1/directory/orion", json={"summary": "crawler"}).status_code == 200
    msg = nova.sign(keys.message_statement("https://other.example", "nova", "orion", "message", None, "hi"))
    body = {"sender": "nova", "to": "orion", "message": "hi", "handle_token": nova.token, "key_id": nova.key_id}
    assert client.post("/v1/messages", json={**body, "signature": msg}).status_code == 422
    challenge = _challenge(client)
    foreign_recovery = nova.sign(keys.recovery_statement("https://other.example", "nova", challenge))
    assert _recover(client, "nova", challenge, foreign_recovery).status_code == 403


def test_signed_message(client, nova):
    orion = client.put("/v1/directory/orion", json={"summary": "crawler"}).json()["handle_token"]
    signature = nova.sign(keys.message_statement(INSTANCE, "nova", "orion", "message", None, "hello"))
    body = {
        "sender": "nova",
        "to": "orion",
        "message": "hello",
        "handle_token": nova.token,
        "key_id": nova.key_id,
        "signature": signature,
    }
    r = client.post("/v1/messages", json=body)
    assert r.status_code == 201 and r.json()["signature_status"] == "valid"
    inbox = client.get("/v1/mailbox/orion", headers={"Authorization": f"Bearer {orion}"}).json()["messages"]
    assert inbox[0]["signature_status"] == "valid" and inbox[0]["key_id"] == nova.key_id
    assert client.post("/v1/messages", json={**body, "message": "changed"}).status_code == 422


def test_signatures_are_stored_canonically(client, nova):
    stmt = keys.board_statement(INSTANCE, "nova", None, "url-safe", [])
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


# --- recovery ------------------------------------------------------------------------------------------


def test_recovery_with_the_key(client, nova):
    challenge = _challenge(client)
    r = _recover(client, "nova", challenge, nova.recover("nova", challenge))
    assert r.status_code == 200
    new_token = r.json()["handle_token"]
    old = {"content": "x", "author": "nova", "handle_token": nova.token}
    assert client.post("/v1/board", json=old).status_code == 409
    assert client.post("/v1/board", json={**old, "handle_token": new_token}).status_code == 201
    # One successful recovery per challenge.
    assert _recover(client, "nova", challenge, nova.recover("nova", challenge)).status_code == 404


def test_attackers_asking_for_challenges_cannot_block_the_owner(client, nova):
    """The attack from review: the owner gets a challenge, then others ask for many while the owner signs."""
    owner_challenge = _challenge(client)
    for _ in range(3):
        _challenge(client)  # the attacker's challenges
    r = _recover(client, "nova", owner_challenge, nova.recover("nova", owner_challenge))
    assert r.status_code == 200


def test_failed_attempts_do_not_spend_the_challenge(client, nova):
    challenge = _challenge(client)
    wrong = Agent().recover("nova", challenge)
    assert _recover(client, "nova", challenge, wrong).status_code == 403
    assert _recover(client, "nova", challenge, nova.recover("nova", challenge)).status_code == 200


def test_challenges_are_server_signed_and_expire(client, nova, monkeypatch):
    challenge = _challenge(client)
    parts = challenge.split(".")
    forged = ".".join([*parts[:3], str(int(parts[3]) + 3600), parts[4]])  # extend the expiry
    assert _recover(client, "nova", forged, nova.recover("nova", forged)).status_code == 404
    assert _recover(client, "nova", "garbage", nova.recover("nova", "garbage")).status_code == 404
    real = store_module.time.time
    monkeypatch.setattr(store_module.time, "time", lambda: real() + store_module.CHALLENGE_SECONDS + 1)
    assert _recover(client, "nova", challenge, nova.recover("nova", challenge)).status_code == 410


def test_challenge_for_one_handle_does_not_recover_another(client, nova):
    orion = Agent()
    _register(client, "orion", orion)
    challenge = _challenge(client, "nova")
    assert _recover(client, "orion", challenge, orion.recover("orion", challenge)).status_code == 404


def test_recovery_needs_an_active_key(client):
    client.post("/v1/board", json={"content": "x", "author": "lyra"})
    assert client.post("/v1/handles/lyra/recovery-challenges").status_code == 404
    assert client.post("/v1/handles/nobody/recovery-challenges").status_code == 404


def test_registered_handle_spelling_is_returned_and_used(client):
    agent = Agent()
    assert _register(client, "Nova", agent).status_code == 200
    assert client.get("/v1/handles/nova/keys").json()["handle"] == "Nova"
    r = client.post("/v1/handles/nova/recovery-challenges").json()
    assert r["statement"] == {
        "purpose": "agent-helper/recover",
        "instance": INSTANCE,
        "handle": "Nova",
        "challenge": r["challenge"],
    }
    assert _recover(client, "nova", r["challenge"], agent.recover("Nova", r["challenge"])).status_code == 200


# --- MCP -----------------------------------------------------------------------------------------------


def test_mcp_key_tools(client):
    from test_mcp import call

    agent = Agent()
    reg = call(client, "register_key", {"handle": "vega", "public_key": agent.public, "proof": agent.proof("vega")})
    assert reg["structuredContent"]["keys"][0]["status"] == "active"
    step1 = call(client, "recover_handle", {"handle": "vega"})["structuredContent"]
    assert step1["statement"]["instance"] == INSTANCE
    sig = agent.recover("vega", step1["challenge"])
    step2 = call(client, "recover_handle", {"handle": "vega", "challenge": step1["challenge"], "signature": sig})
    assert step2["isError"] is False and step2["structuredContent"]["handle_token"]
    token = step2["structuredContent"]["handle_token"]
    note = {
        "content": "signed via mcp",
        "author": "vega",
        "handle_token": token,
        "key_id": agent.key_id,
        "signature": agent.note("vega", "signed via mcp"),
    }
    assert call(client, "post_board", note)["structuredContent"]["signature_status"] == "valid"
    again = {"handle": "vega", "public_key": agent.public, "proof": agent.proof("vega")}
    assert call(client, "register_key", again)["isError"] is True


def test_signatures_survive_a_change_of_public_url(make_client):
    first = make_client(public_base_url="https://agents.example.invalid")
    global INSTANCE
    INSTANCE = first.app.state.store.instance
    agent = Agent()
    token = _register(first, "nova", agent).json()["handle_token"]
    body = {
        "content": "x",
        "author": "nova",
        "handle_token": token,
        "key_id": agent.key_id,
        "signature": agent.note("nova", "x"),
    }
    seq = first.post("/v1/board", json=body).json()["seq"]
    first.__exit__(None, None, None)

    moved = make_client(public_base_url="https://NEW-NAME.example.invalid/")
    assert moved.app.state.store.instance == INSTANCE
    assert moved.get(f"/v1/board/{seq}").json()["signature_status"] == "valid"
    assert moved.get("/.well-known/agent-helper.json").json()["instance_id"] == INSTANCE
    assert INSTANCE in moved.get("/llms.txt").text


def test_a_challenge_survives_a_restart(make_client):
    first = make_client()
    global INSTANCE
    INSTANCE = first.app.state.store.instance
    agent = Agent()
    _register(first, "nova", agent)
    challenge = first.post("/v1/handles/nova/recovery-challenges").json()["challenge"]
    first.__exit__(None, None, None)

    restarted = make_client()
    assert _recover(restarted, "nova", challenge, agent.recover("nova", challenge)).status_code == 200


def test_mcp_instructions_name_the_instance(client):
    from test_mcp import modern

    instructions = modern(client, "server/discover").json()["result"]["instructions"]
    assert client.app.state.store.instance in instructions
