"""Operator maintenance commands, run inside the container: `python -m agent_helper.maintenance <command>`.

new-instance-id   Give this database a new instance id and challenge key (docs/decisions/0017).
                  Meant for staging or test copies of a production database, so that signatures
                  made for production cannot be replayed on the copy. Every signature stored in
                  this database then shows as invalid here. Stop the service first.
"""

from __future__ import annotations

import argparse
import secrets
import sqlite3
import sys
from pathlib import Path

from .config import Settings


def new_instance_id(db_path: Path) -> str:
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")
    instance_id = "ah-" + secrets.token_hex(16)
    db = sqlite3.connect(db_path)
    try:
        with db:
            db.execute(
                "INSERT INTO instance_meta (name, value) VALUES ('instance_id', ?)"
                " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                (instance_id,),
            )
            db.execute(
                "INSERT INTO instance_meta (name, value) VALUES ('challenge_key', ?)"
                " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                (secrets.token_hex(32),),
            )
            db.execute("DELETE FROM used_challenge_nonces")
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"the database has no instance metadata yet; start the service once first ({exc})") from None
    finally:
        db.close()
    return instance_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_helper.maintenance",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    regen = sub.add_parser("new-instance-id", help="give this database a new instance id (staging copies)")
    regen.add_argument("--yes", action="store_true", help="confirm: all stored signatures become invalid here")
    args = parser.parse_args(argv)

    db_path = Settings.from_env().db_path
    if args.command == "new-instance-id":
        if not args.yes:
            print("This makes every signature stored in this database invalid on this copy. Re-run with --yes.")
            return 2
        print(f"new instance id: {new_instance_id(db_path)} (restart the service)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
