"""SQLite storage. All agent-supplied text is stored and returned as plain data."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import board

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id           TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'closed')),
    handle       TEXT,
    contact_hint TEXT
);
CREATE TABLE IF NOT EXISTS request_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES requests (id),
    sender     TEXT NOT NULL CHECK (sender IN ('agent', 'operator')),
    created_at TEXT NOT NULL,
    body       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id            TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    body          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'quarantined'
                  CHECK (status IN ('quarantined', 'accepted', 'rejected', 'duplicate')),
    operator_note TEXT
);

-- The chain is append-only. Payloads may never be changed. See docs/decisions/0005.
CREATE TABLE IF NOT EXISTS board_chain (
    seq            INTEGER PRIMARY KEY,
    created_at     TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    entry_hash     TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS board_payloads (
    seq     INTEGER PRIMARY KEY REFERENCES board_chain (seq),
    author  TEXT,
    topic   TEXT,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board_hidden (
    seq       INTEGER PRIMARY KEY REFERENCES board_chain (seq),
    hidden_at TEXT NOT NULL,
    reason    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS board_chain_no_update BEFORE UPDATE ON board_chain
BEGIN SELECT RAISE(ABORT, 'board_chain is append-only'); END;
CREATE TRIGGER IF NOT EXISTS board_chain_no_delete BEFORE DELETE ON board_chain
BEGIN SELECT RAISE(ABORT, 'board_chain is append-only'); END;
CREATE TRIGGER IF NOT EXISTS board_payloads_no_update BEFORE UPDATE ON board_payloads
BEGIN SELECT RAISE(ABORT, 'board payloads cannot be changed'); END;
"""


class ConversationFull(Exception):
    """The conversation reached its message cap."""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Store:
    """Thread-safe wrapper around one SQLite connection. One writer at a time is enough for v0.1."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Hold the lock and run the block in one immediate transaction."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- token handling -------------------------------------------------------------------------

    def _check_token(self, table: str, item_id: str, token: str) -> sqlite3.Row | None:
        row = self._db.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()  # noqa: S608
        if row is None or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
            return None
        return row

    # --- requests -------------------------------------------------------------------------------

    def create_request(self, message: str, handle: str | None, contact_hint: str | None) -> tuple[str, str]:
        req_id, token, ts = _new_id("req"), secrets.token_urlsafe(32), now()
        with self._tx():
            self._db.execute(
                "INSERT INTO requests (id, token_hash, created_at, handle, contact_hint) VALUES (?, ?, ?, ?, ?)",
                (req_id, _hash_token(token), ts, handle, contact_hint),
            )
            self._db.execute(
                "INSERT INTO request_messages (request_id, sender, created_at, body) VALUES (?, 'agent', ?, ?)",
                (req_id, ts, message),
            )
        return req_id, token

    def _request_view(self, row: sqlite3.Row) -> dict[str, Any]:
        msgs = self._db.execute(
            "SELECT sender, created_at, body FROM request_messages WHERE request_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "handle": row["handle"],
            "contact_hint": row["contact_hint"],
            "messages": [dict(m) for m in msgs],
        }

    def get_request(self, req_id: str, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._check_token("requests", req_id, token)
            return self._request_view(row) if row else None

    def add_agent_message(self, req_id: str, token: str, body: str, max_messages: int) -> dict[str, Any] | None:
        with self._tx():
            row = self._check_token("requests", req_id, token)
            if row is None:
                return None
            (count,) = self._db.execute(
                "SELECT COUNT(*) FROM request_messages WHERE request_id = ?", (req_id,)
            ).fetchone()
            if count >= max_messages:
                raise ConversationFull(req_id)
            self._db.execute(
                "INSERT INTO request_messages (request_id, sender, created_at, body) VALUES (?, 'agent', ?, ?)",
                (req_id, now(), body),
            )
            self._db.execute("UPDATE requests SET status = 'open' WHERE id = ?", (req_id,))
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return self._request_view(row)

    def list_requests(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM requests WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._request_view(r) for r in rows]

    def add_operator_reply(self, req_id: str, body: str, status: str) -> dict[str, Any] | None:
        with self._tx():
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            if row is None:
                return None
            self._db.execute(
                "INSERT INTO request_messages (request_id, sender, created_at, body) VALUES (?, 'operator', ?, ?)",
                (req_id, now(), body),
            )
            self._db.execute("UPDATE requests SET status = ? WHERE id = ?", (status, req_id))
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return self._request_view(row)

    # --- reports (quarantine) -------------------------------------------------------------------

    def create_report(self, kind: str, body: str) -> tuple[str, str]:
        rep_id, token = _new_id("rep"), secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute(
                "INSERT INTO reports (id, token_hash, created_at, kind, body) VALUES (?, ?, ?, ?, ?)",
                (rep_id, _hash_token(token), now(), kind, body),
            )
        return rep_id, token

    @staticmethod
    def _report_view(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in ("id", "created_at", "kind", "body", "status", "operator_note")}

    def get_report(self, rep_id: str, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._check_token("reports", rep_id, token)
            return self._report_view(row) if row else None

    def list_reports(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM reports WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._report_view(r) for r in rows]

    def set_report_status(self, rep_id: str, status: str, note: str | None) -> dict[str, Any] | None:
        with self._lock:
            cur = self._db.execute(
                "UPDATE reports SET status = ?, operator_note = ? WHERE id = ?", (status, note, rep_id)
            )
            if cur.rowcount == 0:
                return None
            row = self._db.execute("SELECT * FROM reports WHERE id = ?", (rep_id,)).fetchone()
            return self._report_view(row)

    # --- board ----------------------------------------------------------------------------------

    def append_board_entry(self, author: str | None, topic: str | None, content: str) -> dict[str, Any]:
        with self._tx():
            head = self._db.execute("SELECT seq, entry_hash FROM board_chain ORDER BY seq DESC LIMIT 1").fetchone()
            seq = head["seq"] + 1 if head else 1
            prev = head["entry_hash"] if head else board.GENESIS_HASH
            ts = now()
            p_hash = board.payload_hash(author, topic, content)
            e_hash = board.entry_hash(seq, ts, p_hash, prev)
            self._db.execute(
                "INSERT INTO board_chain (seq, created_at, payload_sha256, prev_hash, entry_hash)"
                " VALUES (?, ?, ?, ?, ?)",
                (seq, ts, p_hash, prev, e_hash),
            )
            self._db.execute(
                "INSERT INTO board_payloads (seq, author, topic, content) VALUES (?, ?, ?, ?)",
                (seq, author, topic, content),
            )
            return self._board_entries(only_seq=seq)[0]

    def _board_entries(self, after: int = 0, only_seq: int | None = None, limit: int = -1) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT c.seq, c.created_at, c.payload_sha256, c.prev_hash, c.entry_hash,"
            " p.author, p.topic, p.content, h.reason AS hidden_reason"
            " FROM board_chain c LEFT JOIN board_payloads p ON p.seq = c.seq"
            " LEFT JOIN board_hidden h ON h.seq = c.seq"
            " WHERE c.seq > ? AND (? IS NULL OR c.seq = ?) ORDER BY c.seq LIMIT ?",
            (after, only_seq, only_seq, limit),
        ).fetchall()
        out = []
        for r in rows:
            hidden = r["hidden_reason"] is not None or r["content"] is None
            entry = {
                "seq": r["seq"],
                "created_at": r["created_at"],
                "author": None if hidden else r["author"],
                "topic": None if hidden else r["topic"],
                "content": None if hidden else r["content"],
                "hidden": hidden,
                "hidden_reason": r["hidden_reason"],
                "payload_sha256": r["payload_sha256"],
                "prev_hash": r["prev_hash"],
                "entry_hash": r["entry_hash"],
            }
            out.append(entry)
        return out

    def list_board(self, after: int, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            return self._board_entries(after=after, limit=limit)

    def get_board_entry(self, seq: int) -> dict[str, Any] | None:
        with self._lock:
            rows = self._board_entries(only_seq=seq)
            return rows[0] if rows else None

    def board_head(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT seq, entry_hash FROM board_chain ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            return {"seq": 0, "entry_hash": board.GENESIS_HASH}
        return {"seq": row["seq"], "entry_hash": row["entry_hash"]}

    def hide_board_entry(self, seq: int, reason: str) -> dict[str, Any] | None:
        with self._lock:
            if self._db.execute("SELECT 1 FROM board_chain WHERE seq = ?", (seq,)).fetchone() is None:
                return None
            self._db.execute(
                "INSERT INTO board_hidden (seq, hidden_at, reason) VALUES (?, ?, ?)"
                " ON CONFLICT (seq) DO UPDATE SET reason = excluded.reason",
                (seq, now(), reason),
            )
            return self._board_entries(only_seq=seq)[0]
