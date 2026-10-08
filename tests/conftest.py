import dataclasses
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from agent_helper.app import create_app
from agent_helper.config import Settings

ADMIN_SECRET = "test-admin-secret-" + "x" * 32


@pytest.fixture
def make_client(tmp_path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(**overrides) -> TestClient:
        settings = Settings(
            public_base_url="http://testserver",
            data_dir=tmp_path,
            admin_secret=ADMIN_SECRET,
            write_per_minute=1000,
            read_per_minute=1000,
        )
        settings = dataclasses.replace(settings, **overrides)
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}
