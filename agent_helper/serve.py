"""Run the service: `python -m agent_helper.serve` (docs/decisions/0016).

The public port (`LISTEN_PORT`, default 8080) serves everything except `/admin`. With `ADMIN_PORT` set, a
second port serves only `/admin`, so the operator console can be reached through an SSH tunnel to a port
that is published on the host's loopback address only, and the reverse proxy never forwards to it.
Both ports are served by one process and one app, so they share the database and the sessions.
"""

from __future__ import annotations

import asyncio
import os
import socket

import uvicorn

from .app import create_app
from .config import Settings


def bind(port: int, host: str = "0.0.0.0") -> socket.socket:  # noqa: S104 (inside the container)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def main() -> None:
    settings = Settings.from_env()
    sockets = [bind(int(os.environ.get("LISTEN_PORT", "8080")))]
    if settings.admin_port is not None:
        sockets.append(bind(settings.admin_port))
    config = uvicorn.Config(
        create_app(settings),
        access_log=False,  # would record client addresses (docs/decisions/0008)
        proxy_headers=False,  # forwarded headers are handled by the app's own settings only
        log_level=settings.log_level,
    )
    asyncio.run(uvicorn.Server(config).serve(sockets=sockets))


if __name__ == "__main__":
    main()
