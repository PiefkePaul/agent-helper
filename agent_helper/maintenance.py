"""Operator maintenance commands, run inside the container: `python -m agent_helper.maintenance <command>`.

new-instance-id      Give this database a new instance id and challenge key (docs/decisions/0017). Meant
                     for staging or test copies of a production database, so that signatures made for
                     production cannot be replayed on the copy. Every signature stored in this database
                     then shows as invalid here. The old id is kept and printed.
restore-instance-id  Undo new-instance-id: swap back to the previous id and challenge key.

Both need the service stopped and the current instance id typed as confirmation (--confirm <id>).
Without it they only show the current id and how many signatures are affected.
"""

from __future__ import annotations

import argparse
import secrets
import sqlite3
import sys
from pathlib import Path

from .config import Settings

LOCKED = "Database locked, stop the service first."


class MaintenanceError(Exception):
    pass


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise MaintenanceError(f"no database at {db_path}")
    db = sqlite3.connect(db_path, timeout=1, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def _meta(db: sqlite3.Connection, name: str) -> str | None:
    try:
        row = db.execute("SELECT value FROM instance_meta WHERE name = ?", (name,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc):
            raise MaintenanceError(LOCKED) from None
        raise MaintenanceError("this database has no instance id yet; start the service once first") from None
    return row["value"] if row else None


def _count(db: sqlite3.Connection, sql: str) -> int:
    try:
        return db.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def summary(db_path: Path) -> dict[str, object]:
    """The current and previous instance id, and how many stored signatures depend on the id."""
    db = _open(db_path)
    try:
        current = _meta(db, "instance_id")
        if current is None:
            raise MaintenanceError("this database has no instance id yet; start the service once first")
        return {
            "instance_id": current,
            "previous_instance_id": _meta(db, "previous_instance_id"),
            "signed_board_entries": _count(db, "SELECT COUNT(*) FROM board_payloads WHERE signature IS NOT NULL"),
            "signed_messages": _count(db, "SELECT COUNT(*) FROM mail WHERE signature IS NOT NULL"),
            "registered_keys": _count(db, "SELECT COUNT(*) FROM handle_keys"),
        }
    finally:
        db.close()


def _swap(db_path: Path, confirm: str, new_id: str, new_key: str, *, need_previous: bool) -> tuple[str, str]:
    db = _open(db_path)
    try:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MaintenanceError(LOCKED if "locked" in str(exc) else str(exc)) from None
        try:
            current, key = _meta(db, "instance_id"), _meta(db, "challenge_key")
            if current is None or key is None:
                raise MaintenanceError("this database has no instance id yet; start the service once first")
            if confirm != current:
                raise MaintenanceError(f"confirmation does not match the current instance id {current}")
            if need_previous:
                prev_id, prev_key = _meta(db, "previous_instance_id"), _meta(db, "previous_challenge_key")
                if prev_id is None or prev_key is None:
                    raise MaintenanceError("there is no previous instance id to restore")
                new_id, new_key = prev_id, prev_key
            for name, value in (
                ("previous_instance_id", current),
                ("previous_challenge_key", key),
                ("instance_id", new_id),
                ("challenge_key", new_key),
            ):
                db.execute(
                    "INSERT INTO instance_meta (name, value) VALUES (?, ?)"
                    " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                    (name, value),
                )
            db.execute("DELETE FROM used_challenge_nonces")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return current, new_id
    except sqlite3.OperationalError as exc:
        raise MaintenanceError(LOCKED if "locked" in str(exc) else str(exc)) from None
    finally:
        db.close()


def new_instance_id(db_path: Path, confirm: str) -> tuple[str, str]:
    """Replace the instance id and challenge key; the old ones are kept. Returns (old id, new id)."""
    return _swap(db_path, confirm, "ah-" + secrets.token_hex(16), secrets.token_hex(32), need_previous=False)


def restore_instance_id(db_path: Path, confirm: str) -> tuple[str, str]:
    """Swap back to the previous instance id and challenge key. Returns (old id, restored id)."""
    return _swap(db_path, confirm, "", "", need_previous=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_helper.maintenance",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("new-instance-id", "give this database a new instance id (staging copies)"),
        ("restore-instance-id", "go back to the previous instance id"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--confirm", metavar="CURRENT_ID", help="the current instance id, typed out")
    args = parser.parse_args(argv)
    db_path = Settings.from_env().db_path

    try:
        info = summary(db_path)
        if not args.confirm:
            print(f"Current instance id:  {info['instance_id']}")
            print(f"Previous instance id: {info['previous_instance_id'] or '-'}")
            print(
                f"Signatures that depend on it: {info['signed_board_entries']} board entries, "
                f"{info['signed_messages']} messages ({info['registered_keys']} registered keys)."
            )
            print(f"Stop the service, then run again with --confirm {info['instance_id']}")
            return 2
        if args.command == "new-instance-id":
            old, new = new_instance_id(db_path, args.confirm)
            print(f"Instance id changed from {old} to {new}.")
            print("The old id is kept; undo with restore-instance-id. Start the service again.")
        else:
            old, new = restore_instance_id(db_path, args.confirm)
            print(f"Instance id restored from {old} to {new}. Start the service again.")
    except MaintenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
