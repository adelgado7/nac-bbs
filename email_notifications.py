"""Queued email notifications for NAC-BBS.

Email is sent through the Pi's existing local `mail` command (configured
through the system mail transport). Notification bodies contain metadata only;
BBSMail message text is never copied into this queue.
"""
import asyncio
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

DEFAULT_RECIPIENT = "delgado@apollos-dev.com"
POLL_SECONDS = 20
MAX_PER_PASS = 10
KIND_MAIL = "mail"
KIND_REGISTRATION = "registration"


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_settings (
            kind TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            updated_at TEXT NOT NULL,
            updated_by TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            event_key TEXT NOT NULL UNIQUE,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            sent_at TEXT,
            last_error TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_outbox_pending
        ON email_notification_outbox(status, next_attempt_at, id)
    """)


def is_enabled(conn, kind):
    row = conn.execute(
        "SELECT enabled FROM notification_settings WHERE kind = ?",
        (kind,),
    ).fetchone()
    # Both notifications start enabled. A superadmin can switch either off.
    return bool(row["enabled"]) if row else True


def set_enabled(conn, kind, enabled, updated_by):
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with conn:
        conn.execute("""
            INSERT INTO notification_settings(kind, enabled, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(kind) DO UPDATE SET
                enabled=excluded.enabled,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by
        """, (kind, int(bool(enabled)), now, updated_by))


def settings_status(conn):
    return {
        KIND_MAIL: is_enabled(conn, KIND_MAIL),
        KIND_REGISTRATION: is_enabled(conn, KIND_REGISTRATION),
    }


def _display_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError):
        return value


def enqueue_registration(conn, public_key, display_name, created_at):
    if not is_enabled(conn, KIND_REGISTRATION):
        return False
    safe_name = " ".join(display_name.replace("\r", " ").replace("\n", " ").split())
    timestamp = _display_time(created_at)
    conn.execute("""
        INSERT INTO email_notification_outbox(
            kind, event_key, subject, body, created_at, next_attempt_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO NOTHING
    """, (
        KIND_REGISTRATION,
        f"registration:{public_key}:{created_at}",
        "NAC-BBS: New user registration",
        f"{timestamp} {safe_name} just registered on NAC-BBS.",
        created_at,
        created_at,
    ))
    return True


def enqueue_mail_arrival(conn, mail_id, sender_name, created_at):
    if not is_enabled(conn, KIND_MAIL):
        return False
    safe_sender = " ".join(sender_name.replace("\r", " ").replace("\n", " ").split())
    timestamp = _display_time(created_at)
    conn.execute("""
        INSERT INTO email_notification_outbox(
            kind, event_key, subject, body, created_at, next_attempt_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key) DO NOTHING
    """, (
        KIND_MAIL,
        f"mail:{mail_id}",
        "NAC-BBS: New BBSMail for Andy Delgado",
        f"{timestamp} New BBSMail for Andy Delgado from {safe_sender} "
        f"(mailbox message #{mail_id}).",
        created_at,
        created_at,
    ))
    return True


def _record_failure(conn, row_id, attempts, error):
    delay_seconds = min(60 * (2 ** min(attempts, 8)), 6 * 60 * 60)
    next_attempt = (
        datetime.now().astimezone() + timedelta(seconds=delay_seconds)
    ).isoformat(timespec="seconds")
    with conn:
        conn.execute("""
            UPDATE email_notification_outbox
            SET status='pending', attempts=?, next_attempt_at=?, last_error=?
            WHERE id=? AND status='pending'
        """, (attempts, next_attempt, str(error)[:500], row_id))


def deliver_ready(db_file, limit=MAX_PER_PASS):
    """Send due items through the configured local mail transport."""
    mail_command = shutil.which("mail")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        rows = conn.execute("""
            SELECT id, kind, subject, body, attempts
            FROM email_notification_outbox
            WHERE status='pending' AND next_attempt_at <= ?
            ORDER BY id
            LIMIT ?
        """, (now, limit)).fetchall()

        for row in rows:
            if not is_enabled(conn, row["kind"]):
                with conn:
                    conn.execute("""
                        UPDATE email_notification_outbox
                        SET status='suppressed', last_error='disabled before delivery'
                        WHERE id=? AND status='pending'
                    """, (row["id"],))
                continue

            try:
                if not mail_command:
                    raise RuntimeError("mail command is not installed")
                recipient = os.environ.get(
                    "BBS_NOTIFICATION_EMAIL", DEFAULT_RECIPIENT
                ).strip()
                if not recipient or "\n" in recipient or "\r" in recipient:
                    raise RuntimeError("BBS_NOTIFICATION_EMAIL is invalid")

                result = subprocess.run(
                    [mail_command, "-s", row["subject"], recipient],
                    input=row["body"] + "\n",
                    text=True,
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
                if result.returncode != 0:
                    detail = result.stderr.strip() or (
                        f"mail exited with status {result.returncode}"
                    )
                    raise RuntimeError(detail)

                sent_at = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
                with conn:
                    conn.execute("""
                        UPDATE email_notification_outbox
                        SET status='sent', sent_at=?, attempts=attempts+1,
                            last_error=NULL
                        WHERE id=? AND status='pending'
                    """, (sent_at, row["id"]))
                logging.info("Email notification %s sent.", row["id"])
            except Exception as exc:
                attempts = row["attempts"] + 1
                _record_failure(conn, row["id"], attempts, exc)
                logging.warning(
                    "Email notification %s failed; will retry (%s).",
                    row["id"], type(exc).__name__,
                )
    finally:
        conn.close()


async def run_worker(db_file):
    """Periodically drain the durable outbox without blocking radio handling."""
    while True:
        try:
            await asyncio.to_thread(deliver_ready, Path(db_file))
        except Exception:
            logging.exception("Email notification queue pass failed.")
        await asyncio.sleep(POLL_SECONDS)
