from agent_helper.limits import TokenBucket


def test_body_size_limit_with_content_length(make_client):
    client = make_client(max_body_bytes=200)
    r = client.post("/v1/requests", json={"message": "x" * 500})
    assert r.status_code == 413


def test_body_size_limit_without_content_length(make_client):
    client = make_client(max_body_bytes=200)

    def chunks():
        yield b'{"message": "'
        yield b"x" * 500
        yield b'"}'

    r = client.post("/v1/requests", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_write_rate_limit_returns_429_with_retry_after(make_client):
    client = make_client(write_per_minute=2)
    for _ in range(2):
        assert client.post("/v1/board", json={"content": "hi"}).status_code == 201
    r = client.post("/v1/board", json={"content": "hi"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    # reads use a separate budget
    assert client.get("/v1/board").status_code == 200


def test_token_bucket_refills():
    bucket = TokenBucket(per_minute=60)
    for _ in range(60):
        assert bucket.take("k", now=0.0) == 0
    assert bucket.take("k", now=0.0) > 0
    assert bucket.take("k", now=1.5) == 0


def test_proxy_header_ignored_unless_trusted(make_client):
    client = make_client(write_per_minute=1)
    assert (
        client.post("/v1/board", json={"content": "a"}, headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 201
    )
    r = client.post("/v1/board", json={"content": "b"}, headers={"X-Forwarded-For": "203.0.113.2"})
    assert r.status_code == 429


def test_proxy_header_used_when_trusted(make_client):
    client = make_client(write_per_minute=1, trust_proxy_headers=True)
    assert (
        client.post("/v1/board", json={"content": "a"}, headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 201
    )
    assert (
        client.post("/v1/board", json={"content": "b"}, headers={"X-Forwarded-For": "203.0.113.2"}).status_code == 201
    )


def test_admin_requires_secret(client):
    assert client.get("/admin/v1/requests").status_code == 401
    assert client.get("/admin/v1/requests", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_admin_absent_without_strong_secret(make_client):
    for secret in (None, "short"):
        client = make_client(admin_secret=secret)
        r = client.get("/admin/v1/requests", headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 404
