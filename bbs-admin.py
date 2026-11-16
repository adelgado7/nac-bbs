#!/usr/bin/env python3
"""NAC-BBS local administration utility."""

import argparse
import getpass
import json
from datetime import datetime, timezone
import sqlite3
from pathlib import Path

DEFAULT_DB = Path.home() / "meshcore-bbs/bbs.db"


def connect(db_path):
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro",
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    return conn


def list_users(conn, search=None):
    sql = """
        SELECT public_key, display_name,
               advertised_name, created_at, last_seen_at
        FROM users
    """
    params = []

    if search:
        sql += " WHERE display_name LIKE ? OR advertised_name LIKE ?"
        params = [f"%{search}%", f"%{search}%"]

    sql += " ORDER BY display_name COLLATE NOCASE"

    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("No matching users.")
        return

    print(f"\n{'NAME':24} {'PUBLIC KEY':14} {'LAST SEEN'}")
    print("-" * 65)

    for row in rows:
        print(
            f"{row['display_name'][:24]:24} "
            f"{row['public_key'][:12]:14} "
            f"{row['last_seen_at']}"
        )

    print(f"\n{len(rows)} user(s) found.")



def rename_user(db_path, public_key, new_name):
    """Rename a user and record the change atomically."""

    new_name = new_name.strip()

    if not new_name or len(new_name) > 64:
        raise SystemExit("Name must contain 1-64 characters.")

    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("BEGIN IMMEDIATE")

        matches = conn.execute(
            "SELECT public_key, display_name FROM users "
            "WHERE public_key = ?",
            (public_key,),
        ).fetchall()

        if len(matches) != 1:
            raise SystemExit(
                "User not found. Supply the complete public key."
            )

        user = matches[0]
        old_name = user["display_name"]

        if old_name == new_name:
            print("Name is already correct. No change made.")
            return

        print(f"Current name: {old_name}")
        print(f"New name:     {new_name}")
        print(f"Public key:   {public_key}")

        confirmation = input(
            "\\nType RENAME to confirm: "
        ).strip()

        if confirmation != "RENAME":
            print("Cancelled. No changes made.")
            return

        conn.execute(
            "UPDATE users SET display_name = ? "
            "WHERE public_key = ?",
            (new_name, public_key),
        )

        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO admin_audit_log
                (created_at, actor, action,
                 target_key, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                now,
                getpass.getuser(),
                "rename_user",
                public_key,
                json.dumps({
                    "old_name": old_name,
                    "new_name": new_name,
                }),
            ),
        )

        conn.commit()
        print("User renamed and audit entry recorded.")

    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise SystemExit(
            f"Rename rejected (possibly duplicate name): {exc}"
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="NAC-BBS administration utility"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="SQLite database path",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    subparsers.add_parser("users", help="List registered users")

    search_parser = subparsers.add_parser(
        "find", help="Search registered users"
    )
    search_parser.add_argument("name")

    rename_parser = subparsers.add_parser(
        "rename", help="Rename a registered user"
    )
    rename_parser.add_argument("public_key")
    rename_parser.add_argument("new_name")

    args = parser.parse_args()

    if args.command == "rename":
        rename_user(args.db, args.public_key, args.new_name)
        return

    with connect(args.db) as conn:
        if args.command == "users":
            list_users(conn)
        elif args.command == "find":
            list_users(conn, args.name)


if __name__ == "__main__":
    main()
