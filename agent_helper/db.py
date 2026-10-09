"""Opening the SQLite database, the same way for the service and the maintenance commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: Path, *, timeout: float = 5.0, check_same_thread: bool = True) -> sqlite3.Connection:
    """A connection in autocommit mode (transactions are explicit) with the settings every writer needs.

    `recursive_triggers` matters for integrity: without it, INSERT OR REPLACE deletes the old row without
    firing the no-delete triggers that keep the board, its checkpoints and revocation records unchangeable.
    """
    db = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=check_same_thread)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA recursive_triggers=ON")
    db.execute("PRAGMA secure_delete=ON")  # deleted text (expired notes, purged mail) is overwritten
    return db
