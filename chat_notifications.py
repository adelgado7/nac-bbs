"""Queued, private MeshCore chat notices for newly unread BBSMail."""
from datetime import datetime, timedelta
import sqlite3

import email_notifications

POLL_SECONDS = 5
KIND_CHAT = "chat"
NOTICE_TEXT = "You have new BBSMail at NAC-BBS. Send NEW to check your inbox."


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_key TEXT NOT NULL,
            event_key TEXT NOT NULL UNIQUE,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            sent_at TEXT,
            last_error TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_outbox_pending
        ON chat_notification_outbox(status, next_attempt_at, id)
    """)


def enqueue_unread_notice(conn, mail_id, recipient_key, created_at):
    """Queue a notice only if global chat notifications are enabled."""
    if not email_notifications.is_enabled(conn, KIND_CHAT):
        return False
    conn.execute("""
        INSERT INTO chat_notification_outbox(
            recipient_key, event_key, message, created_at, next_attempt_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO NOTHING
    """, (
        recipient_key,
        f"unread-mail:{recipient_key}:{mail_id}",
        NOTICE_TEXT,
        created_at,
        created_at,
    ))
    return True


def next_pending(db_file):
    """Return the oldest due notification, suppressing pending notices if off."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        if not email_notifications.is_enabled(conn, KIND_CHAT):
            with conn:
                conn.execute("""
                    UPDATE chat_notification_outbox
                    SET status='suppressed', last_error='disabled before delivery'
                    WHERE status='pending'
                """)
            return None

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        row = conn.execute("""
            SELECT id, recipient_key, message, attempts
            FROM chat_notification_outbox
            WHERE status='pending' AND next_attempt_at <= ?
            ORDER BY id
            LIMIT 1
        """, (now,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def is_enabled(db_file):
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        return email_notifications.is_enabled(conn, KIND_CHAT)
    finally:
        conn.close()


def mark_suppressed(db_file, notification_id):
    conn = sqlite3.connect(db_file)
    try:
        with conn:
            conn.execute("""
                UPDATE chat_notification_outbox
                SET status='suppressed', last_error='disabled before delivery'
                WHERE id=? AND status='pending'
            """, (notification_id,))
    finally:
        conn.close()


def mark_sent(db_file, notification_id):
    conn = sqlite3.connect(db_file)
    try:
        sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with conn:
            conn.execute("""
                UPDATE chat_notification_outbox
                SET status='sent', sent_at=?, attempts=attempts+1, last_error=NULL
                WHERE id=? AND status='pending'
            """, (sent_at, notification_id))
    finally:
        conn.close()


def mark_failed(db_file, notification_id, attempts, error):
    conn = sqlite3.connect(db_file)
    try:
        delay_seconds = min(60 * (2 ** min(attempts, 8)), 6 * 60 * 60)
        next_attempt = (
            datetime.now().astimezone() + timedelta(seconds=delay_seconds)
        ).isoformat(timespec="seconds")
        with conn:
            conn.execute("""
                UPDATE chat_notification_outbox
                SET attempts=?, next_attempt_at=?, last_error=?
                WHERE id=? AND status='pending'
            """, (attempts, next_attempt, str(error)[:500], notification_id))
    finally:
        conn.close()
