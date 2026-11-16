#!/usr/bin/env python3
"""Add an audit log for local NAC-BBS administration."""

import sqlite3
import sys
from pathlib import Path


def migrate(db_path):
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_key TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}'
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_audit_time
                ON admin_audit_log(created_at)
            """)

        result = conn.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if result != "ok":
            raise RuntimeError(f"Integrity check failed: {result}")

        print("Audit migration complete.")
        print("Database integrity:", result)
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(
            "Usage: python3 migrations/002_admin_audit.py DATABASE"
        )

    db_path = Path(sys.argv[1])

    if not db_path.is_file():
        sys.exit(f"Database not found: {db_path}")

    migrate(db_path)
