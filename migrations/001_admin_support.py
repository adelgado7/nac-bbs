#!/usr/bin/env python3
"""NAC-BBS administrator and support database migration."""

import sqlite3
import sys
from pathlib import Path


def migrate(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        with conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(users)")
            }

            new_columns = {
                "email": "TEXT",
                "mobile_phone": "TEXT",
                "contact_consent": "INTEGER NOT NULL DEFAULT 0",
                "contact_updated_at": "TEXT",
            }

            for name, definition in new_columns.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE users ADD COLUMN {name} {definition}"
                    )
                    print(f"Added users.{name}")

            # Execute statements individually so the migration
            # remains inside the existing transaction.
            schema_script = """
                CREATE TABLE IF NOT EXISTS admin_roles (
                    public_key TEXT PRIMARY KEY,
                    role TEXT NOT NULL
                        CHECK (role IN ('superadmin', 'admin', 'sysop')),
                    granted_at TEXT NOT NULL,
                    granted_by TEXT,
                    FOREIGN KEY (public_key)
                        REFERENCES users(public_key)
                );

                CREATE TABLE IF NOT EXISTS admin_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_key TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    FOREIGN KEY (sender_key)
                        REFERENCES users(public_key)
                );

                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_type TEXT NOT NULL
                        CHECK (ticket_type IN ('bug', 'feature')),
                    sender_key TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    email TEXT,
                    mobile_phone TEXT,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    assigned_to TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (sender_key)
                        REFERENCES users(public_key),
                    FOREIGN KEY (assigned_to)
                        REFERENCES users(public_key)
                );

                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    public_key TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    step TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS email_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient_email TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    sent_at TEXT,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tickets_status
                    ON support_tickets(status, created_at);

                CREATE INDEX IF NOT EXISTS idx_admin_messages_status
                    ON admin_messages(status, created_at);

                CREATE INDEX IF NOT EXISTS idx_email_queue_pending
                    ON email_queue(status, next_attempt_at);
            """
            for statement in schema_script.split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(statement)

        result = conn.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if result != "ok":
            raise RuntimeError(f"Integrity check failed: {result}")

        print("Migration complete.")
        print("Database integrity: ok")

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 migrations/001_admin_support.py DATABASE")

    db_path = Path(sys.argv[1])

    if not db_path.is_file():
        sys.exit(f"Database not found: {db_path}")

    migrate(db_path)
