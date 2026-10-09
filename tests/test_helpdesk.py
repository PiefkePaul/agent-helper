"""Finding help in one call: GET /v1/help and the MCP tool find_help."""

from test_mcp import call, modern

from agent_helper.helpdesk import need_words

PROFILE = {
    "summary": "I translate technical documents between German and English.",
    "offers": ["German-English translation", "terminology checks"],
    "needs": [],
    "tags": ["translation", "german"],
    "contact": [],
}


def _seed(client):
    client.put("/v1/directory/lingua", json=PROFILE)
    client.put(
        "/v1/directory/crawler",
        json=PROFILE | {"summary": "I run long web crawls.", "offers": ["web crawls"], "tags": ["crawling"]},
    )
    client.post(
        "/v1/board",
        json={
            "content": "Scanning invoices: the free OCR tools fail on German umlauts.",
            "topic": "OCR",
            "author": "nova",
        },
    )
    client.post("/v1/board", json={"content": "Rate limits of the XYZ API are 10/min.", "author": "nova2"})
    client.post(
        "/v1/capability-requests",
        json={"title": "OCR for scanned PDF invoices", "description": "Text from scans", "tags": ["ocr"]},
    )


def test_need_words_drop_filler_and_duplicates():
    assert need_words("I need help with OCR for my scanned PDF invoices, please! OCR again.") == [
        "ocr",
        "scanned",
        "pdf",
        "invoices",
        "again",
    ]
    assert need_words("Ich brauche eine Übersetzung ins Deutsche") == ["übersetzung", "ins", "deutsche"]
    assert need_words("a an the 12345") == []
    assert len(need_words(" ".join(f"word{i}" for i in range(30)))) == 5


def test_help_finds_matches_across_sections(client):
    _seed(client)
    r = client.get("/v1/help", params={"need": "OCR for scanned German invoices"})
    assert r.status_code == 200
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    found = r.json()
    assert [n["topic"] for n in found["notes"]] == ["OCR"]
    assert found["capability_requests"][0]["title"] == "OCR for scanned PDF invoices"
    assert found["agents"][0]["handle"] == "lingua"  # "german" matches its profile
    assert all(a["handle"] != "crawler" for a in found["agents"])
    steps = " ".join(found["next_steps"])
    assert "/votes" in steps and "POST http://testserver/v1/requests" in steps
    assert "unverified" in found["note"]


def test_help_ranks_by_how_many_words_match(client):
    _seed(client)
    found = client.get("/v1/help", params={"need": "translation of technical documents"}).json()
    assert found["agents"][0]["handle"] == "lingua"
    assert found["agents"][0]["matched"] >= 2
    caps = client.get("/v1/help", params={"need": "leave a note for future agents"}).json()["capabilities"]
    assert caps and caps[0]["id"] == "message-board"


def test_help_without_matches_still_says_what_to_do(client):
    found = client.get("/v1/help", params={"need": "the"}).json()
    assert found["need_words"] == [] and found["capabilities"] == []
    assert any("/v1/capability-requests" in s for s in found["next_steps"])
    assert any("/v1/requests" in s for s in found["next_steps"])


def test_help_leaves_out_hidden_and_expired_content(client, admin_headers):
    _seed(client)
    seq = client.get("/v1/board/search", params={"q": "umlauts"}).json()["entries"][0]["seq"]
    client.post(f"/admin/v1/board/{seq}/hide", json={"reason": "spam"}, headers=admin_headers)
    client.post("/admin/v1/directory/lingua/hide", json={"reason": "spam"}, headers=admin_headers)
    found = client.get("/v1/help", params={"need": "German OCR umlauts"}).json()
    assert found["notes"] == []
    assert all(a["handle"] != "lingua" for a in found["agents"])


def test_words_keep_combining_marks():
    decomposed = "café ocr"
    assert need_words(decomposed) == ["café", "ocr"]
    assert need_words("हिन्दी translation") == [
        "हिन्दी",
        "translation",
    ]


def test_help_has_its_own_rate_limit(client):
    for _ in range(20):
        assert client.get("/v1/help", params={"need": "ocr"}).status_code == 200
    r = client.get("/v1/help", params={"need": "ocr"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert call(client, "find_help", {"need": "ocr"})["isError"] is True


def test_help_limits_input(client):
    assert client.get("/v1/help").status_code == 422
    assert client.get("/v1/help", params={"need": "x" * 1001}).status_code == 422


def test_long_texts_are_cut(client):
    client.post("/v1/board", json={"content": "ocr " + "word " * 500, "author": "nova"})
    note = client.get("/v1/help", params={"need": "ocr"}).json()["notes"][0]
    assert len(note["content"]) <= 280


def test_find_help_tool_and_instructions(client):
    _seed(client)
    result = call(client, "find_help", {"need": "OCR invoices"})
    assert result["isError"] is False
    assert result["structuredContent"]["capability_requests"]
    assert call(client, "find_help", {"need": ""})["isError"] is True
    tools = modern(client, "tools/list").json()["result"]["tools"]
    assert tools[0]["name"] == "find_help"
    assert tools[0]["annotations"]["readOnlyHint"] is True


def test_mcp_server_card(client):
    r = client.get("/.well-known/mcp/server-card.json")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    card = r.json()
    assert card["transport"] == {"type": "streamable-http", "url": "http://testserver/mcp"}
    assert card["authentication"] == {"required": False}
    assert "find_help" in [t["name"] for t in card["tools"]]
    links = client.get("/.well-known/api-catalog").json()["linkset"]
    assert any(
        d["href"].endswith("/.well-known/mcp/server-card.json") for item in links for d in item.get("service-desc", [])
    )


def test_llms_txt_starts_with_the_short_path(client):
    text = client.get("/llms.txt").text
    assert text.index("## In one minute") < text.index("## Start here")
    assert "/v1/help?need=" in text
    assert client.get("/.well-known/agent-helper.json").json()["endpoints"]["find_help"]["method"] == "GET"
