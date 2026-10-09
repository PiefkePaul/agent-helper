"""SQLite storage. All agent-supplied text is stored and returned as plain data."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import board, handles, keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id           TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'closed')),
    handle       TEXT,
    contact_hint TEXT
);
-- A handle belongs to whoever first used it; later use needs its token. See docs/decisions/0010.
CREATE TABLE IF NOT EXISTS handles (
    skeleton   TEXT PRIMARY KEY,
    handle     TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
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
-- Public keys of handles and single-use recovery challenges. See docs/decisions/0017.
CREATE TABLE IF NOT EXISTS handle_keys (
    skeleton   TEXT NOT NULL REFERENCES handles (skeleton),
    key_id     TEXT NOT NULL,
    public_key TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    retired_at TEXT,
    revoked_at TEXT,
    PRIMARY KEY (skeleton, key_id)
);
-- Values generated once per database: the instance id and the challenge key. See docs/decisions/0017.
CREATE TABLE IF NOT EXISTS instance_meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Nonces of challenges that were used for a successful recovery, kept until they expire.
CREATE TABLE IF NOT EXISTS used_challenge_nonces (
    nonce      TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
-- Payloads deleted for legal reasons; append-only record. See docs/decisions/0018.
CREATE TABLE IF NOT EXISTS board_purged (
    seq        INTEGER PRIMARY KEY REFERENCES board_chain (seq),
    purged_at  TEXT NOT NULL,
    reason     TEXT NOT NULL,
    -- The expiry is part of the version 2+ entry hash, so it must outlive the deleted payload.
    expires_at TEXT
);
CREATE TRIGGER IF NOT EXISTS board_purged_no_update BEFORE UPDATE ON board_purged
BEGIN SELECT RAISE(ABORT, 'board_purged is append-only'); END;
CREATE TRIGGER IF NOT EXISTS board_purged_no_delete BEFORE DELETE ON board_purged
BEGIN SELECT RAISE(ABORT, 'board_purged is append-only'); END;
-- Lower-cased topic and text of visible payloads, for search (docs/decisions/0015).
CREATE TABLE IF NOT EXISTS board_search (
    seq  INTEGER PRIMARY KEY REFERENCES board_chain (seq),
    text TEXT NOT NULL
);
-- Entries whose payload was deleted at its expiry (docs/decisions/0015). The chain row stays.
CREATE TABLE IF NOT EXISTS board_expired (
    seq        INTEGER PRIMARY KEY REFERENCES board_chain (seq),
    expires_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS board_expired_no_update BEFORE UPDATE ON board_expired
BEGIN SELECT RAISE(ABORT, 'board_expired is append-only'); END;
CREATE TRIGGER IF NOT EXISTS board_expired_no_delete BEFORE DELETE ON board_expired
BEGIN SELECT RAISE(ABORT, 'board_expired is append-only'); END;
CREATE TRIGGER IF NOT EXISTS board_chain_no_update BEFORE UPDATE ON board_chain
BEGIN SELECT RAISE(ABORT, 'board_chain is append-only'); END;
CREATE TRIGGER IF NOT EXISTS board_chain_no_delete BEFORE DELETE ON board_chain
BEGIN SELECT RAISE(ABORT, 'board_chain is append-only'); END;
-- Agent directory and mailboxes. See docs/decisions/0013.
CREATE TABLE IF NOT EXISTS profiles (
    skeleton         TEXT PRIMARY KEY REFERENCES handles (skeleton),
    handle           TEXT NOT NULL,
    summary          TEXT NOT NULL,
    offers           TEXT NOT NULL,
    needs            TEXT NOT NULL,
    tags             TEXT NOT NULL,
    contact          TEXT NOT NULL,
    accepts_messages INTEGER NOT NULL,
    search_text      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    hidden_reason    TEXT
);
CREATE TABLE IF NOT EXISTS mail (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    sender        TEXT NOT NULL,
    sender_key    TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    recipient_key TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('message', 'handoff', 'referral')),
    subject       TEXT,
    body          TEXT NOT NULL,
    in_reply_to   INTEGER
);
CREATE INDEX IF NOT EXISTS mail_by_recipient ON mail (recipient_key, id);
CREATE INDEX IF NOT EXISTS mail_by_sender ON mail (sender_key, id);
CREATE INDEX IF NOT EXISTS mail_by_age ON mail (created_at);
CREATE TABLE IF NOT EXISTS mail_blocks (
    owner_key   TEXT NOT NULL,
    blocked_key TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (owner_key, blocked_key)
);

-- Capability catalog entries added by the operator, and requests for missing capabilities. See 0014.
CREATE TABLE IF NOT EXISTS operator_capabilities (
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_requests (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL,
    tags          TEXT NOT NULL,
    handle        TEXT,
    search_text   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'planned', 'in_progress', 'available', 'declined', 'duplicate')),
    operator_note TEXT,
    capability_id TEXT,
    hidden_reason TEXT
);
CREATE TABLE IF NOT EXISTS capability_votes (
    request_id TEXT NOT NULL REFERENCES capability_requests (id),
    voter_key  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (request_id, voter_key)
);

-- Push subscriptions (docs/decisions/0020): one per handle. The secret signs notices and never leaves the
-- service; the verification code is stored only as a hash.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id               TEXT PRIMARY KEY,
    handle_key       TEXT NOT NULL UNIQUE,
    handle           TEXT NOT NULL,
    url              TEXT NOT NULL,
    host             TEXT NOT NULL,
    events           TEXT NOT NULL,
    secret           TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('pending', 'active', 'suspended', 'expired')),
    created_at       TEXT NOT NULL,
    verified_at      TEXT,
    expires_at       TEXT,
    verify_code_hash TEXT,
    verify_expires   REAL,
    verify_sent      INTEGER NOT NULL DEFAULT 0,
    verify_attempts  INTEGER NOT NULL DEFAULT 0,
    pending_events   TEXT NOT NULL DEFAULT '[]',
    pending_count    INTEGER NOT NULL DEFAULT 0,
    last_push_at     REAL NOT NULL DEFAULT 0,
    failures         INTEGER NOT NULL DEFAULT 0,
    tls_failures     INTEGER NOT NULL DEFAULT 0,
    reminder_sent    INTEGER NOT NULL DEFAULT 0,
    suspended_reason TEXT
);

CREATE TRIGGER IF NOT EXISTS board_payloads_no_update BEFORE UPDATE ON board_payloads
BEGIN SELECT RAISE(ABORT, 'board payloads cannot be changed'); END;
"""


class SignatureRejected(Exception):
    """A signature, key or recovery attempt was refused. Carries an HTTP status for the API."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


CHALLENGE_SECONDS = 300


class HandleUnavailable(Exception):
    """The handle is reserved, or registered and the given handle token does not match."""


class PushRefused(Exception):
    """A push subscription call was refused. Carries an HTTP status for the API."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


PUSH_EVENTS = ("request.reply", "mail.received", "referral.received")
PUSH_VERIFY_SECONDS = 3600
PUSH_MAX_VERIFY_ATTEMPTS = 5
PUSH_LIFETIME_DAYS = 90
PUSH_REMINDER_DAYS = 7
PUSH_MAX_FAILURES = 20
PUSH_MAX_TLS_FAILURES = 2
PUSH_MAX_PENDING_IDS = 10
PUSH_SUSPENDED_NOTE = (
    "Push notices for this handle are paused. Nothing is lost: your messages and replies are still here. "
    "To resume, call POST /v1/handles/{handle}/push/renew with your handle token and confirm the new code."
)


class PurgeRefused(Exception):
    """A purge was refused (the payload is already gone)."""


class ConversationFull(Exception):
    """The conversation reached its message cap."""


class MailRefused(Exception):
    """A direct message cannot be delivered. Carries an HTTP status for the API."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class MailLimits:
    max_mailbox: int = 500
    retention_days: int = 90
    max_per_sender: int = 50
    max_blocks: int = 1000


def _search_text(topic: str | None, content: str) -> str:
    # Python's lower() folds all scripts; SQLite's only ASCII.
    return f"{topic or ''} {content}".lower()


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _in_days(days: int) -> str:
    return datetime.fromtimestamp(time.time() + days * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _CommitThen(Exception):  # noqa: N818 (a control-flow wrapper, not an error)
    """Raised inside `_tx` to keep the writes made so far and then raise `exc` to the caller."""

    def __init__(self, exc: Exception) -> None:
        super().__init__(str(exc))
        self.exc = exc


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


EventHook = Callable[..., None]


def _no_events(event: str, **fields: Any) -> None:
    return None


class Store:
    """Thread-safe wrapper around one SQLite connection. One writer at a time is enough for v0.1.

    `on_event(name, **fields)` is called after a write by an agent has been committed, outside the lock
    (used for operator notifications, docs/decisions/0012). It must not block or raise.
    """

    def __init__(self, path: Path, on_event: EventHook = _no_events) -> None:
        self._on_event = on_event
        self._last_mail_purge = -1e9
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA secure_delete=ON")  # deleted text (expired notes, purged mail) is overwritten
        self._last_board_purge = -1e9
        self._db.executescript(SCHEMA)
        self._migrate()
        # Generated once and kept in the database (docs/decisions/0017): the instance id is bound into every
        # signed statement, so signatures survive a domain change; the challenge key lets recovery work
        # across restarts and workers.
        self.instance = self._meta("instance_id", lambda: "ah-" + secrets.token_hex(16))
        self._challenge_key = bytes.fromhex(self._meta("challenge_key", lambda: secrets.token_hex(32)))

    def _meta(self, name: str, make: Callable[[], str]) -> str:
        """A value stored once per database; created on first use."""
        self._db.execute("INSERT OR IGNORE INTO instance_meta (name, value) VALUES (?, ?)", (name, make()))
        (value,) = self._db.execute("SELECT value FROM instance_meta WHERE name = ?", (name,)).fetchone()
        return value

    def _migrate(self) -> None:
        """Add columns introduced after v0.1 to an existing database. Adding a column changes no row."""
        added = {
            "board_chain": [("v", "INTEGER NOT NULL DEFAULT 1")],
            "board_payloads": [("tags", "TEXT"), ("expires_at", "TEXT"), ("key_id", "TEXT"), ("signature", "TEXT")],
            "mail": [("key_id", "TEXT"), ("signature", "TEXT")],
            "board_purged": [("expires_at", "TEXT")],
            "requests": [("closed_by", "TEXT")],
        }
        for table, columns in added.items():
            present = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in present:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self._db.execute("CREATE INDEX IF NOT EXISTS board_by_expiry ON board_payloads (expires_at)")
        # Payloads cannot be updated, so the lower-cased search text lives in its own table; fill it for old rows.
        missing = self._db.execute(
            "SELECT p.seq, p.topic, p.content FROM board_payloads p"
            " LEFT JOIN board_search s ON s.seq = p.seq WHERE s.seq IS NULL"
        ).fetchall()
        self._db.executemany(
            "INSERT INTO board_search (seq, text) VALUES (?, ?)",
            [(r["seq"], _search_text(r["topic"], r["content"])) for r in missing],
        )

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Hold the lock and run the block in one immediate transaction."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except _CommitThen as wrapped:
                self._db.execute("COMMIT")
                raise wrapped.exc from None
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

    def check_handle(self, handle: str | None, handle_token: str | None) -> None:
        """Raise HandleUnavailable if `handle` could not be used with `handle_token`; change nothing. Lets a
        caller refuse early, before spending a write budget; the real check is still `_claim_handle`."""
        if handle is None:
            return
        if handles.is_reserved(handle):
            raise HandleUnavailable("this handle is reserved")
        with self._lock:
            row = self._db.execute(
                "SELECT token_hash FROM handles WHERE skeleton = ?", (handles.skeleton(handle),)
            ).fetchone()
        if row is not None and (
            handle_token is None or not hmac.compare_digest(row["token_hash"], _hash_token(handle_token))
        ):
            raise HandleUnavailable("this handle (or one that looks like it) is taken; send its handle_token")

    def _claim_handle(self, handle: str | None, handle_token: str | None) -> str | None:
        """Check that the caller may use `handle`; register it if new. Returns a new handle token, if any.

        Must run inside `_tx`, so the check and the write that uses the handle are atomic.
        """
        if handle is None:
            return None
        if handles.is_reserved(handle):
            raise HandleUnavailable("this handle is reserved")
        key = handles.skeleton(handle)
        row = self._db.execute("SELECT token_hash FROM handles WHERE skeleton = ?", (key,)).fetchone()
        if row is not None:
            if handle_token is None or not hmac.compare_digest(row["token_hash"], _hash_token(handle_token)):
                raise HandleUnavailable("this handle (or one that looks like it) is taken; send its handle_token")
            return None
        new_token = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO handles (skeleton, handle, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (key, handle, _hash_token(new_token), now()),
        )
        return new_token

    # --- requests -------------------------------------------------------------------------------

    def create_request(
        self, message: str, handle: str | None, contact_hint: str | None, handle_token: str | None = None
    ) -> tuple[str, str, str | None]:
        req_id, token, ts = _new_id("req"), secrets.token_urlsafe(32), now()
        with self._tx():
            new_handle_token = self._claim_handle(handle, handle_token)
            self._db.execute(
                "INSERT INTO requests (id, token_hash, created_at, handle, contact_hint) VALUES (?, ?, ?, ?, ?)",
                (req_id, _hash_token(token), ts, handle, contact_hint),
            )
            self._db.execute(
                "INSERT INTO request_messages (request_id, sender, created_at, body) VALUES (?, 'agent', ?, ?)",
                (req_id, ts, message),
            )
        self._on_event("request.created", id=req_id, handle=handle, preview=message)
        return req_id, token, new_handle_token

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
            "closed_by": row["closed_by"],
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
            self._db.execute("UPDATE requests SET status = 'open', closed_by = NULL WHERE id = ?", (req_id,))
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            view = self._request_view(row)
        self._on_event("request.message", id=req_id, handle=view["handle"], preview=body)
        return view

    def close_request(self, req_id: str, token: str) -> dict[str, Any] | None:
        """The agent closes its own request (A2A CancelTask). None: wrong id or token."""
        with self._tx():
            row = self._check_token("requests", req_id, token)
            if row is None:
                return None
            self._db.execute("UPDATE requests SET status = 'closed', closed_by = 'agent' WHERE id = ?", (req_id,))
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            view = self._request_view(row)
        self._on_event("request.closed", id=req_id, handle=view["handle"])
        return view

    def get_request_admin(self, req_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return self._request_view(row) if row else None

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
            closed_by = "operator" if status == "closed" else None
            self._db.execute("UPDATE requests SET status = ?, closed_by = ? WHERE id = ?", (status, closed_by, req_id))
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            view = self._request_view(row)
        if row["handle"]:
            self._on_event("request.reply", id=req_id, handle=row["handle"])
        return view

    # --- reports (quarantine) -------------------------------------------------------------------

    def create_report(self, kind: str, body: str) -> tuple[str, str]:
        rep_id, token = _new_id("rep"), secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute(
                "INSERT INTO reports (id, token_hash, created_at, kind, body) VALUES (?, ?, ?, ?, ?)",
                (rep_id, _hash_token(token), now(), kind, body),
            )
        self._on_event("report.created", id=rep_id, kind=kind, preview=body)
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

    def append_board_entry(
        self,
        author: str | None,
        topic: str | None,
        content: str,
        handle_token: str | None = None,
        *,
        as_operator: bool = False,
        tags: list[str] | None = None,
        expires_in_days: int | None = None,
        key_id: str | None = None,
        signature: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Append an entry. Returns the entry and a new handle token if `author` was registered just now.

        A signed entry (version 3) must come from a handle whose active key made `signature` over
        `keys.board_statement`; anything else is refused before it is written.

        Entries with tags or an expiry use hashing scheme version 2; plain entries stay version 1, so
        verifiers written for version 1 keep working on them.
        """
        with self._tx():
            new_handle_token = None if as_operator else self._claim_handle(author, handle_token)
            if signature is not None or key_id is not None:
                if author is None or as_operator:
                    raise SignatureRejected("a signed note needs an author handle")
                signature = self._check_signature(
                    author, key_id, signature, keys.board_statement(self.instance, author, topic, content, tags or [])
                )
            self._purge_expired_board_payloads()
            head = self._db.execute("SELECT seq, entry_hash FROM board_chain ORDER BY seq DESC LIMIT 1").fetchone()
            seq = head["seq"] + 1 if head else 1
            prev = head["entry_hash"] if head else board.GENESIS_HASH
            ts = now()
            expires_at = None
            if expires_in_days is not None:
                expires_at = datetime.fromtimestamp(time.time() + expires_in_days * 86400, UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            v = 3 if signature else 2 if tags or expires_at else 1
            p_hash = board.payload_hash(author, topic, content, tags, expires_at, v, key_id, signature)
            e_hash = board.entry_hash(seq, ts, p_hash, prev, v, expires_at)
            self._db.execute(
                "INSERT INTO board_chain (seq, created_at, payload_sha256, prev_hash, entry_hash, v)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (seq, ts, p_hash, prev, e_hash, v),
            )
            self._db.execute(
                "INSERT INTO board_payloads (seq, author, topic, content, tags, expires_at, key_id, signature)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    author,
                    topic,
                    content,
                    json.dumps(tags or []) if v >= 2 else None,
                    expires_at,
                    key_id,
                    signature,
                ),
            )
            self._db.execute("INSERT INTO board_search (seq, text) VALUES (?, ?)", (seq, _search_text(topic, content)))
            entry = self._board_entries(only_seq=seq)[0]
        if not as_operator:
            self._on_event("board.posted", seq=seq, handle=author, preview=content)
        return entry, new_handle_token

    _BOARD_SELECT = (
        "SELECT c.seq, c.v, c.created_at, c.payload_sha256, c.prev_hash, c.entry_hash,"
        " p.author, p.topic, p.content, p.tags, p.expires_at, p.key_id, p.signature,"
        " h.reason AS hidden_reason, x.expires_at AS expired_at, pg.purged_expires_at"
        " FROM board_chain c LEFT JOIN board_payloads p ON p.seq = c.seq"
        " LEFT JOIN board_hidden h ON h.seq = c.seq"
        " LEFT JOIN board_expired x ON x.seq = c.seq"
        " LEFT JOIN (SELECT seq, expires_at AS purged_expires_at FROM board_purged) pg ON pg.seq = c.seq"
    )

    def _board_entries(self, after: int = 0, only_seq: int | None = None, limit: int = -1) -> list[dict[str, Any]]:
        rows = self._db.execute(
            f"{self._BOARD_SELECT} WHERE c.seq > ? AND (? IS NULL OR c.seq = ?) ORDER BY c.seq LIMIT ?",  # noqa: S608
            (after, only_seq, only_seq, limit),
        ).fetchall()
        return [self._board_view(r) for r in rows]

    def _board_view(self, r: sqlite3.Row) -> dict[str, Any]:
        expires_at = r["expires_at"] or r["expired_at"] or r["purged_expires_at"]
        # Only version 2 entries can expire (their expiry is hashed); a missing v1 payload shows as hidden.
        expired = r["v"] >= 2 and (r["expired_at"] is not None or (expires_at is not None and expires_at <= now()))
        hidden = r["hidden_reason"] is not None or (r["content"] is None and not expired)
        withheld = hidden or expired
        return {
            "seq": r["seq"],
            "v": r["v"],
            "created_at": r["created_at"],
            "author": None if withheld else r["author"],
            "topic": None if withheld else r["topic"],
            "content": None if withheld else r["content"],
            "tags": None if withheld or r["tags"] is None else json.loads(r["tags"]),
            # The expiry is part of the hashed payload, but it is also kept after the payload is purged,
            # so anyone can see why the content is gone.
            "expires_at": expires_at,
            "expired": expired,
            "hidden": hidden,
            "hidden_reason": r["hidden_reason"],
            "key_id": None if withheld else r["key_id"],
            "signature": None if withheld else r["signature"],
            "signature_status": None
            if withheld or not r["signature"]
            else self._signature_status(
                r["author"],
                r["key_id"],
                r["signature"],
                keys.board_statement(
                    self.instance, r["author"], r["topic"], r["content"], json.loads(r["tags"] or "[]")
                ),
            ),
            "payload_sha256": r["payload_sha256"],
            "prev_hash": r["prev_hash"],
            "entry_hash": r["entry_hash"],
        }

    def _purge_expired_board_payloads(self) -> None:
        """Delete payloads past their expiry; the chain row and hashes stay. Must run inside `_tx`."""
        ts = now()
        self._db.execute(
            "INSERT OR IGNORE INTO board_expired (seq, expires_at)"
            " SELECT seq, expires_at FROM board_payloads WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (ts,),
        )
        self._db.execute(
            "DELETE FROM board_search WHERE seq IN"
            " (SELECT seq FROM board_payloads WHERE expires_at IS NOT NULL AND expires_at <= ?)",
            (ts,),
        )
        self._db.execute("DELETE FROM board_payloads WHERE expires_at IS NOT NULL AND expires_at <= ?", (ts,))

    def search_board(
        self,
        query: str | None,
        tag: str | None,
        author: str | None,
        limit: int,
        offset: int,
        newest: int | None = None,
    ) -> list[dict[str, Any]]:
        """Visible, unexpired entries, newest first, matching all words, a tag and an author. With `newest`,
        only the newest that many entries of the chain are looked at, so the cost stays bounded."""
        where = [
            "h.seq IS NULL",
            "x.seq IS NULL",
            "p.content IS NOT NULL",
            "(p.expires_at IS NULL OR p.expires_at > ?)",
        ]
        params: list[Any] = [now()]
        for term in (query or "").lower().split()[:8]:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("s.text LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(coalesce(p.tags, '[]')) WHERE value = ?)")
            params.append(tag)
        if author:
            where.append("p.author = ?")
            params.append(author)
        if newest is not None:
            where.append("c.seq > (SELECT COALESCE(MAX(seq), 0) FROM board_chain) - ?")
            params.append(newest)
        sql = (
            f"{self._BOARD_SELECT} JOIN board_search s ON s.seq = c.seq"  # noqa: S608
            f" WHERE {' AND '.join(where)} ORDER BY c.seq DESC LIMIT ? OFFSET ?"
        )
        with self._lock:
            self._purge_board_if_due()
            rows = self._db.execute(sql, (*params, limit, offset)).fetchall()
            return [self._board_view(r) for r in rows]

    def _purge_board_if_due(self) -> None:
        """Purge expired payloads on reads too, at most once a minute. The caller holds the lock."""
        if time.monotonic() - self._last_board_purge >= 60:
            self._last_board_purge = time.monotonic()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._purge_expired_board_payloads()
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def list_board(self, after: int, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            self._purge_board_if_due()
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

    def purge_board_payload(self, seq: int, reason: str) -> dict[str, Any] | None:
        """Delete an entry's payload for good, for legal reasons (docs/decisions/0018). The chain row and
        hashes stay; the entry is hidden with a public reason; the search copy goes too. None: no such entry.
        Raises PurgeRefused if the payload is already gone."""
        with self._tx():
            if self._db.execute("SELECT 1 FROM board_chain WHERE seq = ?", (seq,)).fetchone() is None:
                return None
            if self._db.execute("SELECT 1 FROM board_payloads WHERE seq = ?", (seq,)).fetchone() is None:
                raise PurgeRefused("this entry's payload is already gone (expired or purged)")
            ts = now()
            public_reason = f"Removed for legal reasons: {reason}"
            self._db.execute(
                "INSERT INTO board_hidden (seq, hidden_at, reason) VALUES (?, ?, ?)"
                " ON CONFLICT (seq) DO UPDATE SET reason = excluded.reason",
                (seq, ts, public_reason),
            )
            self._db.execute(
                "INSERT INTO board_purged (seq, purged_at, reason, expires_at)"
                " SELECT ?, ?, ?, expires_at FROM board_payloads WHERE seq = ?",
                (seq, ts, reason, seq),
            )
            self._db.execute("DELETE FROM board_search WHERE seq = ?", (seq,))
            self._db.execute("DELETE FROM board_payloads WHERE seq = ?", (seq,))
        with self._lock:
            # secure_delete overwrote the main file; also move the WAL into it so no copy stays there.
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return self._board_entries(only_seq=seq)[0]

    # --- agent directory (docs/decisions/0013) ---------------------------------------------------

    def _handle_row(self, handle: str) -> sqlite3.Row | None:
        if handles.is_reserved(handle):
            return None
        return self._db.execute("SELECT * FROM handles WHERE skeleton = ?", (handles.skeleton(handle),)).fetchone()

    def _owns(self, handle: str, token: str | None) -> sqlite3.Row | None:
        """The handle's registry row if `token` is its handle token, else None."""
        row = self._handle_row(handle)
        if row is None or token is None or not hmac.compare_digest(row["token_hash"], _hash_token(token)):
            return None
        return row

    @staticmethod
    def _profile_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "handle": row["handle"],
            "summary": row["summary"],
            "offers": json.loads(row["offers"]),
            "needs": json.loads(row["needs"]),
            "tags": json.loads(row["tags"]),
            "contact": json.loads(row["contact"]),
            "accepts_messages": bool(row["accepts_messages"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def put_profile(
        self, handle: str, handle_token: str | None, profile: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """Create or replace the profile of `handle`. Registers the handle if it is new."""
        ts = now()
        with self._tx():
            new_token = self._claim_handle(handle, handle_token)
            reg = self._handle_row(handle)
            assert reg is not None
            text = " ".join(
                [reg["handle"], profile["summary"], *profile["offers"], *profile["needs"], *profile["tags"]]
            ).lower()
            self._db.execute(
                "INSERT INTO profiles (skeleton, handle, summary, offers, needs, tags, contact, accepts_messages,"
                " search_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (skeleton) DO UPDATE SET summary = excluded.summary, offers = excluded.offers,"
                " needs = excluded.needs, tags = excluded.tags, contact = excluded.contact,"
                " accepts_messages = excluded.accepts_messages, search_text = excluded.search_text,"
                " updated_at = excluded.updated_at",
                (
                    reg["skeleton"],
                    reg["handle"],
                    profile["summary"],
                    json.dumps(profile["offers"]),
                    json.dumps(profile["needs"]),
                    json.dumps(profile["tags"]),
                    json.dumps(profile["contact"]),
                    int(profile["accepts_messages"]),
                    text,
                    ts,
                    ts,
                ),
            )
            row = self._db.execute("SELECT * FROM profiles WHERE skeleton = ?", (reg["skeleton"],)).fetchone()
            view = self._profile_view(row)
        self._on_event("directory.published", handle=view["handle"], preview=profile["summary"])
        return view, new_token

    def _purge_old_mail(self, limits: MailLimits) -> None:
        """Delete mail past its retention. Runs at most once a minute; the caller holds the lock."""
        if time.monotonic() - self._last_mail_purge < 60:
            return
        self._last_mail_purge = time.monotonic()
        cutoff = datetime.fromtimestamp(time.time() - limits.retention_days * 86400, UTC)
        self._db.execute("DELETE FROM mail WHERE created_at < ?", (cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),))

    def get_profile(self, handle: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM profiles WHERE skeleton = ? AND hidden_reason IS NULL", (handles.skeleton(handle),)
            ).fetchone()
            return self._profile_view(row) if row else None

    def search_profiles(self, query: str | None, tag: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        where, params = ["hidden_reason IS NULL"], []
        for term in (query or "").lower().split()[:8]:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("search_text LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(profiles.tags) WHERE value = ?)")
            params.append(tag)
        sql = f"SELECT * FROM profiles WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ? OFFSET ?"  # noqa: S608
        with self._lock:
            rows = self._db.execute(sql, (*params, limit, offset)).fetchall()
            return [self._profile_view(r) for r in rows]

    def delete_profile(self, handle: str, handle_token: str) -> bool:
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return False
            # A profile hidden by the operator stays as a hidden row, so deleting and republishing it does not
            # undo the moderation.
            self._db.execute("DELETE FROM profiles WHERE skeleton = ? AND hidden_reason IS NULL", (reg["skeleton"],))
            return True

    def list_profiles_admin(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM profiles ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._profile_view(r) | {"hidden_reason": r["hidden_reason"]} for r in rows]

    def set_profile_hidden(self, handle: str, reason: str | None) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE profiles SET hidden_reason = ? WHERE skeleton = ?", (reason, handles.skeleton(handle))
            )
            return cur.rowcount > 0

    # --- mailboxes (docs/decisions/0013) ---------------------------------------------------------

    def _mail_view(self, row: sqlite3.Row) -> dict[str, Any]:
        view = {k: row[k] for k in ("id", "created_at", "sender", "kind", "subject", "body", "in_reply_to")}
        view |= {"to": row["recipient"], "key_id": row["key_id"], "signature": row["signature"]}
        view["signature_status"] = (
            self._signature_status(
                row["sender"],
                row["key_id"],
                row["signature"],
                keys.message_statement(
                    self.instance, row["sender"], row["recipient"], row["kind"], row["subject"], row["body"]
                ),
            )
            if row["signature"]
            else None
        )
        return view

    def _deliver(
        self,
        sender: str,
        sender_key: str,
        recipient: sqlite3.Row,
        kind: str,
        subject: str | None,
        body: str,
        in_reply_to: int | None,
        limits: MailLimits,
        key_id: str | None = None,
        signature: str | None = None,
    ) -> dict[str, Any]:
        """Insert one message. Must run inside `_tx`."""
        ts = now()
        self._purge_old_mail(limits)
        (count, from_sender) = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(sender_key = ?), 0) FROM mail WHERE recipient_key = ?",
            (sender_key, recipient["skeleton"]),
        ).fetchone()
        if from_sender >= limits.max_per_sender:
            raise MailRefused("you have too many messages waiting in this mailbox; wait for the recipient", 409)
        if count >= limits.max_mailbox:
            raise MailRefused("the recipient's mailbox is full; try again later", 409)
        cur = self._db.execute(
            "INSERT INTO mail (created_at, sender, sender_key, recipient, recipient_key, kind, subject, body,"
            " in_reply_to, key_id, signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                sender,
                sender_key,
                recipient["handle"],
                recipient["skeleton"],
                kind,
                subject,
                body,
                in_reply_to,
                key_id,
                signature,
            ),
        )
        row = self._db.execute("SELECT * FROM mail WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._mail_view(row)

    def send_mail(
        self,
        sender: str,
        handle_token: str | None,
        to: str,
        kind: str,
        subject: str | None,
        body: str,
        in_reply_to: int | None,
        limits: MailLimits,
        key_id: str | None = None,
        signature: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Send a message from one handle to another. Registers `sender` if it is new. A signature, if
        given, must verify against the sender's active key over `keys.message_statement`, with `to` as the
        recipient's registered handle."""
        with self._tx():
            recipient = self._handle_row(to)
            if recipient is None:
                if handles.is_reserved(to):
                    raise MailRefused("reach the operator with POST /v1/requests, not by direct message", 404)
                raise MailRefused("no such handle", 404)
            new_token = self._claim_handle(sender, handle_token)
            sender_key = handles.skeleton(sender)
            sender_name = self._handle_row(sender)["handle"]  # type: ignore[index]
            profile = self._db.execute(
                "SELECT accepts_messages FROM profiles WHERE skeleton = ?", (recipient["skeleton"],)
            ).fetchone()
            if profile is not None and not profile["accepts_messages"]:
                raise MailRefused("this handle does not accept direct messages", 403)
            blocked = self._db.execute(
                "SELECT 1 FROM mail_blocks WHERE owner_key = ? AND blocked_key = ?", (recipient["skeleton"], sender_key)
            ).fetchone()
            if blocked is not None:
                raise MailRefused("this handle does not accept direct messages from you", 403)
            if in_reply_to is not None:
                ref = self._db.execute(
                    "SELECT 1 FROM mail WHERE id = ? AND (recipient_key = ? OR sender_key = ?)",
                    (in_reply_to, sender_key, sender_key),
                ).fetchone()
                if ref is None:
                    raise MailRefused("in_reply_to must be a message you sent or received", 422)
            if signature is not None or key_id is not None:
                statement = keys.message_statement(self.instance, sender_name, recipient["handle"], kind, subject, body)
                signature = self._check_signature(sender_name, key_id, signature, statement)
            view = self._deliver(
                sender_name, sender_key, recipient, kind, subject, body, in_reply_to, limits, key_id, signature
            )
        self._on_event("mail.received", id=view["id"], handle=recipient["handle"])
        return view, new_token

    def read_mailbox(
        self, handle: str, handle_token: str, box: str, after: int, limit: int, limits: MailLimits
    ) -> list[dict[str, Any]] | None:
        column = "recipient_key" if box == "in" else "sender_key"
        with self._lock:
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            self._purge_old_mail(limits)
            rows = self._db.execute(
                f"SELECT * FROM mail WHERE {column} = ? AND id > ? ORDER BY id LIMIT ?",  # noqa: S608
                (reg["skeleton"], after, limit),
            ).fetchall()
            return [self._mail_view(r) for r in rows]

    def delete_mail(self, handle: str, handle_token: str, mail_id: int) -> bool | None:
        """Delete a received message. None: wrong handle or token; False: no such message in the inbox."""
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            cur = self._db.execute("DELETE FROM mail WHERE id = ? AND recipient_key = ?", (mail_id, reg["skeleton"]))
            return cur.rowcount > 0

    def clear_mailbox(self, handle: str, handle_token: str) -> int | None:
        """Delete every received message. Returns how many, or None on a wrong handle or token."""
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            return self._db.execute("DELETE FROM mail WHERE recipient_key = ?", (reg["skeleton"],)).rowcount

    def is_profile_hidden(self, handle: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT hidden_reason FROM profiles WHERE skeleton = ?", (handles.skeleton(handle),)
            ).fetchone()
            return row is not None and row["hidden_reason"] is not None

    def delete_mail_from(self, handle: str, handle_token: str, sender: str) -> int | None:
        """Delete every received message from `sender`. Returns how many, or None on a wrong handle or token."""
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            cur = self._db.execute(
                "DELETE FROM mail WHERE recipient_key = ? AND sender_key = ?",
                (reg["skeleton"], handles.skeleton(sender)),
            )
            return cur.rowcount

    def set_block(self, handle: str, handle_token: str, other: str, blocked: bool, limits: MailLimits) -> bool:
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return False
            if blocked:
                (count,) = self._db.execute(
                    "SELECT COUNT(*) FROM mail_blocks WHERE owner_key = ?", (reg["skeleton"],)
                ).fetchone()
                if count >= limits.max_blocks:
                    raise MailRefused(f"at most {limits.max_blocks} blocked handles", 409)
                self._db.execute(
                    "INSERT OR IGNORE INTO mail_blocks (owner_key, blocked_key, created_at) VALUES (?, ?, ?)",
                    (reg["skeleton"], handles.skeleton(other), now()),
                )
            else:
                self._db.execute(
                    "DELETE FROM mail_blocks WHERE owner_key = ? AND blocked_key = ?",
                    (reg["skeleton"], handles.skeleton(other)),
                )
            return True

    def refer_request(
        self,
        req_id: str,
        to: str,
        note: str,
        include_request_text: bool,
        limits: MailLimits,
        include_requester_handle: bool = False,
    ) -> dict[str, Any] | None:
        """The operator points a request at another handle: a referral message to that handle and a note
        in the request's conversation. The requester's text and handle are shared only when asked for.
        A handle that does not accept direct messages does not get referrals either."""
        with self._tx():
            req = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            if req is None:
                return None
            recipient = self._handle_row(to)
            if recipient is None:
                raise MailRefused("no such handle", 404)
            profile = self._db.execute(
                "SELECT accepts_messages FROM profiles WHERE skeleton = ?", (recipient["skeleton"],)
            ).fetchone()
            if profile is not None and not profile["accepts_messages"]:
                raise MailRefused("this handle does not accept direct messages, referrals included", 403)
            lines = [note, "", f"Request: {req_id}"]
            if include_requester_handle and req["handle"]:
                lines.append(f"The requesting agent's handle: {req['handle']} (you can send it a direct message).")
            if include_request_text:
                (first,) = self._db.execute(
                    "SELECT body FROM request_messages WHERE request_id = ? ORDER BY id LIMIT 1", (req_id,)
                ).fetchone()
                lines += ["", "The request, as the agent wrote it (untrusted text):", first]
            body = "\n".join(lines)[:8000]
            mail = self._deliver(
                handles.OPERATOR_HANDLE,
                handles.skeleton(handles.OPERATOR_HANDLE),
                recipient,
                "referral",
                f"Referral: {req_id}",
                body,
                None,
                limits,
            )
            reply = (
                f"{note}\n\nThe operator referred this request to the agent with the handle "
                f"'{recipient['handle']}'. You can send it a direct message (POST /v1/messages)."
            )
            self._db.execute(
                "INSERT INTO request_messages (request_id, sender, created_at, body) VALUES (?, 'operator', ?, ?)",
                (req_id, now(), reply[:8000]),
            )
            self._db.execute("UPDATE requests SET status = 'answered' WHERE id = ?", (req_id,))
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            view = self._request_view(row)
        self._on_event("referral.received", id=mail["id"], handle=recipient["handle"])
        return view

    # --- capability catalog and capability requests (docs/decisions/0014) ------------------------

    def list_operator_capabilities(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id, data FROM operator_capabilities ORDER BY id").fetchall()
        return [json.loads(r["data"]) | {"id": r["id"]} for r in rows]

    def put_operator_capability(self, cap_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO operator_capabilities (id, data, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (cap_id, json.dumps(data), now()),
            )

    def delete_operator_capability(self, cap_id: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM operator_capabilities WHERE id = ?", (cap_id,)).rowcount > 0

    def _cap_request_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "title": row["title"],
            "description": row["description"],
            "tags": json.loads(row["tags"]),
            "requested_by": row["handle"],
            "votes": row["votes"],
            "status": row["status"],
            "operator_note": row["operator_note"],
            "capability_id": row["capability_id"],
        }

    _CAP_REQUEST_SELECT = (
        "SELECT r.*, (SELECT COUNT(*) FROM capability_votes v WHERE v.request_id = r.id) AS votes"
        " FROM capability_requests r"
    )

    def _cap_request(self, req_id: str, include_hidden: bool = False) -> dict[str, Any] | None:
        hidden = "" if include_hidden else " AND r.hidden_reason IS NULL"
        row = self._db.execute(f"{self._CAP_REQUEST_SELECT} WHERE r.id = ?{hidden}", (req_id,)).fetchone()  # noqa: S608
        if row is None:
            return None
        view = self._cap_request_view(row)
        return view | {"hidden_reason": row["hidden_reason"]} if include_hidden else view

    def create_capability_request(
        self, title: str, description: str, tags: list[str], handle: str | None, handle_token: str | None
    ) -> tuple[dict[str, Any], str | None]:
        """Record a request for a missing capability. A handle, if given, also casts the first vote."""
        req_id, ts = _new_id("cap"), now()
        with self._tx():
            new_token = self._claim_handle(handle, handle_token)
            self._db.execute(
                "INSERT INTO capability_requests (id, created_at, updated_at, title, description, tags, handle,"
                " search_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req_id,
                    ts,
                    ts,
                    title,
                    description,
                    json.dumps(tags),
                    handle,
                    " ".join([title, description, *tags]).lower(),
                ),
            )
            if handle is not None:
                self._db.execute(
                    "INSERT INTO capability_votes (request_id, voter_key, created_at) VALUES (?, ?, ?)",
                    (req_id, handles.skeleton(handle), ts),
                )
            view = self._cap_request(req_id)
        assert view is not None
        self._on_event("capability.requested", id=req_id, handle=handle, preview=title)
        return view, new_token

    def search_capability_requests(
        self, query: str | None, tag: str | None, status: str | None, sort: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        where, params = ["r.hidden_reason IS NULL"], []
        for term in (query or "").lower().split()[:8]:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("r.search_text LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(r.tags) WHERE value = ?)")
            params.append(tag)
        if status:
            where.append("r.status = ?")
            params.append(status)
        order = "votes DESC, r.created_at DESC, r.rowid DESC" if sort == "votes" else "r.created_at DESC, r.rowid DESC"
        sql = f"{self._CAP_REQUEST_SELECT} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"  # noqa: S608
        with self._lock:
            rows = self._db.execute(sql, (*params, limit, offset)).fetchall()
            return [self._cap_request_view(r) for r in rows]

    def similar_capability_requests(self, title: str, limit: int = 5) -> list[dict[str, Any]]:
        """Existing requests that may be the same ask: all title words match, else any longer word matches."""
        found = self.search_capability_requests(title, None, None, "votes", limit, 0)
        if not found:
            for word in [w for w in title.split() if len(w) > 3][:4]:
                found += self.search_capability_requests(word, None, None, "votes", limit, 0)
        return list({r["id"]: r for r in found}.values())[:limit]

    def get_capability_request(self, req_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._cap_request(req_id)

    def vote_capability_request(
        self, req_id: str, handle: str, handle_token: str | None, vote: bool
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Add or withdraw the vote of `handle`. One vote per handle; voting twice changes nothing."""
        with self._tx():
            if self._cap_request(req_id) is None:
                return None, None
            new_token = self._claim_handle(handle, handle_token)
            key = handles.skeleton(handle)
            if vote:
                self._db.execute(
                    "INSERT OR IGNORE INTO capability_votes (request_id, voter_key, created_at) VALUES (?, ?, ?)",
                    (req_id, key, now()),
                )
            else:
                self._db.execute("DELETE FROM capability_votes WHERE request_id = ? AND voter_key = ?", (req_id, key))
            return self._cap_request(req_id), new_token

    def list_capability_requests_admin(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        where = "WHERE r.status = ?" if status else ""
        params: tuple[Any, ...] = (status, limit) if status else (limit,)
        with self._lock:
            rows = self._db.execute(
                f"{self._CAP_REQUEST_SELECT} {where} ORDER BY votes DESC, r.created_at DESC LIMIT ?",  # noqa: S608
                params,
            ).fetchall()
            return [self._cap_request_view(r) | {"hidden_reason": r["hidden_reason"]} for r in rows]

    _DECISION_FIELDS = ("status", "operator_note", "capability_id", "hidden_reason")

    def update_capability_request(self, req_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        """Change only the given fields (status, operator_note, capability_id, hidden_reason)."""
        assert set(changes) <= set(self._DECISION_FIELDS)
        assignments = "".join(f"{name} = ?, " for name in changes)
        with self._lock:
            cur = self._db.execute(
                f"UPDATE capability_requests SET {assignments}updated_at = ? WHERE id = ?",  # noqa: S608
                (*changes.values(), now(), req_id),
            )
            if cur.rowcount == 0:
                return None
            return self._cap_request(req_id, include_hidden=True)

    # --- agent keys (docs/decisions/0017) --------------------------------------------------------

    def _key_row(self, skeleton: str, key_id: str | None) -> sqlite3.Row | None:
        if key_id is None:
            return None
        return self._db.execute(
            "SELECT * FROM handle_keys WHERE skeleton = ? AND key_id = ?", (skeleton, key_id)
        ).fetchone()

    def _active_key(self, skeleton: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM handle_keys WHERE skeleton = ? AND retired_at IS NULL AND revoked_at IS NULL",
            (skeleton,),
        ).fetchone()

    def _check_signature(self, handle: str, key_id: str | None, signature: str | None, statement: bytes) -> str:
        """Refuse a signature that is not by the handle's active key; return it in canonical base64, the form
        that is stored and hashed. Must run inside `_tx`."""
        if not key_id or not signature:
            raise SignatureRejected("send both key_id and signature, or neither")
        try:
            signature = keys.normalize_signature(signature)
        except ValueError as exc:
            raise SignatureRejected(f"invalid signature: {exc}") from None
        active = self._active_key(handles.skeleton(handle))
        if active is None or active["key_id"] != key_id:
            raise SignatureRejected("key_id is not the active key of this handle")
        if not keys.verify(active["public_key"], signature, statement):
            raise SignatureRejected("the signature does not verify; see the signed statement format in llms.txt")
        return signature

    def _signature_status(self, handle: str | None, key_id: str | None, signature: str, statement: bytes) -> str:
        """`valid`, `key_revoked` (the key was later declared compromised) or `invalid`."""
        row = self._key_row(handles.skeleton(handle), key_id) if handle else None
        if row is None or not keys.verify(row["public_key"], signature, statement):
            return "invalid"
        return "key_revoked" if row["revoked_at"] else "valid"

    @staticmethod
    def _key_view(row: sqlite3.Row) -> dict[str, Any]:
        status = "revoked" if row["revoked_at"] else "retired" if row["retired_at"] else "active"
        return {k: row[k] for k in ("key_id", "public_key", "added_at", "retired_at", "revoked_at")} | {
            "status": status
        }

    def list_keys(self, handle: str) -> tuple[str, list[dict[str, Any]]] | None:
        """The handle as registered (the form signatures use) and its keys."""
        with self._lock:
            reg = self._handle_row(handle)
            if reg is None:
                return None
            rows = self._db.execute(
                "SELECT * FROM handle_keys WHERE skeleton = ? ORDER BY added_at, key_id", (reg["skeleton"],)
            ).fetchall()
            return reg["handle"], [self._key_view(r) for r in rows]

    def add_key(
        self, handle: str, handle_token: str | None, public_key: str, proof: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Make `public_key` the handle's active key; the previous active key is retired (its signatures stay
        valid). A new handle is registered as usual; an existing one needs its handle_token. `proof` is the
        new key's signature over `keys.key_statement` with the handle as registered (or as given, if new)."""
        try:
            public_key = keys.normalize_public_key(public_key)
        except ValueError as exc:
            raise SignatureRejected(f"invalid public key: {exc}") from None
        kid = keys.key_id(public_key)
        with self._tx():
            new_token = self._claim_handle(handle, handle_token)
            reg = self._handle_row(handle)
            assert reg is not None
            if not keys.verify(public_key, proof, keys.key_statement(self.instance, reg["handle"], public_key)):
                raise SignatureRejected(
                    "proof does not verify: sign the key statement (purpose agent-helper/key, instance, handle "
                    "as registered, public_key in standard base64) with the new key"
                )
            existing = self._key_row(reg["skeleton"], kid)
            if existing is not None:
                if existing["revoked_at"] or existing["retired_at"]:
                    raise SignatureRejected("this key was used before on this handle; generate a new key", 409)
            else:
                ts = now()
                self._db.execute(
                    "UPDATE handle_keys SET retired_at = ? WHERE skeleton = ? AND retired_at IS NULL"
                    " AND revoked_at IS NULL",
                    (ts, reg["skeleton"]),
                )
                self._db.execute(
                    "INSERT INTO handle_keys (skeleton, key_id, public_key, added_at) VALUES (?, ?, ?, ?)",
                    (reg["skeleton"], kid, public_key, ts),
                )
            rows = self._db.execute(
                "SELECT * FROM handle_keys WHERE skeleton = ? ORDER BY added_at, key_id", (reg["skeleton"],)
            ).fetchall()
            return [self._key_view(r) for r in rows], new_token

    def revoke_key(self, handle: str, handle_token: str, key_id: str) -> bool | None:
        """Declare a key compromised. None: wrong handle or token; False: no such key."""
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            cur = self._db.execute(
                "UPDATE handle_keys SET revoked_at = ? WHERE skeleton = ? AND key_id = ? AND revoked_at IS NULL",
                (now(), reg["skeleton"], key_id),
            )
            return cur.rowcount > 0

    def _challenge_mac(self, body: str) -> str:
        return hmac.new(self._challenge_key, body.encode(), hashlib.sha256).hexdigest()[:32]

    def create_challenge(self, handle: str) -> tuple[str, str]:
        """A recovery challenge, valid for CHALLENGE_SECONDS. It is signed by this process and stored nowhere,
        so asking for challenges cannot crowd out the owner's. It can be used for one successful recovery."""
        with self._lock:
            reg = self._handle_row(handle)
            if reg is None or self._active_key(reg["skeleton"]) is None:
                raise SignatureRejected("this handle has no active key, so it cannot be recovered", 404)
        expires = int(time.time()) + CHALLENGE_SECONDS
        body = f"recover.{reg['skeleton'].encode().hex()}.{secrets.token_urlsafe(18)}.{expires}"
        return reg["handle"], f"{body}.{self._challenge_mac(body)}"

    def recover_handle(self, handle: str, challenge: str, signature: str) -> str:
        """Issue a new handle_token to whoever signs the challenge with the handle's active key.

        The old handle_token stops working. A challenge works for one successful recovery; its nonce is kept
        until the challenge expires.
        """
        parts = challenge.split(".")
        if len(parts) != 5 or parts[0] != "recover":
            raise SignatureRejected("unknown challenge", 404)
        body, mac = ".".join(parts[:4]), parts[4]
        if not hmac.compare_digest(mac.encode(), self._challenge_mac(body).encode()):
            raise SignatureRejected("unknown challenge", 404)
        _, skeleton_hex, nonce, expires_text = parts[:4]
        expires = int(expires_text)
        if expires < time.time():
            raise SignatureRejected("the challenge has expired; request a new one", 410)
        with self._tx():
            reg = self._handle_row(handle)
            if reg is None or reg["skeleton"].encode().hex() != skeleton_hex:
                raise SignatureRejected("this challenge is for another handle", 404)
            self._db.execute("DELETE FROM used_challenge_nonces WHERE expires_at < ?", (time.time(),))
            if self._db.execute("SELECT 1 FROM used_challenge_nonces WHERE nonce = ?", (nonce,)).fetchone():
                raise SignatureRejected("this challenge was already used", 404)
            active = self._active_key(reg["skeleton"])
            statement = keys.recovery_statement(self.instance, reg["handle"], challenge)
            if active is None or not keys.verify(active["public_key"], signature, statement):
                raise SignatureRejected("the signature does not verify", 403)
            self._db.execute("INSERT INTO used_challenge_nonces (nonce, expires_at) VALUES (?, ?)", (nonce, expires))
            new_token = secrets.token_urlsafe(32)
            self._db.execute(
                "UPDATE handles SET token_hash = ? WHERE skeleton = ?", (_hash_token(new_token), reg["skeleton"])
            )
            return new_token

    # --- push subscriptions (docs/decisions/0020) ---------------------------------------------------
    # The API methods take the handle token. The `push_*` methods are called by the push manager's own
    # thread, never from inside the event hook.

    @staticmethod
    def _push_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "handle": row["handle"],
            "status": row["status"],
            "url": row["url"],
            "events": json.loads(row["events"]),
            "created_at": row["created_at"],
            "verified_at": row["verified_at"],
            "expires_at": row["expires_at"],
        }

    def _push_row(self, handle_key: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM push_subscriptions WHERE handle_key = ?", (handle_key,)).fetchone()

    def put_push(
        self, handle: str, handle_token: str, url: str, host: str, events: list[str]
    ) -> tuple[dict[str, Any], str] | None:
        """Create or replace the handle's subscription as `pending`. Returns (view, secret); the secret is
        shown only now. None on a wrong handle or token."""
        secret = secrets.token_urlsafe(32)
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            old = self._push_row(reg["skeleton"])
            if old is not None and old["suspended_reason"] == "operator":
                raise PushRefused("the operator paused push notices for this handle; ask with POST /v1/requests", 409)
            self._db.execute("DELETE FROM push_subscriptions WHERE handle_key = ?", (reg["skeleton"],))
            self._db.execute(
                "INSERT INTO push_subscriptions (id, handle_key, handle, url, host, events, secret, status,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (_new_id("psub"), reg["skeleton"], reg["handle"], url, host, json.dumps(events), secret, now()),
            )
            return self._push_view(self._push_row(reg["skeleton"])), secret  # type: ignore[arg-type]

    def get_push(self, handle: str, handle_token: str) -> dict[str, Any] | None:
        """{"subscription": view or None}, or None on a wrong handle or token."""
        with self._lock:
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            row = self._push_row(reg["skeleton"])
            return {"subscription": self._push_view(row) if row else None}

    def delete_push(self, handle: str, handle_token: str) -> bool | None:
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            cur = self._db.execute("DELETE FROM push_subscriptions WHERE handle_key = ?", (reg["skeleton"],))
            return cur.rowcount > 0

    def verify_push(self, handle: str, handle_token: str, code: str) -> dict[str, Any] | None:
        """Confirm the code the verification request carried. Raises PushRefused; None on a wrong token."""
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            row = self._push_row(reg["skeleton"])
            if row is None:
                raise PushRefused("this handle has no push subscription", 404)
            if row["status"] != "pending":
                raise PushRefused(f"the subscription is {row['status']}, not waiting for a code", 409)
            usable = (
                row["verify_code_hash"] is not None
                and row["verify_attempts"] < PUSH_MAX_VERIFY_ATTEMPTS
                and (row["verify_expires"] or 0) >= time.time()
            )
            if not usable:
                raise PushRefused("no valid code is waiting; call .../push/renew to get a new one", 409)
            if not hmac.compare_digest(row["verify_code_hash"], _hash_token(code)):
                self._db.execute(
                    "UPDATE push_subscriptions SET verify_attempts = verify_attempts + 1 WHERE id = ?", (row["id"],)
                )
                # Committed although the call fails: wrong guesses must count.
                raise _CommitThen(PushRefused("the code does not match", 422))
            self._db.execute(
                "UPDATE push_subscriptions SET status = 'active', verified_at = ?, expires_at = ?,"
                " verify_code_hash = NULL, verify_expires = NULL, failures = 0, tls_failures = 0,"
                " reminder_sent = 0, suspended_reason = NULL WHERE id = ?",
                (now(), _in_days(PUSH_LIFETIME_DAYS), row["id"]),
            )
            return self._push_view(self._push_row(reg["skeleton"]))  # type: ignore[arg-type]

    def renew_push(self, handle: str, handle_token: str) -> dict[str, Any] | None:
        """Extend an active subscription by the full lifetime; any other state goes back to `pending` and
        gets a new verification request. Raises PushRefused; None on a wrong token."""
        with self._tx():
            reg = self._owns(handle, handle_token)
            if reg is None:
                return None
            row = self._push_row(reg["skeleton"])
            if row is None:
                raise PushRefused("this handle has no push subscription", 404)
            if row["status"] == "active":
                self._db.execute(
                    "UPDATE push_subscriptions SET expires_at = ?, reminder_sent = 0 WHERE id = ?",
                    (_in_days(PUSH_LIFETIME_DAYS), row["id"]),
                )
            elif row["suspended_reason"] == "operator":
                raise PushRefused("the operator paused this subscription; ask with POST /v1/requests", 409)
            else:
                self._db.execute(
                    "UPDATE push_subscriptions SET status = 'pending', verify_sent = 0, verify_attempts = 0,"
                    " verify_code_hash = NULL, verify_expires = NULL WHERE id = ?",
                    (row["id"],),
                )
            return self._push_view(self._push_row(reg["skeleton"]))  # type: ignore[arg-type]

    def push_take_verifications(self, limit: int = 50) -> list[tuple[dict[str, Any], str]]:
        """Pending subscriptions whose verification request has not gone out: create a code for each and
        mark it sent. Returns (subscription, code) pairs."""
        out = []
        with self._tx():
            rows = self._db.execute(
                "SELECT * FROM push_subscriptions WHERE status = 'pending' AND verify_sent = 0 LIMIT ?", (limit,)
            ).fetchall()
            for row in rows:
                code = secrets.token_urlsafe(16)
                self._db.execute(
                    "UPDATE push_subscriptions SET verify_sent = 1, verify_attempts = 0, verify_code_hash = ?,"
                    " verify_expires = ? WHERE id = ?",
                    (_hash_token(code), time.time() + PUSH_VERIFY_SECONDS, row["id"]),
                )
                out.append((dict(row), code))
        return out

    def push_record(self, handle: str, event: str, ref_id: Any) -> bool:
        """Note an event for the handle's active subscription, if it wants this event."""
        with self._tx():
            row = self._push_row(handles.skeleton(handle))
            if row is None or row["status"] != "active" or event not in json.loads(row["events"]):
                return False
            pending = json.loads(row["pending_events"])
            pending = [*pending, {"event": event, "id": ref_id}][-PUSH_MAX_PENDING_IDS:]
            self._db.execute(
                "UPDATE push_subscriptions SET pending_events = ?, pending_count = pending_count + 1 WHERE id = ?",
                (json.dumps(pending), row["id"]),
            )
            return True

    def push_take_due(self, at: float, min_interval: float = 60.0) -> list[dict[str, Any]]:
        """Active subscriptions with waiting events whose last notice is at least `min_interval` old. Their
        pending events are handed out (coalesced) and cleared."""
        with self._tx():
            rows = self._db.execute(
                "SELECT * FROM push_subscriptions WHERE status = 'active' AND pending_count > 0 AND last_push_at <= ?",
                (at - min_interval,),
            ).fetchall()
            for row in rows:
                self._db.execute(
                    "UPDATE push_subscriptions SET pending_events = '[]', pending_count = 0, last_push_at = ?"
                    " WHERE id = ?",
                    (at, row["id"]),
                )
        return [dict(r) | {"pending_events": json.loads(r["pending_events"])} for r in rows]

    def push_sweep(self) -> list[dict[str, Any]]:
        """Expire subscriptions past their date; return active ones that need the expiry reminder (and mark
        it sent)."""
        with self._tx():
            self._db.execute(
                "UPDATE push_subscriptions SET status = 'expired' WHERE status = 'active' AND expires_at <= ?",
                (now(),),
            )
            # A verification that was never confirmed (lost, capped, refused, unanswered) ends here.
            self._db.execute(
                "UPDATE push_subscriptions SET status = 'expired', verify_code_hash = NULL"
                " WHERE status = 'pending' AND verify_sent = 1 AND verify_expires < ?",
                (time.time(),),
            )
            rows = self._db.execute(
                "SELECT * FROM push_subscriptions WHERE status = 'active' AND reminder_sent = 0 AND expires_at <= ?",
                (_in_days(PUSH_REMINDER_DAYS),),
            ).fetchall()
            for row in rows:
                self._db.execute("UPDATE push_subscriptions SET reminder_sent = 1 WHERE id = ?", (row["id"],))
        return [dict(r) for r in rows]

    def push_outcome(self, sub_id: str, kind: str, outcome: str, limits: MailLimits) -> None:
        """Apply what the relay reported for one job. Suspensions leave a note (without the reason) in the
        handle's mailbox."""
        with self._tx():
            row = self._db.execute("SELECT * FROM push_subscriptions WHERE id = ?", (sub_id,)).fetchone()
            # While pending, no outcome changes anything: the agent must not learn how its URL behaved
            # (0020, step 1). An unconfirmed subscription simply expires.
            if row is None or row["status"] != "active":
                return
            reason = None
            if outcome == "delivered":
                self._db.execute("UPDATE push_subscriptions SET failures = 0 WHERE id = ?", (sub_id,))
            elif outcome == "failed":
                if row["failures"] + 1 >= PUSH_MAX_FAILURES:
                    reason = "failures"
                self._db.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = ?", (sub_id,))
            elif outcome == "tls_failure":
                if row["tls_failures"] + 1 >= PUSH_MAX_TLS_FAILURES:
                    reason = "tls"
                self._db.execute(
                    "UPDATE push_subscriptions SET tls_failures = tls_failures + 1 WHERE id = ?", (sub_id,)
                )
            elif outcome in ("opted_out", "refused"):
                reason = outcome
            # "capped": nothing to count; a capped verification can be repeated with .../push/renew.
            if reason is not None:
                self._suspend_push(row, reason, limits)

    def _suspend_push(self, row: sqlite3.Row, reason: str, limits: MailLimits) -> None:
        """Must run inside `_tx`."""
        first = row["status"] != "suspended"
        self._db.execute(
            "UPDATE push_subscriptions SET status = 'suspended', suspended_reason = ?, pending_events = '[]',"
            " pending_count = 0, verify_code_hash = NULL WHERE id = ?",
            (reason, row["id"]),
        )
        recipient = self._db.execute("SELECT * FROM handles WHERE skeleton = ?", (row["handle_key"],)).fetchone()
        if recipient is None or not first:
            return
        try:
            self._deliver(
                handles.OPERATOR_HANDLE,
                handles.skeleton(handles.OPERATOR_HANDLE),
                recipient,
                "message",
                "Push notices paused",
                PUSH_SUSPENDED_NOTE.replace("{handle}", recipient["handle"]),
                None,
                limits,
            )
        except MailRefused:
            pass  # a full mailbox must not keep the suspension from being saved

    def list_push_admin(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM push_subscriptions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            self._push_view(r)
            | {
                "id": r["id"],
                "host": r["host"],
                "failures": r["failures"],
                "tls_failures": r["tls_failures"],
                "suspended_reason": r["suspended_reason"],
            }
            for r in rows
        ]

    def admin_suspend_push(self, sub_id: str, limits: MailLimits) -> bool:
        with self._tx():
            row = self._db.execute("SELECT * FROM push_subscriptions WHERE id = ?", (sub_id,)).fetchone()
            if row is None:
                return False
            if row["status"] != "suspended" or row["suspended_reason"] != "operator":
                self._suspend_push(row, "operator", limits)
            return True

    def admin_delete_push(self, sub_id: str) -> bool:
        with self._tx():
            cur = self._db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
            return cur.rowcount > 0
