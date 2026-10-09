"""Operator maintenance commands, run inside the container: `python -m agent_helper.maintenance <command>`.

new-instance-id      Give this database a new instance id, challenge key and signing key
                     (docs/decisions/0017, 0022). Meant
                     for staging or test copies of a production database, so that signatures made for
                     production cannot be replayed on the copy. Every signature stored in this database
                     then shows as invalid here. The old id is kept and printed.
                     With --revoke-old-key the old signing key is marked revoked instead of
                     rotated: its checkpoints then count neither as proof nor as counter-proof.
restore-instance-id  Undo new-instance-id: swap back to the previous id and challenge key.
revoke-key           Mark an earlier board-checkpoint signing key as revoked (--key-id, confirmed with
                     --confirm <the same key id>), for example after a backup holding it leaked. Takes
                     effect when the service starts next. The current key cannot be revoked this way;
                     move to a new key first (new-instance-id, or INSTANCE_SIGNING_KEY_FILE).

Both need the service stopped and the current instance id typed as confirmation (--confirm <id>).
Without it they only show the current id and how many signatures are affected.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import sys
from pathlib import Path

from . import keys
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


def _now() -> str:
    from .store import now

    return now()


def _registry(db: sqlite3.Connection) -> dict[str, dict[str, str]]:
    return json.loads(_meta(db, "previous_keys") or "{}")


def _set(db: sqlite3.Connection, name: str, value: str) -> None:
    db.execute(
        "INSERT INTO instance_meta (name, value) VALUES (?, ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value",
        (name, value),
    )


def _swap(
    db_path: Path,
    confirm: str,
    new_id: str,
    new_key: str,
    new_signing: str,
    *,
    need_previous: bool,
    revoke_old: bool = False,
) -> tuple[str, str]:
    db = _open(db_path)
    try:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MaintenanceError(LOCKED if "locked" in str(exc) else str(exc)) from None
        try:
            current, key = _meta(db, "instance_id"), _meta(db, "challenge_key")
            signing = _meta(db, "signing_key")
            if current is None or key is None:
                raise MaintenanceError("this database has no instance id yet; start the service once first")
            if confirm != current:
                raise MaintenanceError(f"confirmation does not match the current instance id {current}")
            registry = _registry(db)
            if need_previous:
                prev_id, prev_key = _meta(db, "previous_instance_id"), _meta(db, "previous_challenge_key")
                if prev_id is None or prev_key is None:
                    raise MaintenanceError("there is no previous instance id to restore")
                new_id, new_key = prev_id, prev_key
                new_signing = _meta(db, "previous_signing_key") or signing or ""
                restored_id = keys.key_id(keys.public_key_of(new_signing)) if new_signing else None
                if restored_id and registry.get(restored_id, {}).get("status") == "revoked":
                    raise MaintenanceError("the previous signing key is revoked; it cannot be restored")
                registry.pop(restored_id, None)
            pairs = [
                ("previous_instance_id", current),
                ("previous_challenge_key", key),
                ("instance_id", new_id),
                ("challenge_key", new_key),
            ]
            # The board checkpoint key moves with the id, so a copy cannot sign as the original instance.
            if new_signing:
                if signing and signing != new_signing:
                    pairs.append(("previous_signing_key", signing))
                    status = "revoked" if revoke_old else "rotated"
                    keys.add_previous_key(registry, signing, status, _now(), "replaced by maintenance")
                pairs.append(("signing_key", new_signing))
                registry.pop(keys.key_id(keys.public_key_of(new_signing)), None)
            pairs.append(("previous_keys", json.dumps(registry, sort_keys=True)))
            for name, value in pairs:
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


def new_instance_id(db_path: Path, confirm: str, revoke_old_key: bool = False) -> tuple[str, str]:
    """Replace the instance id and challenge key; the old ones are kept. Returns (old id, new id)."""
    return _swap(
        db_path,
        confirm,
        "ah-" + secrets.token_hex(16),
        secrets.token_hex(32),
        keys.new_private_key(),
        need_previous=False,
        revoke_old=revoke_old_key,
    )


def revoke_key(db_path: Path, key_id: str, confirm: str) -> None:
    """Mark an earlier signing key as revoked. The current key cannot be revoked."""
    if confirm != key_id:
        raise MaintenanceError("type the key id again with --confirm to revoke it")
    db = _open(db_path)
    try:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MaintenanceError(LOCKED if "locked" in str(exc) else str(exc)) from None
        try:
            current = _meta(db, "signing_key")
            if current and keys.key_id(keys.public_key_of(current)) == key_id:
                raise MaintenanceError("this is the current signing key; move to a new key first")
            registry = _registry(db)
            if key_id not in registry:
                known = ", ".join(sorted(registry)) or "none"
                raise MaintenanceError(f"no earlier signing key {key_id} (known: {known})")
            registry[key_id] |= {"status": "revoked", "since": _now(), "reason": "revoked by the operator"}
            _set(db, "previous_keys", json.dumps(registry, sort_keys=True))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    finally:
        db.close()


def restore_instance_id(db_path: Path, confirm: str) -> tuple[str, str]:
    """Swap back to the previous instance id and challenge key. Returns (old id, restored id)."""
    return _swap(db_path, confirm, "", "", "", need_previous=True)


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
        if name == "new-instance-id":
            cmd.add_argument("--revoke-old-key", action="store_true", help="mark the old signing key revoked")
    revoke = sub.add_parser("revoke-key", help="mark an earlier checkpoint signing key as revoked")
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--confirm", metavar="KEY_ID", help="the same key id, typed out")
    args = parser.parse_args(argv)
    db_path = Settings.from_env().db_path

    if args.command == "revoke-key":
        try:
            revoke_key(db_path, args.key_id, args.confirm or "")
        except MaintenanceError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Key {args.key_id} is revoked; restart the service for it to take effect.")
        return 0

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
            old, new = new_instance_id(db_path, args.confirm, args.revoke_old_key)
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
