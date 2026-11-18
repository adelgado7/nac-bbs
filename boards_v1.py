"""Public bulletin-board helpers shared by radio commands and local admin."""

import re
import sqlite3
from datetime import datetime

BOARD_NAME_MAX = 32
BOARD_DESCRIPTION_MAX = 100
BOARD_POST_MAX = 300
BOARD_LIST_PAGE_SIZE = 3
BOARD_PREVIEW_CHARS = 35


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_schema(conn):
    """Create the board and audit tables, safe to call at every startup."""
    conn.execute("""CREATE TABLE IF NOT EXISTS boards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'archived')),
        created_at TEXT NOT NULL,
        created_by TEXT,
        updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS board_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        board_id INTEGER NOT NULL,
        author_key TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (board_id) REFERENCES boards(id),
        FOREIGN KEY (author_key) REFERENCES users(public_key)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_board_posts_recent
        ON board_posts(board_id, id DESC)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target_key TEXT,
        details_json TEXT NOT NULL DEFAULT '{}'
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_admin_audit_time
        ON admin_audit_log(created_at)""")


def validate_board(name, description=""):
    name = " ".join(name.strip().split())
    description = " ".join(description.strip().split())
    if not name or len(name) > BOARD_NAME_MAX:
        return None, None, f"Board name must be 1-{BOARD_NAME_MAX} characters."
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", name):
        return None, None, "Use letters, numbers, spaces, hyphens, or underscores."
    if len(description) > BOARD_DESCRIPTION_MAX:
        return None, None, f"Description must be {BOARD_DESCRIPTION_MAX} characters or less."
    return name, description, None


def find_board(conn, name, active_only=False):
    sql = "SELECT * FROM boards WHERE name = ? COLLATE NOCASE"
    if active_only:
        sql += " AND status = 'active'"
    return conn.execute(sql, (name.strip(),)).fetchone()


def create_board(conn, name, description="", created_by=None):
    name, description, error = validate_board(name, description)
    if error:
        return None, error
    if find_board(conn, name):
        return None, "A board with that name already exists."
    stamp = now()
    try:
        with conn:
            cursor = conn.execute("""INSERT INTO boards
                (name, description, created_at, created_by, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (name, description, stamp, created_by, stamp))
        return cursor.lastrowid, f"Board created: {name}."
    except sqlite3.IntegrityError:
        return None, "A board with that name already exists."


def set_board_status(conn, name, status):
    if status not in ("active", "archived"):
        return False, "Invalid board status."
    board = find_board(conn, name)
    if board is None:
        return False, "Board not found."
    if board["status"] == status:
        return False, f"Board is already {status}."
    with conn:
        conn.execute("UPDATE boards SET status = ?, updated_at = ? WHERE id = ?",
                     (status, now(), board["id"]))
    return True, f"Board {board['name']} {status}."


def list_boards(conn, page=1, include_archived=False):
    if page < 1:
        return "Use BOARDS [page]."
    where = "" if include_archived else "WHERE status = 'active'"
    total = conn.execute(f"SELECT COUNT(*) FROM boards {where}").fetchone()[0]
    pages = max(1, (total + BOARD_LIST_PAGE_SIZE - 1) // BOARD_LIST_PAGE_SIZE)
    if page > pages:
        if total == 0:
            return "No boards have been created."
        return f"Boards: {pages} page(s). Use BOARDS 1-{pages}."
    rows = conn.execute(f"""SELECT name, status FROM boards {where}
        ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?""",
        (BOARD_LIST_PAGE_SIZE, (page - 1) * BOARD_LIST_PAGE_SIZE)).fetchall()
    if not rows:
        return "No active boards yet."
    lines = [f"Boards {page}/{pages} ({total} total):"]
    lines.extend(f"{r['name']}" + (" (archived)" if r["status"] != "active" else "")
                 for r in rows)
    if page < pages:
        lines.append(f"More: BOARDS {page + 1}")
    return radio_limit("\n".join(lines))


def post_message(conn, author_key, board_name, body):
    board_name = board_name.strip()
    body = body.strip()
    if not body or len(body) > BOARD_POST_MAX:
        return f"Post must be 1-{BOARD_POST_MAX} characters."
    board = find_board(conn, board_name, active_only=True)
    if board is None:
        return "Active board not found. Send BOARDS to list boards."
    author = conn.execute("SELECT 1 FROM users WHERE public_key = ?",
                          (author_key,)).fetchone()
    if author is None:
        return "Register first with REGISTER <name>."
    with conn:
        conn.execute("""INSERT INTO board_posts
            (board_id, author_key, body, created_at) VALUES (?, ?, ?, ?)""",
            (board["id"], author_key, body, now()))
    return f"Posted to {board['name']}."


def board_preview(conn, board_name, page=1):
    board = find_board(conn, board_name, active_only=True)
    if board is None:
        return "Active board not found. Send BOARDS to list boards."
    total = conn.execute("SELECT COUNT(*) FROM board_posts WHERE board_id = ?",
                         (board["id"],)).fetchone()[0]
    pages = max(1, total)
    if page < 1 or page > pages:
        return f"Posts: {pages}. Use BOARD {board['name']} 1-{pages}."
    row = conn.execute("""SELECT p.id, p.body, p.created_at, u.display_name
        FROM board_posts p JOIN users u ON u.public_key = p.author_key
        WHERE p.board_id = ? ORDER BY p.id DESC LIMIT 1 OFFSET ?""",
        (board["id"], page - 1)).fetchone()
    lines = [f"{board['name']} post {page}/{pages}"]
    if row is None:
        lines.append("No posts yet. POST <board>|<message>")
    else:
        body = row["body"]
        if len(body) > BOARD_PREVIEW_CHARS:
            body = body[:BOARD_PREVIEW_CHARS - 1] + "…"
        lines.append(f"#{row['id']} {row['display_name']}: {body}")
        lines.append(f"BREAD {row['id']} for full post")
    return radio_limit("\n".join(lines))


def read_post(conn, post_id, part=1):
    row = conn.execute("""SELECT p.id, p.body, u.display_name
        FROM board_posts p JOIN users u ON u.public_key = p.author_key
        WHERE p.id = ?""", (post_id,)).fetchone()
    if row is None:
        return "Board post not found."
    header = f"#{row['id']} {row['display_name']}: "
    budget = 140 - len(header.encode("utf-8")) - 24
    chunks = _chunks_utf8(row["body"], budget)
    if part < 1 or part > len(chunks):
        return f"Post {post_id} has {len(chunks)} part(s). Use BREAD {post_id} 1."
    suffix = f" ({part}/{len(chunks)})"
    if part < len(chunks):
        suffix += f" BREAD {post_id} {part + 1}"
    return radio_limit(header + chunks[part - 1] + suffix)


def _chunks_utf8(text, budget):
    chunks = []
    chunk = ""
    for char in text:
        if len((chunk + char).encode("utf-8")) > budget:
            if chunk:
                chunks.append(chunk)
            chunk = char
        else:
            chunk += char
    if chunk:
        chunks.append(chunk)
    return chunks or [""]


def radio_limit(text, budget=140):
    """Keep a reply within the conservative radio payload budget."""
    result = ""
    for char in text:
        if len((result + char).encode("utf-8")) > budget:
            return result.rstrip() + "…"
        result += char
    return result


def audit(conn, actor, action, target, details=""):
    import json
    with conn:
        conn.execute("""INSERT INTO admin_audit_log
            (created_at, actor, action, target_key, details_json)
            VALUES (?, ?, ?, ?, ?)""",
            (now(), actor, action, target, json.dumps({"details": details})))
