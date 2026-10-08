# 0007: Python, FastAPI, SQLite, one container

- Status: accepted
- Date: 2026-10-08

## Context

The service should run for years on modest self-hosted hardware such as a NAS, be easy to audit, and be
easy to back up.

## Decision

1. **Python 3.12 + FastAPI**, served by uvicorn.
2. **SQLite** through the standard library (no ORM), one database file in a data volume.
3. **One Docker container**, running as a non-root user. `docker-compose.example.yml` shows the shape;
   live compose files stay outside the repository.
4. TLS, public exposure, and the operator's second factor are handled by a **reverse proxy in front of
   the container**, not by the application.
5. Backups are copies of the data volume made with SQLite's online backup, for example
   `sqlite3 agent-helper.db ".backup <target>"`.

## Consequences

- Small dependency surface and a single file to back up.
- One writer at a time. This is ample for the expected load and can be revisited if it is not.
- Which reverse proxy and which second factor are used is an operations decision outside this repository.
