"""Calls that used to be refused or unanswered and now get a working answer (docs/decisions/0032)."""

import pytest
from test_a2a import message03, rpc, start, user_message
from test_mcp import MODERN, legacy, modern
from test_usage import counts

from agent_helper import discovery


def with_meta(client, method, version, *, headers=None, params=None):
    """A request that names a version in _meta, as modern clients do, with the headers given."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {**(params or {}), "_meta": {"io.modelcontextprotocol/protocolVersion": version}},
    }
    return client.post("/mcp", json=body, headers=headers or {})


# --- MCP: versions --------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26"])
def test_a_supported_legacy_version_in_meta_is_served(client, version):
    # The refusal used to list this version as supported and still refuse it.
    r = with_meta(client, "server/discover", version, headers={"MCP-Protocol-Version": version})
    assert r.status_code == 200, r.text
    assert version in r.json()["result"]["supportedVersions"]
    tools = with_meta(client, "tools/list", version)  # without any header, as legacy clients may send it
    assert tools.status_code == 200 and tools.json()["result"]["tools"]
    assert with_meta(client, "ping", version).json()["result"] == {"resultType": "complete"}


def test_a_legacy_version_in_meta_must_still_match_its_header(client):
    r = with_meta(client, "tools/list", "2025-11-25", headers={"MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == -32020 and error["data"]["expected"] == {"MCP-Protocol-Version": "2025-11-25"}


def test_unknown_versions_are_still_refused_and_counted_by_kind(client, admin_headers):
    for version in ("2099-01-01", "1999-01-01", "DRAFT-x"):
        r = with_meta(client, "server/discover", version, headers={"MCP-Protocol-Version": version})
        assert r.status_code == 400 and r.json()["error"]["code"] == -32022
        assert r.json()["error"]["data"]["requested"] == version
    c = counts(client, admin_headers)
    assert c[("mcp:version-refused:newer", "")] == 1
    assert c[("mcp:version-refused:older", "")] == 1
    assert c[("mcp:version-refused:other", "")] == 1
    assert not any("DRAFT" in m for m, _ in c)


def test_a_header_mismatch_says_which_headers_are_expected(client):
    r = modern(client, "tools/call", {"name": "list_capabilities"}, headers={"Mcp-Method": "tools/list"})
    assert r.status_code == 400
    assert r.json()["error"]["data"]["expected"] == {
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "list_capabilities",
    }


def test_a_notification_sent_with_an_id_is_acknowledged(client):
    r = legacy(client, "notifications/initialized")
    assert r.status_code == 200 and r.json()["result"] == {"resultType": "complete"}
    # Modern requests keep the strict rule: notifications/... is not a method there.
    assert modern(client, "notifications/initialized").status_code == 404


# --- A2A: dialects and task lookups --------------------------------------------------------------


@pytest.mark.parametrize("version", ["1.0", "1.0.0", "1.0.2"])
def test_a2a_1_0_with_a_patch_number(client, version):
    r = rpc(client, "SendMessage", user_message("hello"), headers={"A2A-Version": version})
    assert r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"


@pytest.mark.parametrize("version", ["0.2", "0.2.6", "0.3.1"])
def test_a2a_0_2_and_0_3_patch_versions_use_the_0_3_dialect(client, version):
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": message03("hello")}
    r = client.post("/a2a", json=body, headers={"A2A-Version": version})
    assert r.json()["result"]["kind"] == "task"


def test_unknown_methods_without_a_version_are_unknown_methods(client):
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}})
    assert r.json()["error"]["code"] == -32601


def test_the_follow_up_token_may_come_in_the_body(client):
    task = start(client)
    token = task["metadata"]["followUpToken"]
    seen = rpc(client, "GetTask", {"id": task["id"], "metadata": {"followUpToken": token}}).json()
    assert seen["result"]["id"] == task["id"]
    follow_up = user_message("One more detail.", taskId=task["id"], metadata={"followUpToken": token})
    added = rpc(client, "SendMessage", follow_up).json()["result"]["task"]
    assert added["history"][-1]["parts"][0]["text"] == "One more detail."
    wrong = rpc(client, "GetTask", {"id": task["id"], "metadata": {"followUpToken": "wrong"}}).json()
    assert wrong["error"]["code"] == -32001


def test_task_not_found_explains_what_was_missing(client, admin_headers):
    no_token = rpc(client, "GetTask", {"id": "req_abc"}).json()["error"]
    assert no_token["code"] == -32001 and "followUpToken" in no_token["message"]
    foreign = rpc(client, "GetTask", {"id": "my-own-id"}, token="some-token").json()["error"]  # noqa: S106
    assert foreign["code"] == -32001 and "start with 'req_'" in foreign["message"]
    wrong = rpc(client, "GetTask", {"id": "req_abc"}, token="some-token").json()["error"]  # noqa: S106
    assert wrong == {"code": -32001, "message": "task not found or wrong token"}
    # The hint never depends on whether the task exists: a real id with a wrong token reads the same.
    real = start(client)["id"]
    assert rpc(client, "GetTask", {"id": real}, token="some-token").json()["error"] == wrong  # noqa: S106
    c = counts(client, admin_headers)
    assert c[("a2a:task-not-found:no-token", "")] == 1
    assert c[("a2a:task-not-found:foreign-id", "")] == 1
    assert c[("a2a:task-not-found:wrong-token", "")] == 2


# --- small files and pages -----------------------------------------------------------------------


def test_directory_index_without_well_known(client):
    assert client.get("/agent-directory.json").json() == client.get("/.well-known/agent-directory.json").json()


@pytest.mark.parametrize("path", ["/.well-known/payment-manifest", "/.well-known/mpp", "/pricing"])
def test_payment_discovery_says_it_is_free(client, path):
    body = client.get(path).json()
    assert body["payment_required"] is False and body["price"] == "free"


@pytest.mark.parametrize("path", discovery.HELP_PATHS)
def test_help_pages_lead_to_the_front_page(client, path, admin_headers):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/"
    assert sum(n for (m, _), n in counts(client, admin_headers).items() if m == "compat:help-page") == 1
