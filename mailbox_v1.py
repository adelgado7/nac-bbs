"""Personal mailbox v1 commands. No radio access; operates on a supplied SQLite connection."""
from datetime import datetime
import sqlite3

PAGE_SIZE = 5
MAX_MESSAGE = 500


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_schema(conn):
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(mail)")}
    if "deleted_at" not in columns:
        conn.execute("ALTER TABLE mail ADD COLUMN deleted_at TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS mailbox_state (
        public_key TEXT PRIMARY KEY,
        last_read_id INTEGER,
        FOREIGN KEY(public_key) REFERENCES users(public_key)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_mail_visible
        ON mail(recipient_key, deleted_at, id)""")


def _page(value):
    try:
        n = int(value)
        return n if n > 0 else None
    except (ValueError, TypeError):
        return None


def _list(conn, key, page, unread=False, sent=False):
    where = "m.sender_key = ?" if sent else "m.recipient_key = ? AND m.deleted_at IS NULL"
    if unread:
        where += " AND m.read_at IS NULL"
    name_col = "recipient_key" if sent else "sender_key"
    count = conn.execute(f"SELECT COUNT(*) FROM mail m WHERE {where}", (key,)).fetchone()[0]
    rows = conn.execute(f"""SELECT m.id, m.read_at, u.display_name AS name
        FROM mail m JOIN users u ON u.public_key = m.{name_col}
        WHERE {where} ORDER BY m.id DESC LIMIT ? OFFSET ?""",
        (key, PAGE_SIZE, (page-1)*PAGE_SIZE)).fetchall()
    if not rows:
        return "No messages on that page."
    title = "Sent" if sent else ("Unread" if unread else "Inbox")
    lines = [f"{title}: {count} total; page {page}"]
    for r in rows:
        flag = "N" if r["read_at"] is None and not sent else " "
        lines.append(f"{r['id']} {flag} {r['name']}")
    if page * PAGE_SIZE < count:
        lines.append(f"Next: {'SENT' if sent else 'NEW' if unread else 'LIST'} {page+1}")
    return "\n".join(lines)


def _read(conn, key, msg_id=None):
    if msg_id is None:
        row = conn.execute("""SELECT m.id,m.message,m.created_at,u.display_name AS name
            FROM mail m JOIN users u ON u.public_key=m.sender_key
            WHERE m.recipient_key=? AND m.deleted_at IS NULL AND m.read_at IS NULL
            ORDER BY m.id LIMIT 1""", (key,)).fetchone()
    else:
        row = conn.execute("""SELECT m.id,m.message,m.created_at,u.display_name AS name
            FROM mail m JOIN users u ON u.public_key=m.sender_key
            WHERE m.recipient_key=? AND m.deleted_at IS NULL AND m.id=?""",
            (key,msg_id)).fetchone()
    if row is None:
        return "No unread mail." if msg_id is None else "Message not found in your inbox."
    with conn:
        conn.execute("UPDATE mail SET read_at=COALESCE(read_at,?) WHERE id=? AND recipient_key=?",
                     (now(),row["id"],key))
        conn.execute("""INSERT INTO mailbox_state(public_key,last_read_id) VALUES (?,?)
            ON CONFLICT(public_key) DO UPDATE SET last_read_id=excluded.last_read_id""",
            (key,row["id"]))
    return f"Msg {row['id']} from {row['name']} ({row['created_at']}): {row['message']}"


def _reply(conn, key, rest):
    parts = rest.split(None, 1)
    if not parts:
        return "Usage: REPLY <text> or REPLY <id> <text>"
    msg_id = None
    body = rest
    if parts[0].isdigit():
        msg_id = int(parts[0])
        if len(parts) < 2:
            return "Usage: REPLY <id> <text>"
        body = parts[1].strip()
    if msg_id is None:
        state = conn.execute("SELECT last_read_id FROM mailbox_state WHERE public_key=?",
                             (key,)).fetchone()
        msg_id = state["last_read_id"] if state else None
    if msg_id is None:
        return "Read a message first, or use REPLY <id> <text>."
    if not body or len(body) > MAX_MESSAGE:
        return "Reply must be 1 to 500 characters."
    with conn:
        row = conn.execute("""SELECT sender_key FROM mail
            WHERE id=? AND recipient_key=? AND deleted_at IS NULL""", (msg_id,key)).fetchone()
        if row is None:
            return "Message not found in your inbox."
        conn.execute("""INSERT INTO mail(sender_key,recipient_key,message,created_at)
            VALUES (?,?,?,?)""", (key,row["sender_key"],body,now()))
    return f"Reply saved to sender of message {msg_id}."


def _delete(conn, key, msg_id):
    with conn:
        cursor = conn.execute("""UPDATE mail SET deleted_at=?
            WHERE id=? AND recipient_key=? AND deleted_at IS NULL""", (now(),msg_id,key))
    return f"Message {msg_id} removed from your inbox." if cursor.rowcount else "Message not found in your inbox."


def handle(text, key, user, get_db):
    """Return None for unrelated commands; otherwise a radio-sized response."""
    parts = text.strip().split(None, 1)
    if not parts:
        return None
    cmd = parts[0].upper()
    aliases = {"L":"LIST","LN":"NEW","R":"READ","K":"DELETE"}
    cmd = aliases.get(cmd,cmd)
    supported = {"MAIL","LIST","NEW","READ","REPLY","SENT","DELETE","KM"}
    if cmd == "HELP" and len(parts)>1 and parts[1].strip().upper()=="MAIL":
        return ("MAIL counts; LIST [page]; NEW [page]; READ [id]; "
                "REPLY <text> or REPLY <id> <text>; SENT [page]; "
                "DELETE <id>; KM CONFIRM (delete read mail). "
                "Shortcuts: L LN R K.")
    if cmd not in supported:
        return None
    if not user:
        return "You are not registered. Use REGISTER <name> first."
    rest = parts[1].strip() if len(parts)>1 else ""
    conn = get_db()
    try:
        if cmd == "MAIL":
            if rest:
                return "Usage: MAIL"
            unread,total = conn.execute("""SELECT COUNT(*) FILTER (WHERE read_at IS NULL),
                COUNT(*) FROM mail WHERE recipient_key=? AND deleted_at IS NULL""",
                (key,)).fetchone()
            sent = conn.execute("SELECT COUNT(*) FROM mail WHERE sender_key=?", (key,)).fetchone()[0]
            return f"Mailbox for {user['display_name']}: {unread} unread, {total} total, {sent} sent."
        if cmd in ("LIST","NEW","SENT"):
            page = _page(rest or "1")
            if page is None:
                return f"Usage: {cmd} [page]"
            return _list(conn,key,page,unread=cmd=="NEW",sent=cmd=="SENT")
        if cmd == "READ":
            msg_id = _page(rest) if rest else None
            if rest and msg_id is None:
                return "Usage: READ [id]"
            return _read(conn,key,msg_id)
        if cmd == "REPLY":
            return _reply(conn,key,rest)
        if cmd == "DELETE":
            msg_id = _page(rest)
            return _delete(conn,key,msg_id) if msg_id else "Usage: DELETE <id>"
        if cmd == "KM":
            if rest.upper() != "CONFIRM":
                return "Delete all read inbox mail? Send KM CONFIRM."
            with conn:
                count = conn.execute("""UPDATE mail SET deleted_at=? WHERE recipient_key=?
                    AND read_at IS NOT NULL AND deleted_at IS NULL""",(now(),key)).rowcount
            return f"Removed {count} read messages from your inbox."
    finally:
        conn.close()
