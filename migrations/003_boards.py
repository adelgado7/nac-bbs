#!/usr/bin/env python3
"""Create public NAC-BBS boards and their audit support."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boards_v1


def migrate(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            boards_v1.ensure_schema(conn)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Integrity check failed: {result}")
        print("Boards migration complete.")
        print("Database integrity:", result)
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 migrations/003_boards.py DATABASE")
    db_path = Path(sys.argv[1])
    if not db_path.is_file():
        sys.exit(f"Database not found: {db_path}")
    migrate(db_path)
