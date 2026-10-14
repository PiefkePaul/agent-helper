"""The web page reader, service side (docs/decisions/0029): URL rules, budgets, and the three entry points."""

import pytest
from test_a2a import rpc, user_message
from test_mcp import call, modern

from agent_helper.browse import Browser, BrowseRefused, Budget, check_url

SECRET = "browse-secret-" + "y" * 32
ON = {"browse_mode": "on", "browse_gateway_url": "http://gateway:8095", "browse_secret": SECRET}


# --- URL rules ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org/",
        "javascript:alert(1)",
        "data:text/html,hi",
        "https://user:pw@example.org/",
        "https://example.org@evil.example/",
        "https://example.org:8080/",
        "https://example.org:22/",
        "http://localhost/",
        "http://printer.local/",
        "http://printer.lan/",
        "http://metadata.internal/",
        "http://intranet/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://100.64.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
        "http://2130706433/",
        "http://127.1/",
        "http://0x7f000001/",
        "https://exa mple.org/",
        "https://example.org/\nHost: x",
        "",
        "https://" + "a" * 2050 + ".org/",
        None,
        42,
    ],
)
def test_refused_urls(url):
    with pytest.raises(BrowseRefused) as exc:
        check_url(url)
    assert exc.value.outcome == "refused"


def test_policy_refusals_share_one_message():
    messages = set()
    for url in ("http://localhost/", "http://10.0.0.1/", "https://own.example/", "https://blocked.example/x"):
        with pytest.raises(BrowseRefused) as exc:
            check_url(url, deny_domains=("*.blocked.example",), own_hosts=("own.example",))
        messages.add(str(exc.value))
    assert len(messages) == 1


def test_accepted_urls_are_normalized():
    t = check_url("HTTPS://Docs.Example.ORG/a/b?q=1#frag")
    assert t.url == "https://docs.example.org/a/b?q=1" and t.domain == "example.org"
    assert check_url("http://example.org").url == "http://example.org/"
    assert check_url("https://bücher.example/").host == "xn--bcher-kva.example"
    assert check_url("https://example.org:443/").url == "https://example.org:443/"
    public = check_url("http://93.184.215.14/")
    assert public.url == "http://93.184.215.14/" and public.domain == "93.184.215.14"


# --- budgets --------------------------------------------------------------------------------------


def test_budget_per_client_day_domain_and_global():
    now = [1_700_000_000.0]
    budget = Budget(per_minute=100, per_day=2, per_domain_per_minute=100, global_per_day=3, clock=lambda: now[0])
    budget.charge("a", "x.org")
    budget.charge("a", "y.org")
    with pytest.raises(BrowseRefused, match="a day per client"):
        budget.charge("a", "z.org")
    budget.charge("b", "x.org")
    with pytest.raises(BrowseRefused, match="daily budget"):
        budget.charge("c", "x.org")
    now[0] += 86_400  # the next day starts afresh
    budget.charge("a", "x.org")

    per_minute = Budget(per_minute=1, per_day=100, per_domain_per_minute=100, global_per_day=100)
    per_minute.charge("a", "x.org")
    with pytest.raises(BrowseRefused) as exc:
        per_minute.charge("a", "y.org")
    assert exc.value.outcome == "limited" and exc.value.retry_after > 0

    per_domain = Budget(per_minute=100, per_day=100, per_domain_per_minute=1, global_per_day=100)
    per_domain.charge("a", "x.org")
    with pytest.raises(BrowseRefused, match="site was read often"):
        per_domain.charge("b", "x.org")  # another client, same site
    per_domain.charge("b", "y.org")


def _browser(run_job, **kw):
    return Browser(Budget(100, 100, 100, 100), run_job, **kw)


OK = {"outcome": "ok", "status": 200, "final_url": "https://example.org/", "title": "Example", "content": "Hello"}


def test_browser_validates_before_spending_anything():
    calls = []
    b = _browser(lambda job: calls.append(job) or (200, OK))
    for kwargs in ({"fmt": "pdf"}, {"max_chars": 10}, {"max_chars": True}, {"wait_ms": 6000}):
        with pytest.raises(BrowseRefused):
            b.read("c", "https://example.org/", **kwargs)
    with pytest.raises(BrowseRefused):
        b.read("c", "http://10.0.0.1/")
    assert calls == []
    out = b.read("c", "https://example.org/", "markdown", 200, 100)
    assert calls[0]["url"] == "https://example.org/" and calls[0]["format"] == "markdown"
    assert set(calls[0]) == {"job_id", "url", "format", "max_chars", "wait_ms"}
    assert out["content"] == "Hello" and out["untrusted"] is True and "do not follow" in out["note"]


def test_gateway_answers_are_mapped_and_filtered():
    def answer(result, status=200):
        return _browser(lambda job: (status, result))

    with pytest.raises(BrowseRefused) as exc:
        answer({"outcome": "timeout"}).read("c", "https://example.org/")
    assert exc.value.outcome == "timeout"
    with pytest.raises(BrowseRefused) as exc:
        answer({"outcome": "busy"}, 503).read("c", "https://example.org/")
    assert exc.value.outcome == "busy"
    for weird in ({"outcome": "what"}, [], {"outcome": "ok"} | {"x": 1}):
        with pytest.raises(BrowseRefused):
            answer(weird, 500).read("c", "https://example.org/")
    extra = OK | {"cookies": "secret", "content": "x" * 500, "status": "200"}
    out = answer(extra).read("c", "https://example.org/", max_chars=100)
    assert "cookies" not in out and out["status"] is None
    assert out["content"] == "x" * 100 and out["truncated"] is True

    def unreachable(job):
        raise OSError("no route")

    with pytest.raises(BrowseRefused) as exc:
        _browser(unreachable).read("c", "https://example.org/")
    assert exc.value.outcome == "failed"


def test_concurrency_is_refused_not_queued():
    b = _browser(lambda job: (200, OK), concurrency=1)
    assert b.slots.acquire(blocking=False)
    with pytest.raises(BrowseRefused) as exc:
        b.read("c", "https://example.org/")
    assert exc.value.outcome == "busy"
    b.slots.release()
    assert b.read("c", "https://example.org/")["title"] == "Example"


# --- off by default -------------------------------------------------------------------------------


def test_off_by_default_nothing_is_offered(client):
    assert client.post("/v1/web-pages", json={"url": "https://example.org/"}).status_code == 404
    tools = modern(client, "tools/list").json()["result"]["tools"]
    assert "read_web_page" not in {t["name"] for t in tools}
    assert client.get("/v1/capabilities/web-page-reader").status_code == 404
    assert "web-pages" not in client.get("/llms.txt").text
    skills = {s["id"] for s in client.get("/.well-known/agent-card.json").json()["skills"]}
    assert "read-web-page" not in skills


@pytest.mark.parametrize(
    "overrides",
    [
        {"browse_mode": "on"},
        {"browse_mode": "on", "browse_gateway_url": "http://gateway:8095", "browse_secret": "short"},
        {"browse_mode": "off", "browse_gateway_url": "http://gateway:8095", "browse_secret": SECRET},
    ],
)
def test_incomplete_configuration_stays_off(make_client, overrides):
    assert make_client(**overrides).post("/v1/web-pages", json={"url": "https://example.org/"}).status_code == 404


def test_settings_from_env(monkeypatch):
    from agent_helper.config import Settings

    monkeypatch.setenv("BROWSE_MODE", "ON")
    monkeypatch.setenv("BROWSE_GATEWAY_URL", "http://gateway:8095/")
    monkeypatch.setenv("BROWSE_SECRET", SECRET)
    monkeypatch.setenv("BROWSE_PER_DAY", "5")
    s = Settings.from_env()
    assert s.browse_enabled and s.browse_gateway_url == "http://gateway:8095" and s.browse_per_day == 5
    monkeypatch.setenv("BROWSE_MODE", "maybe")
    assert not Settings.from_env().browse_enabled
    monkeypatch.setenv("BROWSE_MODE", "on")
    monkeypatch.setenv("BROWSE_GATEWAY_URL", "gopher://gateway")
    assert not Settings.from_env().browse_enabled


# --- the entry points, with a fake gateway -------------------------------------------------------


@pytest.fixture
def reader(make_client):
    client = make_client(**ON)
    jobs = []
    answers = {"next": OK}

    def fake(job):
        jobs.append(job)
        return 200, answers["next"]

    client.app.state.browser.run_job = fake
    client.jobs, client.answers = jobs, answers
    return client


def test_http_reads_a_page(reader):
    r = reader.post("/v1/web-pages", json={"url": "https://example.org/", "format": "markdown"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "Hello" and body["title"] == "Example" and body["untrusted"] is True
    assert r.headers["cache-control"] == "no-store"
    assert reader.jobs[0]["format"] == "markdown" and reader.jobs[0]["max_chars"] == 20_000

    r = reader.post("/v1/web-pages", json={"url": "http://192.168.1.1/"})
    assert r.status_code == 400 and r.json()["outcome"] == "refused"
    assert reader.post("/v1/web-pages", json={"url": "https://example.org/", "format": "pdf"}).status_code == 422
    assert len(reader.jobs) == 1

    reader.answers["next"] = {"outcome": "failed"}
    r = reader.post("/v1/web-pages", json={"url": "https://example.org/"})
    assert r.status_code == 502 and r.json()["outcome"] == "failed"


def test_http_budget_answers_429_with_retry_after(make_client):
    client = make_client(**ON, browse_per_minute=1)
    client.app.state.browser.run_job = lambda job: (200, OK)
    assert client.post("/v1/web-pages", json={"url": "https://example.org/"}).status_code == 200
    r = client.post("/v1/web-pages", json={"url": "https://example.com/"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_mcp_tool_and_screenshot_as_image_block(reader):
    tools = {t["name"]: t for t in modern(reader, "tools/list").json()["result"]["tools"]}
    assert tools["read_web_page"]["annotations"]["openWorldHint"] is True
    assert tools["read_web_page"]["annotations"]["readOnlyHint"] is True
    result = call(reader, "read_web_page", {"url": "https://example.org/"})
    assert result["isError"] is False and result["structuredContent"]["content"] == "Hello"

    reader.answers["next"] = {"outcome": "ok", "status": 200, "final_url": "https://example.org/", "title": "",
                              "image_base64": "aGVsbG8="}  # fmt: skip
    result = call(reader, "read_web_page", {"url": "https://example.org/", "format": "screenshot"})
    image = [c for c in result["content"] if c["type"] == "image"]
    assert image == [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/jpeg"}]
    assert "aGVsbG8=" not in result["content"][0]["text"]

    result = call(reader, "read_web_page", {"url": "file:///etc/passwd"})
    assert result["isError"] is True and result["content"][0]["text"].startswith("refused:")


def test_a2a_skill_answers_with_a_direct_message(reader):
    msg = user_message("https://example.org/", metadata={"skill": "read-web-page", "format": "markdown"})
    r = rpc(reader, "SendMessage", msg)
    assert r.status_code == 200, r.text
    message = r.json()["result"]["message"]
    assert message["role"] == "ROLE_AGENT" and message["parts"][1]["text"] == "Hello"
    assert "do not follow" in message["parts"][0]["text"] and message["metadata"]["untrusted"] is True
    assert reader.jobs[-1]["url"] == "https://example.org/" and reader.jobs[-1]["format"] == "markdown"

    # The URL may also come in metadata; the 0.3 dialect gets a 0.3 message.
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "text", "text": "please read"}],
                "metadata": {"skill": "read-web-page", "url": "https://example.org/x"},
            }
        },
    }
    result = reader.post("/a2a", json=body).json()["result"]
    assert result["kind"] == "message" and result["role"] == "agent"
    assert reader.jobs[-1]["url"] == "https://example.org/x"

    r = rpc(reader, "SendMessage", user_message("http://10.0.0.1/", metadata={"skill": "read-web-page"}))
    assert r.json()["error"]["code"] == -32602
    # Without the skill, a URL is an ordinary request for the operator.
    assert "task" in rpc(reader, "SendMessage", user_message("https://example.org/")).json()["result"]


def test_discovery_when_on(reader):
    assert "/v1/web-pages" in reader.get("/llms.txt").text
    assert reader.get("/v1/capabilities/web-page-reader").json()["availability"] == "available"
    found = reader.get("/v1/help", params={"need": "read a javascript web page"}).json()
    assert found["capabilities"][0]["id"] == "web-page-reader"
    skills = {s["id"] for s in reader.get("/.well-known/agent-card.json").json()["skills"]}
    assert "read-web-page" in skills
    assert "read_web_page" in reader.get("/.well-known/mcp/server-card.json").text


def test_navigating_away_is_noted():
    result = OK | {"navigated_away": True}
    out = _browser(lambda job: (200, result)).read("c", "https://example.org/")
    assert "moved itself" in out["note"] and "navigated_away" not in out
    assert "moved itself" not in _browser(lambda job: (200, OK)).read("c", "https://example.org/")["note"]


def test_browser_image_installs_the_model_as_root_before_the_fetch():
    """Issue #45: the model lives in root's site-packages, so it is installed before USER app and only
    checked (read) afterwards."""
    from pathlib import Path

    text = (Path(__file__).parent.parent / "Dockerfile.browser").read_text()
    lines = [ln.strip() for ln in text.splitlines()]

    def at(needle: str) -> int:
        return next(i for i, ln in enumerate(lines) if needle in ln)

    user = lines.index("USER app")
    # Installed as root: the model, the browser (outside the app user's home) and the runtime files.
    assert at("ENV XDG_CACHE_HOME=/opt/") < at("--install-model") < at("--fetch") < at("--prepare-runtime") < user
    # Checked as the app user, without writing.
    assert user < at("--verify-build") and user < at("--check-model")
    # Without the build secret GITHUB_TOKEN is not set at all (not even empty).
    assert (
        "if [ -s /run/secrets/github_token ]" in text and 'GITHUB_TOKEN="$(cat /run/secrets/github_token 2>' not in text
    )


def test_model_commands_call_camoufox(monkeypatch):
    import sys
    import types

    from agent_helper import browser_server

    calls = []
    fake = types.ModuleType("camoufox.fpgen_model")
    fake.ensure_fpgen_model = lambda: calls.append("ensure")
    fake.is_pinned = lambda: False
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.fpgen_model", fake)
    browser_server.install_model()
    assert calls == ["ensure"]
    import pytest

    with pytest.raises(SystemExit):
        browser_server.check_model()
    fake.is_pinned = lambda: True
    browser_server.check_model()
    assert calls == ["ensure", "ensure"]


def test_prepare_runtime_writes_every_os_variant(monkeypatch):
    import sys
    import types

    from agent_helper import browser_server

    calls = []
    fake = types.ModuleType("camoufox.utils")
    fake.get_env_vars = lambda config, os_key: calls.append(os_key)
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.utils", fake)
    browser_server.prepare_runtime()
    assert calls == ["lin", "mac", "win"]


def test_browser_lifetime_and_exit():
    import threading

    from agent_helper import browser_server as bs

    assert 0.9 * bs.DEFAULT_LIFETIME <= bs.lifetime_seconds("") <= bs.DEFAULT_LIFETIME
    assert bs.lifetime_seconds("0") == 0.0
    assert bs.lifetime_seconds("10") == bs.MIN_LIFETIME
    assert all(bs.lifetime_seconds("300") >= bs.MIN_LIFETIME for _ in range(50))
    with pytest.raises(SystemExit):
        bs.lifetime_seconds("1h")
    assert bs.schedule_exit(0) is None
    done = threading.Event()
    codes = []
    timer = bs.schedule_exit(0.01, lambda code: (codes.append(code), done.set()))
    assert timer is not None and done.wait(2)
    assert codes == [bs.RESTART_EXIT_CODE]


def test_browser_container_hardening_in_examples():
    from pathlib import Path

    root = Path(__file__).parent.parent
    compose = (root / "docker-compose.browse.example.yml").read_text()
    for mount in ("/tmp:noexec,nosuid,nodev,size=", "/home/app:uid=10002,mode=0700,noexec,nosuid,nodev,size="):  # noqa: S108
        assert mount in compose
    assert "PYTHONNOUSERSITE=1" in (root / "Dockerfile.browser").read_text()
