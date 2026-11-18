#!/usr/bin/env python3
"""NAC-BBS local administration utility."""

import argparse
import getpass
import json
from datetime import datetime, timezone
import sqlite3
from pathlib import Path

import boards_v1

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




def withdraw_consent(db_path, public_key):
    """Withdraw contact consent and clear stored contact details atomically."""
    conn = connect_write(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            """SELECT display_name, email, mobile_phone, contact_consent
               FROM users WHERE public_key = ?""",
            (public_key,),
        ).fetchone()
        if user is None:
            print("User not found. Supply the complete public key.")
            return
        if not (user["email"] or user["mobile_phone"] or user["contact_consent"]):
            print("No stored contact details or active consent. No change made.")
            return

        print(f"User: {user['display_name']}")
        print(f"Public key: {public_key}")
        print("This will clear email and mobile phone and withdraw consent.")
        if input("Type WITHDRAW to confirm: ").strip() != "WITHDRAW":
            print("Cancelled. No changes made.")
            return

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE users SET email = NULL, mobile_phone = NULL,
                      contact_consent = 0, contact_updated_at = ?
               WHERE public_key = ?""",
            (now, public_key),
        )
        conn.execute(
            """INSERT INTO admin_audit_log
                   (created_at, actor, action, target_key, details_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                now, getpass.getuser(), "withdraw_contact_consent",
                public_key,
                json.dumps({
                    "email_cleared": bool(user["email"]),
                    "mobile_cleared": bool(user["mobile_phone"]),
                    "consent_withdrawn": bool(user["contact_consent"]),
                }),
            ),
        )
        conn.commit()
        print("Consent withdrawn; contact details cleared and action audited.")
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


def board_admin(db_path, action, name=None, description="",
                author=None, message="", page=1, reason=""):
    """Manage public boards from the Pi's local terminal."""
    conn = connect_write(db_path)
    try:
        boards_v1.ensure_schema(conn)
        if action == "list":
            rows = conn.execute("""SELECT name, description, status, created_at
                FROM boards ORDER BY name COLLATE NOCASE""").fetchall()
            if not rows:
                print("No boards have been created.")
                return
            print(f"\n{'NAME':24} {'STATUS':10} DESCRIPTION")
            print("-" * 72)
            for row in rows:
                print(f"{row['name'][:24]:24} {row['status']:10} {row['description']}")
            return

        if action == "create":
            board_id, reply = boards_v1.create_board(
                conn, name, description, created_by=None
            )
            print(reply)
            if board_id is not None:
                boards_v1.audit(conn, getpass.getuser(), "create_board",
                                str(board_id), name)
            return

        if action == "post":
            user = conn.execute("""SELECT public_key, display_name FROM users
                WHERE display_name = ? COLLATE NOCASE OR public_key = ?""",
                (author, author)).fetchone()
            if user is None:
                print("Author not found. Use a registered BBS name or full public key.")
                return
            reply = boards_v1.post_message(
                conn, user["public_key"], name, message
            )
            print(f"As {user['display_name']}: {reply}")
            return

        if action == "posts":
            print(boards_v1.list_posts_for_moderation(conn, page))
            return

        if action in {"hide", "restore"}:
            try:
                post_id = int(name)
            except (TypeError, ValueError):
                print("Post ID must be a number.")
                return
            changed, reply = boards_v1.moderate_post(
                conn, getpass.getuser(), post_id, action, reason
            )
            print(reply)
            return

        status = "archived" if action == "archive" else "active"
        changed, reply = boards_v1.set_board_status(conn, name, status)
        print(reply)
        if changed:
            board = boards_v1.find_board(conn, name)
            boards_v1.audit(conn, getpass.getuser(), f"{action}_board",
                            str(board["id"]), board["name"])
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

    subparsers.add_parser(
        "roles", help="List assigned BBS administrator roles"
    )

    contact_parser = subparsers.add_parser(
        "contact", help="Update a user's contact information"
    )
    contact_parser.add_argument("identifier")

    withdraw_parser = subparsers.add_parser(
        "withdraw-consent", help="Clear contact details and withdraw consent"
    )
    withdraw_parser.add_argument("public_key")

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

    board_parser = subparsers.add_parser(
        "board", help="List, create, archive, or restore public boards"
    )
    board_actions = board_parser.add_subparsers(
        dest="board_action", required=True
    )
    board_actions.add_parser("list", help="List boards and their status")
    posts_parser = board_actions.add_parser(
        "posts", help="List public posts for moderation"
    )
    posts_parser.add_argument("--page", type=int, default=1)
    create_board_parser = board_actions.add_parser(
        "create", help="Create a public board"
    )
    create_board_parser.add_argument("name")
    create_board_parser.add_argument("--description", default="")
    post_board_parser = board_actions.add_parser(
        "post", help="Post to an active public board as a registered user"
    )
    post_board_parser.add_argument("name", help="Board name")
    post_board_parser.add_argument(
        "--author", required=True,
        help="Registered BBS display name or complete public key",
    )
    post_board_parser.add_argument(
        "--message", required=True, help="Public post text"
    )
    for action in ("hide-post", "restore-post"):
        mod_parser = board_actions.add_parser(
            action, help=f"{action.replace('-', ' ').capitalize()} a public post"
        )
        mod_parser.add_argument("post_id", help="Public post ID")
        if action == "hide-post":
            mod_parser.add_argument("--reason", default="",
                                    help="Optional moderation reason")
    for action in ("archive", "restore"):
        action_parser = board_actions.add_parser(
            action, help=f"{action.capitalize()} a board without deleting its posts"
        )
        action_parser.add_argument("name")

    args = parser.parse_args()

    if args.command == "withdraw-consent":
        withdraw_consent(args.db, args.public_key)
        return

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

    if args.command == "board":
        if args.board_action == "list":
            board_admin(args.db, "list")
        elif args.board_action == "create":
            board_admin(args.db, "create", args.name, args.description)
        elif args.board_action == "post":
            board_admin(args.db, "post", args.name,
                        author=args.author, message=args.message)
        elif args.board_action == "posts":
            board_admin(args.db, "posts", page=args.page)
        elif args.board_action in {"hide-post", "restore-post"}:
            action = "hide" if args.board_action == "hide-post" else "restore"
            board_admin(args.db, action, args.post_id,
                        reason=getattr(args, "reason", ""))
        else:
            board_admin(args.db, args.board_action, args.name)
        return

    with connect(args.db) as conn:
        if args.command == "users":
            list_users(conn)
        elif args.command == "roles":
            role_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_roles'"
            ).fetchone()
            if not role_table:
                print("Admin roles are not installed. Run migrations/001_admin_support.py.")
            else:
                rows = conn.execute("""SELECT u.display_name, a.role, a.granted_at
                    FROM admin_roles a JOIN users u ON u.public_key = a.public_key
                    ORDER BY CASE a.role WHEN 'superadmin' THEN 0
                        WHEN 'admin' THEN 1 ELSE 2 END,
                        u.display_name COLLATE NOCASE""").fetchall()
                if not rows:
                    print("No administrator roles are assigned.")
                else:
                    print(f"\n{'NAME':24} {'ROLE':12} GRANTED")
                    print("-" * 60)
                    for row in rows:
                        print(f"{row['display_name'][:24]:24} {row['role']:12} {row['granted_at']}")
        elif args.command == "find":
            list_users(conn, args.name)
        elif args.command == "user":
            show_user(conn, args.identifier)


if __name__ == "__main__":
    main()
