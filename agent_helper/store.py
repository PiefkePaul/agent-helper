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

from . import board, handles

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

CREATE TRIGGER IF NOT EXISTS board_payloads_no_update BEFORE UPDATE ON board_payloads
BEGIN SELECT RAISE(ABORT, 'board payloads cannot be changed'); END;
"""


class HandleUnavailable(Exception):
    """The handle is reserved, or registered and the given handle token does not match."""


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


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


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
            view = self._request_view(row)
        self._on_event("request.message", id=req_id, handle=view["handle"], preview=body)
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
    ) -> tuple[dict[str, Any], str | None]:
        """Append an entry. Returns the entry and a new handle token if `author` was registered just now."""
        with self._tx():
            new_handle_token = None if as_operator else self._claim_handle(author, handle_token)
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
            entry = self._board_entries(only_seq=seq)[0]
        if not as_operator:
            self._on_event("board.posted", seq=seq, handle=author, preview=content)
        return entry, new_handle_token

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

    @staticmethod
    def _mail_view(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in ("id", "created_at", "sender", "kind", "subject", "body", "in_reply_to")} | {
            "to": row["recipient"]
        }

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
            " in_reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, sender, sender_key, recipient["handle"], recipient["skeleton"], kind, subject, body, in_reply_to),
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
    ) -> tuple[dict[str, Any], str | None]:
        """Send a message from one handle to another. Registers `sender` if it is new."""
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
            view = self._deliver(sender_name, sender_key, recipient, kind, subject, body, in_reply_to, limits)
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
            self._deliver(
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
                f"'{recipient['handle']}'. You can look it up in the directory and send it a direct message."
            )
            self._db.execute(
                "INSERT INTO request_messages (request_id, sender, created_at, body) VALUES (?, 'operator', ?, ?)",
                (req_id, now(), reply[:8000]),
            )
            self._db.execute("UPDATE requests SET status = 'answered' WHERE id = ?", (req_id,))
            row = self._db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            return self._request_view(row)
