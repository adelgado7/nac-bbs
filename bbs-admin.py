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



def connect_write(db_path):
    """Open an existing SQLite database for authorized local writes."""
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=rw",
        uri=True,
        timeout=10,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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



def show_user(conn, identifier):
    """Display a registered user's details without modifying data."""

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)")
    }

    if "email" not in columns:
        print("This database has not received migration 001.")
        return

    matches = conn.execute(
        """
        SELECT *
        FROM users
        WHERE public_key = ?
           OR display_name = ? COLLATE NOCASE
        """,
        (identifier, identifier),
    ).fetchall()

    if not matches:
        print("User not found.")
        return

    user = matches[0]

    print("\nNAC-BBS USER RECORD")
    print("-" * 50)

    fields = [
        ("Display name", "display_name"),
        ("Public key", "public_key"),
        ("Advertised name", "advertised_name"),
        ("Registered", "created_at"),
        ("Last seen", "last_seen_at"),
        ("Plan", "plan"),
        ("Email", "email"),
        ("Mobile phone", "mobile_phone"),
        ("Contact consent", "contact_consent"),
        ("Contact updated", "contact_updated_at"),
    ]

    for label, field in fields:
        value = user[field]
        if field == "contact_consent":
            value = "Yes" if value else "No"
        print(f"{label:18}: {value if value is not None else '-'}")

    role_table = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = 'admin_roles'
        """
    ).fetchone()

    if role_table:
        role = conn.execute(
            "SELECT role FROM admin_roles WHERE public_key = ?",
            (user["public_key"],),
        ).fetchone()

        print(f"{'BBS role':18}: {role['role'] if role else 'user'}")

    print()


def update_contact(db_path, identifier):
    """Update a user's contact information with consent."""

    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("BEGIN IMMEDIATE")

        user = conn.execute(
            """
            SELECT * FROM users
            WHERE public_key = ?
               OR display_name = ? COLLATE NOCASE
            """,
            (identifier, identifier),
        ).fetchone()

        if not user:
            print("User not found.")
            return

        print(f"\\nUpdating contact: {user['display_name']}")
        print("Press Enter to retain an existing value.")

        email = input(
            f"Email [{user['email'] or '-'}]: "
        ).strip() or user["email"]

        mobile = input(
            f"Mobile [{user['mobile_phone'] or '-'}]: "
        ).strip() or user["mobile_phone"]

        consent = input(
            "Has the user explicitly agreed to save "
            "this contact information? (yes/no): "
        ).strip().lower()

        if consent != "yes":
            print("Cancelled. No contact details saved.")
            return

        print(f"\\nEmail:  {email or '-'}")
        print(f"Mobile: {mobile or '-'}")

        if input("Type SAVE to confirm: ").strip() != "SAVE":
            print("Cancelled.")
            return

        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            UPDATE users
            SET email = ?,
                mobile_phone = ?,
                contact_consent = 1,
                contact_updated_at = ?
            WHERE public_key = ?
            """,
            (email, mobile, now, user["public_key"]),
        )

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
                "update_contact",
                user["public_key"],
                json.dumps({
                    "email_changed": email != user["email"],
                    "mobile_changed": mobile != user["mobile_phone"],
                    "consent": True,
                }),
            ),
        )

        conn.commit()
        print("Contact information saved and audited.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



VALID_ROLES = ("superadmin", "admin", "sysop")


def grant_role(db_path, public_key, role):
    """Assign or change a BBS administrator role."""

    if role not in VALID_ROLES:
        raise SystemExit(f"Invalid role: {role}")

    conn = connect_write(db_path)

    try:
        conn.execute("BEGIN IMMEDIATE")

        user = conn.execute(
            "SELECT display_name FROM users WHERE public_key = ?",
            (public_key,)
        ).fetchone()

        if not user:
            print("User not found. Full public key required.")
            return

        existing = conn.execute(
            "SELECT role FROM admin_roles WHERE public_key = ?",
            (public_key,)
        ).fetchone()

        old_role = existing["role"] if existing else None

        if old_role == role:
            print(f"User already has role: {role}")
            return

        if old_role == "superadmin" and role != "superadmin":
            count = conn.execute(
                "SELECT COUNT(*) FROM admin_roles WHERE role = 'superadmin'"
            ).fetchone()[0]

            if count <= 1:
                print("Cannot demote the last super-admin.")
                return

        print(f"User: {user['display_name']}")
        print(f"Public key: {public_key}")
        print(f"Current role: {old_role or 'user'}")
        print(f"New role: {role}")

        if input("Type GRANT to confirm: ").strip() != "GRANT":
            print("Cancelled.")
            return

        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO admin_roles
                (public_key, role, granted_at, granted_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(public_key) DO UPDATE SET
                role = excluded.role,
                granted_at = excluded.granted_at,
                granted_by = excluded.granted_by
            """,
            (public_key, role, now, getpass.getuser())
        )

        conn.execute(
            """
            INSERT INTO admin_audit_log
                (created_at, actor, action, target_key, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                now,
                getpass.getuser(),
                "grant_role",
                public_key,
                json.dumps({
                    "old_role": old_role,
                    "new_role": role
                })
            )
        )

        conn.commit()
        print("Role saved and audited.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revoke_role(db_path, public_key):
    """Remove an administrator role, retaining normal BBS access."""

    conn = connect_write(db_path)

    try:
        conn.execute("BEGIN IMMEDIATE")

        user = conn.execute(
            """
            SELECT u.display_name, a.role
            FROM users u
            JOIN admin_roles a ON a.public_key = u.public_key
            WHERE u.public_key = ?
            """,
            (public_key,)
        ).fetchone()

        if not user:
            print("No administrator role found for that public key.")
            return

        if user["role"] == "superadmin":
            count = conn.execute(
                "SELECT COUNT(*) FROM admin_roles WHERE role = 'superadmin'"
            ).fetchone()[0]

            if count <= 1:
                print("Cannot revoke the last super-admin.")
                return

        print(f"User: {user['display_name']}")
        print(f"Public key: {public_key}")
        print(f"Current role: {user['role']}")

        if input("Type REVOKE to confirm: ").strip() != "REVOKE":
            print("Cancelled.")
            return

        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "DELETE FROM admin_roles WHERE public_key = ?",
            (public_key,)
        )

        conn.execute(
            """
            INSERT INTO admin_audit_log
                (created_at, actor, action, target_key, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                now,
                getpass.getuser(),
                "revoke_role",
                public_key,
                json.dumps({"old_role": user["role"]})
            )
        )

        conn.commit()
        print("Role revoked and audited.")

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

    user_parser = subparsers.add_parser(
        "user", help="Display a registered user's details"
    )
    user_parser.add_argument("identifier")

    contact_parser = subparsers.add_parser(
        "contact", help="Update a user's contact information"
    )
    contact_parser.add_argument("identifier")

    grant_parser = subparsers.add_parser(
        "grant-role", help="Assign or change a BBS administrator role"
    )
    grant_parser.add_argument("public_key")
    grant_parser.add_argument("role", choices=VALID_ROLES)

    revoke_parser = subparsers.add_parser(
        "revoke-role", help="Remove a BBS administrator role"
    )
    revoke_parser.add_argument("public_key")

    rename_parser = subparsers.add_parser(
        "rename", help="Rename a registered user"
    )
    rename_parser.add_argument("public_key")
    rename_parser.add_argument("new_name")

    args = parser.parse_args()

    if args.command == "grant-role":
        grant_role(args.db, args.public_key, args.role)
        return

    if args.command == "revoke-role":
        revoke_role(args.db, args.public_key)
        return

    if args.command == "rename":
        rename_user(args.db, args.public_key, args.new_name)
        return

    if args.command == "contact":
        update_contact(args.db, args.identifier)
        return

    with connect(args.db) as conn:
        if args.command == "users":
            list_users(conn)
        elif args.command == "find":
            list_users(conn, args.name)
        elif args.command == "user":
            show_user(conn, args.identifier)


if __name__ == "__main__":
    main()
