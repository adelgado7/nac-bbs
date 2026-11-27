import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import mailbox_v1
import boards_v1
import email_notifications
import chat_notifications


DEVICE = (
    "/dev/serial/by-id/"
    "usb-RAKwireless_WisCore_RAK4631_Board_"
    "E0F5CFB4A54536EA-if00"
)

BASE_DIR = Path.home() / "meshcore-bbs"
DB_FILE = BASE_DIR / "bbs.db"
WEATHER_CACHE = BASE_DIR / "weather_cache.json"

# Courtesy advert behavior. When NAC-BBS hears a direct client advert,
# it answers with one zero-hop advert so the client also learns NAC-BBS.
# The per-client cooldown prevents advert ping-pong or repeated chatter.
COURTESY_ADVERT_COOLDOWN_SECONDS = 30 * 60
COURTESY_ADVERT_DELAY_SECONDS = 1.0
MESHCORE_CLIENT_TYPE = 1

# Community directory replies are kept within the radio payload budget.
DIR_REPLY_MAX_BYTES = 140
PLAN_MAX_LENGTH = 120


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------
# Database
# ---------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()

    # Existing incoming-message log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            sender_key TEXT NOT NULL,
            message TEXT NOT NULL,
            snr REAL
        )
    """)

    # Registered BBS users
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            public_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            advertised_name TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            plan TEXT
        )
    """)

    # Existing databases predate the optional .plan field. Add it in
    # place without disturbing registered users or mailbox data.
    user_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }

    if "plan" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT")

    # Persistent mailbox
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_key TEXT NOT NULL,
            recipient_key TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT,
            FOREIGN KEY (sender_key)
                REFERENCES users(public_key),
            FOREIGN KEY (recipient_key)
                REFERENCES users(public_key)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mail_recipient_unread
        ON mail(recipient_key, read_at)
    """)

    # Current MeshCore device roster. One row per public key.
    # Mailbox identity remains private in the users table and is joined
    # only when needed for local/admin reporting.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            public_key TEXT PRIMARY KEY,
            public_id TEXT NOT NULL UNIQUE,
            advertised_name TEXT,
            node_type INTEGER,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_event_type TEXT NOT NULL,
            last_snr REAL,
            last_advert_path_len INTEGER,
            sighting_count INTEGER NOT NULL DEFAULT 1
        )
    """)

    # Append-only observation history. This is intentionally metadata
    # only; message bodies are not copied into the sightings log.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            public_key TEXT NOT NULL,
            public_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            advertised_name TEXT,
            mailbox_name TEXT,
            node_type INTEGER,
            snr REAL,
            advert_path_len INTEGER,
            FOREIGN KEY (public_key)
                REFERENCES devices(public_key)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_devices_last_seen
        ON devices(last_seen_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sightings_observed_at
        ON sightings(observed_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sightings_public_key
        ON sightings(public_key, observed_at)
    """)

    mailbox_v1.ensure_schema(conn)
    boards_v1.ensure_schema(conn)
    email_notifications.ensure_schema(conn)
    chat_notifications.ensure_schema(conn)

    conn.commit()
    conn.close()


def store_incoming_message(sender_key, snr):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO messages (
            received_at,
            sender_key,
            message,
            snr
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            sender_key,
            "[REDACTED]",
            snr,
        ),
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------
# MeshCore device observation logger
# ---------------------------------------------------------

def public_node_id(public_key):
    """Return a stable non-raw identifier suitable for future exports."""
    return hashlib.sha256(
        public_key.encode("utf-8")
    ).hexdigest()[:12]


def record_device_sighting(
    public_key,
    event_type,
    advertised_name=None,
    node_type=None,
    snr=None,
    advert_path_len=None,
):
    """Record one heard-device observation without copying message text."""
    if not public_key:
        return

    observed_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    public_id = public_node_id(public_key)

    user = get_user_by_key(public_key)
    mailbox_name = user["display_name"] if user else None

    conn = get_db()

    conn.execute(
        """
        INSERT INTO devices (
            public_key,
            public_id,
            advertised_name,
            node_type,
            first_seen_at,
            last_seen_at,
            last_event_type,
            last_snr,
            last_advert_path_len,
            sighting_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(public_key) DO UPDATE SET
            advertised_name = COALESCE(
                excluded.advertised_name,
                devices.advertised_name
            ),
            node_type = COALESCE(
                excluded.node_type,
                devices.node_type
            ),
            last_seen_at = excluded.last_seen_at,
            last_event_type = excluded.last_event_type,
            last_snr = COALESCE(
                excluded.last_snr,
                devices.last_snr
            ),
            last_advert_path_len = COALESCE(
                excluded.last_advert_path_len,
                devices.last_advert_path_len
            ),
            sighting_count = devices.sighting_count + 1
        """,
        (
            public_key,
            public_id,
            advertised_name,
            node_type,
            observed_at,
            observed_at,
            event_type,
            snr,
            advert_path_len,
        ),
    )

    conn.execute(
        """
        INSERT INTO sightings (
            observed_at,
            public_key,
            public_id,
            event_type,
            advertised_name,
            mailbox_name,
            node_type,
            snr,
            advert_path_len
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observed_at,
            public_key,
            public_id,
            event_type,
            advertised_name,
            mailbox_name,
            node_type,
            snr,
            advert_path_len,
        ),
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------
# User registration
# ---------------------------------------------------------

def get_user_by_key(public_key):
    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE public_key = ?
        """,
        (public_key,),
    ).fetchone()

    conn.close()
    return row


def get_user_by_name(display_name):
    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE display_name = ? COLLATE NOCASE
        """,
        (display_name.strip(),),
    ).fetchone()

    conn.close()
    return row


def register_user(
    public_key,
    display_name,
    advertised_name=None,
):
    display_name = display_name.strip()

    if not display_name:
        return False, "Name cannot be blank."

    if len(display_name) > 32:
        return False, "Name must be 32 characters or less."

    existing_name = get_user_by_name(display_name)

    if (
        existing_name
        and existing_name["public_key"] != public_key
    ):
        return False, "That BBS name is already registered."

    now = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    conn = get_db()

    existing_user = conn.execute(
        """
        SELECT public_key
        FROM users
        WHERE public_key = ?
        """,
        (public_key,),
    ).fetchone()

    if existing_user:
        conn.execute(
            """
            UPDATE users
            SET display_name = ?,
                advertised_name = ?,
                last_seen_at = ?
            WHERE public_key = ?
            """,
            (
                display_name,
                advertised_name,
                now,
                public_key,
            ),
        )

        action = "updated"

    else:
        conn.execute(
            """
            INSERT INTO users (
                public_key,
                display_name,
                advertised_name,
                created_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                public_key,
                display_name,
                advertised_name,
                now,
                now,
            ),
        )

        action = "registered"

    if action == "registered":
        email_notifications.enqueue_registration(
            conn, public_key, display_name, now
        )

    conn.commit()
    conn.close()

    return (
        True,
        f"{display_name} {action} with NAC-BBS."
    )


def get_directory_page(page=1):
    """Return an alphabetized directory page that fits a radio reply."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT display_name
        FROM users
        ORDER BY display_name COLLATE NOCASE
        """
    ).fetchall()
    conn.close()

    total = len(rows)
    max_page_number = max(total, 1)
    header = (
        "NAC-BBS Mailbox Directory\n"
        f"Page {max_page_number} of {max_page_number} "
        f"- {total} registered"
    )
    header_budget = len(header.encode("utf-8"))
    footer_budget = (
        len(f"Use DIR {max_page_number} for more.".encode("utf-8"))
        if total > 1 else 0
    )
    content_budget = max(
        1,
        DIR_REPLY_MAX_BYTES - header_budget - footer_budget - 2,
    )

    pages = [[]]
    page_bytes = 0
    for number, row in enumerate(rows, start=1):
        name = row["display_name"]
        entry = (number, name)
        entry_bytes = len(f"{number}. {name}".encode("utf-8"))
        extra_bytes = entry_bytes + (1 if pages[-1] else 0)

        if pages[-1] and page_bytes + extra_bytes > content_budget:
            pages.append([])
            page_bytes = 0
            extra_bytes = entry_bytes

        pages[-1].append(entry)
        page_bytes += extra_bytes

    total_pages = len(pages)
    if page < 1 or page > total_pages:
        return None, total, total_pages

    return pages[page - 1], total, total_pages


def directory_reply(page=1):
    rows, total, total_pages = get_directory_page(page)

    if rows is None:
        return (
            f"Directory has {total_pages} page(s). "
            f"Use DIR 1 through DIR {total_pages}."
        )

    lines = [
        "NAC-BBS Mailbox Directory",
        f"Page {page} of {total_pages} - {total} registered",
    ]

    for index, display_name in rows:
        lines.append(f"{index}. {display_name}")

    if page < total_pages:
        lines.append(f"Use DIR {page + 1} for more.")

    return boards_v1.radio_limit(
        "\n".join(lines),
        DIR_REPLY_MAX_BYTES,
    )


def set_user_plan(public_key, plan):
    """Set, replace, or clear a registered user's short public .plan."""
    plan = plan.strip()

    if len(plan) > PLAN_MAX_LENGTH:
        return (
            f"Plan too long. Maximum is "
            f"{PLAN_MAX_LENGTH} characters."
        )

    conn = get_db()
    conn.execute(
        """
        UPDATE users
        SET plan = ?
        WHERE public_key = ?
        """,
        (plan or None, public_key),
    )
    conn.commit()
    conn.close()

    if plan:
        return "Your .plan has been updated."

    return "Your .plan has been cleared."


def who_reply(display_name):
    user = get_user_by_name(display_name)

    if not user:
        return f"No registered user named {display_name}."

    lines = [
        user["display_name"],
        "Registered NAC-BBS user",
        "",
    ]

    plan = user["plan"]

    if plan:
        lines.extend([
            "Plan:",
            plan,
        ])
    else:
        lines.append("No plan set.")

    return "\n".join(lines)



def update_last_seen(public_key, advertised_name=None):
    conn = get_db()

    conn.execute(
        """
        UPDATE users
        SET last_seen_at = ?,
            advertised_name = COALESCE(?, advertised_name)
        WHERE public_key = ?
        """,
        (
            datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            advertised_name,
            public_key,
        ),
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------
# Mailbox
# ---------------------------------------------------------

def mailbox_status(public_key):
    conn = get_db()

    unread = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM mail
        WHERE recipient_key = ?
          AND read_at IS NULL
        """,
        (public_key,),
    ).fetchone()["count"]

    total = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM mail
        WHERE recipient_key = ?
        """,
        (public_key,),
    ).fetchone()["count"]

    conn.close()

    return unread, total


def store_mail(sender_key, recipient_key, message):
    conn = get_db()
    created_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    unread_before = conn.execute(
        """SELECT COUNT(*) FROM mail
           WHERE recipient_key=? AND read_at IS NULL
             AND deleted_at IS NULL""",
        (recipient_key,),
    ).fetchone()[0]

    cursor = conn.execute(
        """
        INSERT INTO mail (
            sender_key,
            recipient_key,
            message,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (sender_key, recipient_key, message, created_at),
    )
    mail_id = cursor.lastrowid

    recipient = conn.execute(
        "SELECT display_name FROM users WHERE public_key = ?",
        (recipient_key,),
    ).fetchone()
    if recipient and recipient["display_name"].casefold() == "andy delgado":
        sender = conn.execute(
            "SELECT display_name FROM users WHERE public_key = ?",
            (sender_key,),
        ).fetchone()
        sender_name = sender["display_name"] if sender else "Unknown sender"
        email_notifications.enqueue_mail_arrival(
            conn, mail_id, sender_name, created_at
        )

    if unread_before == 0:
        chat_notifications.enqueue_unread_notice(
            conn, mail_id, recipient_key, created_at
        )

    conn.commit()
    conn.close()


def read_next_mail(public_key):
    conn = get_db()

    row = conn.execute(
        """
        SELECT
            mail.id,
            mail.sender_key,
            mail.message,
            mail.created_at,
            users.display_name AS sender_name
        FROM mail
        JOIN users
          ON users.public_key = mail.sender_key
        WHERE mail.recipient_key = ?
          AND mail.read_at IS NULL
        ORDER BY mail.id ASC
        LIMIT 1
        """,
        (public_key,),
    ).fetchone()

    if not row:
        conn.close()
        return None

    conn.execute(
        """
        UPDATE mail
        SET read_at = ?
        WHERE id = ?
        """,
        (
            datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            row["id"],
        ),
    )

    conn.commit()
    conn.close()

    return row


# ---------------------------------------------------------
# Weather
# ---------------------------------------------------------

def get_weather_reply():
    if not WEATHER_CACHE.exists():
        return "WX unavailable. No cached NWS forecast yet."

    try:
        data = json.loads(
            WEATHER_CACHE.read_text()
        )
    except Exception:
        return "WX unavailable. Weather cache could not be read."

    updated_at = data.get("updated_at")
    current = data.get("current", {})
    forecast = data.get("forecast", [])

    lines = ["NAC WX"]

    temp = current.get("temperature_f")
    desc = current.get("description")

    if temp is not None or desc:
        parts = []

        if temp is not None:
            parts.append(f"{temp}F")

        if desc:
            parts.append(desc)

        lines.append(
            "Now: " + ", ".join(parts)
        )

    for period in forecast[:2]:
        name = period.get("name")
        temperature = period.get("temperature")
        unit = period.get("temperature_unit", "F")
        short = period.get("short_forecast")

        parts = []

        if temperature is not None:
            parts.append(f"{temperature}{unit}")

        if short:
            parts.append(short)

        if name and parts:
            lines.append(
                f"{name}: " + ", ".join(parts)
            )

    if updated_at:
        try:
            update_time = datetime.fromisoformat(
                updated_at
            )

            age = (
                datetime.now().astimezone()
                - update_time
            )

            lines.append(
                "Updated: "
                + update_time.strftime(
                    "%b %d %I:%M %p"
                )
            )

            if age.total_seconds() > 8 * 3600:
                lines.append(
                    "Forecast may be stale; "
                    "Internet update unavailable."
                )

        except Exception:
            pass

    return "\n".join(lines)


def get_weather_summary():
    """Return a short one-line weather summary for greetings."""
    if not WEATHER_CACHE.exists():
        return "Weather: unavailable."

    try:
        data = json.loads(WEATHER_CACHE.read_text())
    except Exception:
        return "Weather: unavailable."

    current = data.get("current", {})
    temp = current.get("temperature_f")
    desc = current.get("description")

    parts = []

    if temp is not None:
        parts.append(f"{temp}F")

    if desc:
        parts.append(desc)

    if not parts:
        return "Weather: unavailable."

    return "Weather: " + ", ".join(parts)


# ---------------------------------------------------------
# Commands
# ---------------------------------------------------------

def _process_admin_board_command(text, public_key, conn):
    """Run a board-management command after the role check succeeds."""
    parts = text.split(None, 3)
    if len(parts) < 3 or parts[2].upper() == "HELP":
        return (
            "ADMIN BOARD CREATE <name>|<description>; "
            "LIST; ARCHIVE <name>; RESTORE <name>"
        )

    action = parts[2].upper()
    remainder = parts[3].strip() if len(parts) > 3 else ""

    if action == "LIST" and not remainder:
        return boards_v1.list_boards(conn, include_archived=True)

    if action == "CREATE":
        if not remainder:
            return "Usage: ADMIN BOARD CREATE <name>|<description>"
        if "|" in remainder:
            name, description = remainder.split("|", 1)
        else:
            name, description = remainder, ""
        board_id, reply = boards_v1.create_board(
            conn, name, description, created_by=public_key
        )
        if board_id is not None:
            boards_v1.audit(conn, public_key, "create_board", str(board_id), name)
        return reply

    if action in {"ARCHIVE", "RESTORE"} and remainder:
        status = "archived" if action == "ARCHIVE" else "active"
        changed, reply = boards_v1.set_board_status(conn, remainder, status)
        if changed:
            board = boards_v1.find_board(conn, remainder)
            boards_v1.audit(conn, public_key, f"{action.lower()}_board",
                            str(board["id"]), board["name"])
        return reply

    return (
        "Use ADMIN BOARD CREATE <name>|<description>, LIST, "
        "ARCHIVE <name>, or RESTORE <name>."
    )


def _is_superadmin(public_key, conn):
    return _bbs_role(public_key, conn) == "superadmin"


def _bbs_role(public_key, conn):
    has_roles = conn.execute("""SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'admin_roles'""").fetchone()
    if not has_roles:
        return None
    row = conn.execute(
        "SELECT role FROM admin_roles WHERE public_key = ?",
        (public_key,),
    ).fetchone()
    return row["role"] if row else None


def _can_moderate(public_key, conn):
    return _bbs_role(public_key, conn) in {"superadmin", "admin", "sysop"}


def _process_moderation_command(text, public_key, conn):
    """Moderate individual board posts without deleting their history."""
    parts = text.split(None, 3)
    if len(parts) < 3 or parts[2].upper() == "HELP":
        return (
            "ADMIN MOD LIST [page]; HIDE <id>|<reason>; "
            "RESTORE <id>; hides retain audit history."
        )

    action = parts[2].upper()
    payload = parts[3].strip() if len(parts) > 3 else ""
    if action == "LIST":
        try:
            page = int(payload or "1")
        except ValueError:
            return "Usage: ADMIN MOD LIST [page]"
        return boards_v1.list_posts_for_moderation(conn, page)

    if action in {"HIDE", "RESTORE"} and payload:
        if "|" in payload:
            post_text, reason = payload.split("|", 1)
        else:
            post_text, reason = payload, ""
        try:
            post_id = int(post_text.strip())
        except ValueError:
            return f"Usage: ADMIN MOD {action} <post-id>" + ("|<reason>" if action == "HIDE" else "")
        _, reply = boards_v1.moderate_post(
            conn, public_key, post_id, action.lower(), reason
        )
        return reply

    return "Use ADMIN MOD LIST [page], HIDE <id>|<reason>, or RESTORE <id>."


def _process_admin_role_command(text, public_key, conn):
    """Allow superadmins to assign or remove moderator roles over the air."""
    parts = text.split(None, 3)
    if len(parts) < 3 or parts[2].upper() == "HELP":
        return "ADMIN ROLE LIST [page]; GRANT <name>|sysop; REVOKE <name>."
    if not conn.execute("""SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'admin_roles'""").fetchone():
        return "Admin roles are not installed on this BBS."

    action = parts[2].upper()
    payload = parts[3].strip() if len(parts) > 3 else ""
    if action == "LIST":
        try:
            page = int(payload or "1")
        except ValueError:
            return "Usage: ADMIN ROLE LIST [page]"
        if page < 1:
            return "Use ADMIN ROLE LIST [page]."
        size = 3
        total = conn.execute("SELECT COUNT(*) FROM admin_roles").fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        if page > pages:
            return f"Roles: {total}. Use ADMIN ROLE LIST 1-{pages}."
        rows = conn.execute("""SELECT u.display_name, a.role
            FROM admin_roles a JOIN users u ON u.public_key = a.public_key
            ORDER BY u.display_name COLLATE NOCASE LIMIT ? OFFSET ?""",
            (size, (page - 1) * size)).fetchall()
        if not rows:
            return "No administrator roles are assigned."
        lines = [f"BBS roles {page}/{pages}:"]
        lines.extend(f"{row['display_name'][:20]}={row['role']}" for row in rows)
        if page < pages:
            lines.append(f"More: ADMIN ROLE LIST {page + 1}")
        return boards_v1.radio_limit("; ".join(lines))

    if action == "GRANT" and "|" in payload:
        name, role = (piece.strip() for piece in payload.split("|", 1))
        role = role.lower()
        if role not in {"sysop", "admin"}:
            return "Assign sysop or admin here; superadmin changes use the local terminal."
        if not name:
            return "Usage: ADMIN ROLE GRANT <registered name>|sysop"
        target = conn.execute("""SELECT public_key, display_name FROM users
            WHERE display_name = ? COLLATE NOCASE""", (name,)).fetchone()
        if target is None:
            return "Registered user not found. Check the exact BBS display name."
        existing = conn.execute("SELECT role FROM admin_roles WHERE public_key = ?",
                                 (target["public_key"],)).fetchone()
        old_role = existing["role"] if existing else "user"
        if old_role == role:
            return f"{target['display_name']} already has role {role}."
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with conn:
            conn.execute("""INSERT INTO admin_roles
                (public_key, role, granted_at, granted_by) VALUES (?, ?, ?, ?)
                ON CONFLICT(public_key) DO UPDATE SET role=excluded.role,
                    granted_at=excluded.granted_at, granted_by=excluded.granted_by""",
                (target["public_key"], role, stamp, public_key))
            boards_v1.audit(conn, public_key, "grant_role", target["public_key"],
                            f"{old_role} -> {role}")
        return f"Assigned {role} to {target['display_name']}."

    if action == "REVOKE" and payload:
        target = conn.execute("""SELECT u.public_key, u.display_name, a.role
            FROM users u JOIN admin_roles a ON a.public_key = u.public_key
            WHERE u.display_name = ? COLLATE NOCASE""", (payload,)).fetchone()
        if target is None:
            return "Moderator not found. Check the exact BBS display name."
        if target["role"] == "superadmin":
            return "Superadmin roles can only be changed from the local terminal."
        with conn:
            conn.execute("DELETE FROM admin_roles WHERE public_key = ?",
                         (target["public_key"],))
            boards_v1.audit(conn, public_key, "revoke_role", target["public_key"],
                            f"Removed role {target['role']}")
        return f"Removed {target['role']} role from {target['display_name']}."

    return "Use ADMIN ROLE LIST [page], GRANT <name>|sysop, or REVOKE <name>."

def _process_admin_notify_command(text, public_key, conn):
    """Show or change the notification switches."""
    parts = text.split()
    if len(parts) == 3 and parts[2].upper() == "STATUS":
        status = email_notifications.settings_status(conn)
        return (
            "Notifications: "
            f"MAIL={'ON' if status[email_notifications.KIND_MAIL] else 'OFF'}, "
            f"REGISTRATION={'ON' if status[email_notifications.KIND_REGISTRATION] else 'OFF'}, "
            f"CHAT={'ON' if status[email_notifications.KIND_CHAT] else 'OFF'}."
        )

    if len(parts) != 4:
        return (
            "Use ADMIN NOTIFY STATUS, MAIL ON|OFF, "
            "REGISTRATION ON|OFF, or CHAT ON|OFF."
        )

    kind_name = parts[2].upper()
    value = parts[3].upper()
    kinds = {
        "MAIL": email_notifications.KIND_MAIL,
        "REGISTRATION": email_notifications.KIND_REGISTRATION,
        "CHAT": email_notifications.KIND_CHAT,
    }
    kind = kinds.get(kind_name)
    if kind is None or value not in {"ON", "OFF"}:
        return (
            "Use ADMIN NOTIFY STATUS, MAIL ON|OFF, "
            "REGISTRATION ON|OFF, or CHAT ON|OFF."
        )

    enabled = value == "ON"
    email_notifications.set_enabled(conn, kind, enabled, public_key)
    boards_v1.audit(
        conn,
        public_key,
        "set_notification",
        kind,
        value.lower(),
    )
    label = "chat notifications" if kind_name == "CHAT" else "email alerts"
    return f"{kind_name} {label} turned {value.lower()}."


def process_command(
    text,
    public_key,
    advertised_name,
):
    stripped = text.strip()
    upper = stripped.upper()
    user = get_user_by_key(public_key)

    if upper == "BOARDS" or upper.startswith("BOARDS "):
        page_text = stripped[6:].strip() if upper != "BOARDS" else "1"
        try:
            page = int(page_text)
        except ValueError:
            return "Usage: BOARDS [page]"
        conn = get_db()
        try:
            return boards_v1.list_boards(conn, page)
        finally:
            conn.close()

    if upper.startswith("BOARD "):
        board_arg = stripped[6:].strip()
        page = 1
        candidate = board_arg.rsplit(None, 1)
        if len(candidate) == 2 and candidate[1].isdigit():
            board_arg, page_text = candidate
            page = int(page_text)
        board_name = board_arg.strip()
        if not board_name:
            return "Usage: BOARD <name> [page]"
        conn = get_db()
        try:
            return boards_v1.board_preview(conn, board_name, page)
        finally:
            conn.close()

    if upper.startswith("BREAD"):
        args = stripped.split()
        if len(args) not in (2, 3) or not args[1].isdigit():
            return "Usage: BREAD <post-id> [part]"
        part = 1
        if len(args) == 3:
            if not args[2].isdigit():
                return "Usage: BREAD <post-id> [part]"
            part = int(args[2])
        conn = get_db()
        try:
            return boards_v1.read_post(conn, int(args[1]), part)
        finally:
            conn.close()

    if upper.startswith("POST "):
        payload = stripped[5:].strip()
        if "|" not in payload:
            return "Usage: POST <board>|<message>"
        board_name, body = payload.split("|", 1)
        conn = get_db()
        try:
            return boards_v1.post_message(conn, public_key, board_name, body)
        finally:
            conn.close()

    if upper.startswith("ADMIN BOARD"):
        conn = get_db()
        try:
            if not _is_superadmin(public_key, conn):
                return "Superadmin access required."
            return _process_admin_board_command(stripped, public_key, conn)
        finally:
            conn.close()

    if upper.startswith("ADMIN NOTIFY"):
        conn = get_db()
        try:
            if not _is_superadmin(public_key, conn):
                return "Superadmin access required."
            return _process_admin_notify_command(
                stripped, public_key, conn
            )
        finally:
            conn.close()

    if upper.startswith("ADMIN MOD"):
        conn = get_db()
        try:
            if not _can_moderate(public_key, conn):
                return "Sysop or admin access required."
            return _process_moderation_command(stripped, public_key, conn)
        finally:
            conn.close()

    if upper.startswith("ADMIN ROLE"):
        conn = get_db()
        try:
            if not _is_superadmin(public_key, conn):
                return "Superadmin access required."
            return _process_admin_role_command(stripped, public_key, conn)
        finally:
            conn.close()

    if upper in {"HELLO", "HI", "HEY"}:
        now = datetime.now()
        lines = []

        if user:
            lines.append(f"Hello {user['display_name']}!")
        else:
            lines.append("Hello from NAC-BBS!")

        lines.append("Nacogdoches, Texas")
        lines.append(now.strftime("%b %d, %Y - %I:%M %p"))
        lines.append(get_weather_summary())

        if user:
            unread, _ = mailbox_status(public_key)

            if unread == 1:
                lines.append("You have 1 unread message.")
            else:
                lines.append(f"You have {unread} unread messages.")

            if unread:
                lines.append("Send READ to read mail, or HELP for the menu.")
            else:
                lines.append("Send HELP for the menu.")
        else:
            lines.append("New here? Send REGISTER Your Name")
            lines.append("Send HELP for the menu.")

        return "\n".join(lines)

    if upper in {"HELP", "HELP 1"}:
        return (
            "NAC-BBS HELP 1/2\n"
            "HELLO ABOUT TIME WX BYE\n"
            "REGISTER <name>; DIR [page]; WHO <name>\n"
            "PLAN [text|CLEAR]; HELP 2 (mail/boards)"
        )

    if upper == "HELP 2":
        return (
            "NAC-BBS HELP 2/2\n"
            "MAIL [command]; SEND name|text\n"
            "BOARDS [page]; BOARD <name> [page]\n"
            "POST <board>|<text>; BREAD <id> [part]\n"
            "HELP MAIL/BOARD"
        )

    if upper == "HELP BOARD":
        return (
            "BOARDS [page]: list.\n"
            "BOARD <name> [page]: browse.\n"
            "BREAD <id> [part]: read.\n"
            "POST <board>|<text>: public post.\n"
            "ADMIN BOARD: admin tools."
        )

    if upper == "HELP MOD":
        conn = get_db()
        try:
            if not _can_moderate(public_key, conn):
                return "Moderator help is available to assigned sysops/admins."
            return _process_moderation_command("ADMIN MOD HELP", public_key, conn)
        finally:
            conn.close()

    if upper == "HELP ADMIN":
        conn = get_db()
        try:
            if not _is_superadmin(public_key, conn):
                return "Superadmin access required."
            return (
                "ADMIN ROLE LIST; GRANT name|sysop; REVOKE name. "
                "ADMIN NOTIFY STATUS/MAIL ON|OFF/REGISTRATION ON|OFF/"
                "CHAT ON|OFF; ADMIN BOARD HELP."
            )
        finally:
            conn.close()

    if upper == "HELP":
        return "Send HELP 1, HELP 2, HELP MAIL, or HELP BOARD."

    if upper == "BYE":
        return (
            "NAC-BBS: Nice talking with you! "
            "Stop by and say hello when you're back. Take care!"
        )

    if upper == "ABOUT":
        return (
            "NAC-BBS is an experimental "
            "EastTexasMeshNet MeshCore BBS "
            "in Nacogdoches, Texas."
        )

    if upper == "TIME":
        return datetime.now().strftime(
            "NAC-BBS time: %Y-%m-%d %H:%M:%S"
        )

    if upper == "WX":
        return get_weather_reply()

    if upper.startswith("REGISTER "):
        display_name = stripped[9:].strip()

        success, reply = register_user(
            public_key,
            display_name,
            advertised_name,
        )

        return reply

    if upper == "DIR" or upper.startswith("DIR "):
        if upper == "DIR":
            page = 1
        else:
            page_text = stripped[4:].strip()

            try:
                page = int(page_text)
            except ValueError:
                return "Usage: DIR or DIR <page>"

        return directory_reply(page)

    if upper.startswith("WHO "):
        display_name = stripped[4:].strip()

        if not display_name:
            return "Usage: WHO <name>"

        return who_reply(display_name)

    if upper == "WHO":
        return "Usage: WHO <name>"

    if upper == "PLAN":
        if not user:
            return (
                "You are not registered. "
                "Use REGISTER <name> first."
            )

        if user["plan"]:
            return f"Your .plan: {user['plan']}"

        return "You have no .plan set. Use PLAN <text>."

    if upper == "PLAN CLEAR":
        if not user:
            return (
                "You are not registered. "
                "Use REGISTER <name> first."
            )

        return set_user_plan(public_key, "")

    if upper.startswith("PLAN "):
        if not user:
            return (
                "You are not registered. "
                "Use REGISTER <name> first."
            )

        plan = stripped[5:].strip()

        if not plan:
            return "Usage: PLAN <text> or PLAN CLEAR"

        return set_user_plan(public_key, plan)


    mailbox_response = mailbox_v1.handle(stripped, public_key, user, get_db)
    if mailbox_response is not None:
        return mailbox_response

    if upper == "MAIL":
        if not user:
            return (
                "You are not registered. "
                "Use REGISTER <name> first."
            )

        unread, total = mailbox_status(
            public_key
        )

        return (
            f"Mailbox for {user['display_name']}: "
            f"{unread} unread, {total} total."
        )

    if upper == "READ":
        if not user:
            return (
                "You are not registered. "
                "Use REGISTER <name> first."
            )

        message = read_next_mail(public_key)

        if not message:
            return "No unread mail."

        created = datetime.fromisoformat(
            message["created_at"]
        )

        return (
            f"From {message['sender_name']} "
            f"{created.strftime('%b %d %I:%M %p')}: "
            f"{message['message']}"
        )

    if upper.startswith("SEND "):
        if not user:
            return (
                "You are not registered. "
                "Use REGISTER <name> first."
            )

        body = stripped[5:].strip()

        if "|" not in body:
            return (
                "Usage: SEND <name>|<message>"
            )

        recipient_name, message = body.split(
            "|",
            1,
        )

        recipient_name = recipient_name.strip()
        message = message.strip()

        if not recipient_name or not message:
            return (
                "Usage: SEND <name>|<message>"
            )

        if len(message) > 500:
            return (
                "Message too long. "
                "Maximum is 500 characters."
            )

        recipient = get_user_by_name(
            recipient_name
        )

        if not recipient:
            return (
                f"No registered user named "
                f"{recipient_name}."
            )

        store_mail(
            public_key,
            recipient["public_key"],
            message,
        )

        return (
            f"Message saved for "
            f"{recipient['display_name']}."
        )

    return (
        "Unknown command. Send HELP."
    )


# ---------------------------------------------------------
# MeshCore identity resolution
# ---------------------------------------------------------

def resolve_contact(
    contacts,
    prefix,
):
    for public_key, contact in contacts.items():
        if public_key.startswith(prefix):
            return public_key, contact

    return None, None


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

async def main():
    from meshcore import MeshCore, EventType

    init_db()

    print(f"[{stamp()}] Starting NAC-BBS")
    print(f"[{stamp()}] Connecting to {DEVICE}")

    meshcore = await MeshCore.create_serial(
        DEVICE,
        115200,
        debug=False,
    )

    print(f"[{stamp()}] Connected.")

    result = await meshcore.commands.send_appstart()

    if result.type == EventType.ERROR:
        print(f"[{stamp()}] APPSTART failed:")
        print(result.payload)
        await meshcore.disconnect()
        return

    print(
        f"[{stamp()}] Radio: "
        f"{result.payload.get('name', 'Unknown')}"
    )

    print(
        f"[{stamp()}] Location: "
        f"{result.payload.get('adv_lat')}, "
        f"{result.payload.get('adv_lon')}"
    )

    print(
        f"[{stamp()}] Advert location policy: "
        f"{result.payload.get('adv_loc_policy', 'Unknown')}"
    )

    contacts_result = (
        await meshcore.commands.get_contacts()
    )

    if contacts_result.type == EventType.ERROR:
        contacts = {}
        print(
            f"[{stamp()}] Could not load contacts."
        )
    else:
        contacts = contacts_result.payload

        print(
            f"[{stamp()}] Loaded "
            f"{len(contacts)} contacts."
        )

    # Track the last courtesy advert sent for each client public key.
    courtesy_advert_times = {}

    async def refresh_contacts():
        nonlocal contacts

        result = (
            await meshcore.commands.get_contacts()
        )

        if result.type != EventType.ERROR:
            contacts = result.payload

    async def chat_notification_worker():
        """Send queued private inbox notices over MeshCore."""
        while True:
            item = await asyncio.to_thread(
                chat_notifications.next_pending, DB_FILE
            )
            if item is None:
                await asyncio.sleep(chat_notifications.POLL_SECONDS)
                continue

            if not await asyncio.to_thread(
                chat_notifications.is_enabled, DB_FILE
            ):
                await asyncio.to_thread(
                    chat_notifications.mark_suppressed,
                    DB_FILE,
                    item["id"],
                )
                continue

            contact = contacts.get(item["recipient_key"])
            if not contact:
                await refresh_contacts()
                contact = contacts.get(item["recipient_key"])

            if not contact:
                await asyncio.to_thread(
                    chat_notifications.mark_failed,
                    DB_FILE,
                    item["id"],
                    item["attempts"] + 1,
                    "recipient contact unavailable",
                )
                print(
                    f"[{stamp()}] Chat notification {item['id']} "
                    "queued; recipient contact unavailable."
                )
                continue

            try:
                result = await meshcore.commands.send_msg(
                    contact,
                    item["message"],
                )
                if result.type == EventType.ERROR:
                    await asyncio.to_thread(
                        chat_notifications.mark_failed,
                        DB_FILE,
                        item["id"],
                        item["attempts"] + 1,
                        "radio send returned an error",
                    )
                    print(
                        f"[{stamp()}] Chat notification {item['id']} "
                        "send failed; retry scheduled."
                    )
                else:
                    await asyncio.to_thread(
                        chat_notifications.mark_sent,
                        DB_FILE,
                        item["id"],
                    )
                    print(
                        f"[{stamp()}] Chat notification {item['id']} "
                        "queued for radio delivery."
                    )
            except Exception as exc:
                await asyncio.to_thread(
                    chat_notifications.mark_failed,
                    DB_FILE,
                    item["id"],
                    item["attempts"] + 1,
                    type(exc).__name__,
                )
                print(
                    f"[{stamp()}] Chat notification {item['id']} "
                    "send failed; retry scheduled."
                )

    async def scheduled_advertisements():
        while True:
            now = datetime.now().astimezone()

            if now.minute < 30:
                next_run = now.replace(
                    minute=30,
                    second=0,
                    microsecond=0,
                )
            else:
                next_run = (
                    now + timedelta(hours=1)
                ).replace(
                    minute=0,
                    second=0,
                    microsecond=0,
                )

            delay = (next_run - now).total_seconds()
            await asyncio.sleep(delay)

            print(
                f"[{stamp()}] Sending scheduled "
                f"NAC-BBS flood advertisement..."
            )

            result = await meshcore.commands.send_advert(
                flood=True,
            )

            if result.type == EventType.ERROR:
                print(
                    f"[{stamp()}] ERROR sending scheduled "
                    f"advertisement:"
                )
                print(result.payload)
            else:
                print(
                    f"[{stamp()}] Scheduled NAC-BBS "
                    f"flood advertisement sent."
                )

    async def handle_contact_message(event):
        msg = event.payload

        sender_prefix = msg.get(
            "pubkey_prefix",
            "",
        )

        text = msg.get("text", "")
        snr = msg.get("SNR")

        print()
        print("----------------------------------------")
        print(f"[{stamp()}] DIRECT MESSAGE")
        print(f"From: {sender_prefix}")
        print(f"SNR:  {snr}")
        message_bytes = len(text.encode("utf-8"))
        print(f"Message length: {message_bytes} bytes (content omitted)")
        print("----------------------------------------")

        public_key, contact = resolve_contact(
            contacts,
            sender_prefix,
        )

        if not public_key:
            print(
                f"[{stamp()}] Sender not in "
                f"contact cache. Refreshing..."
            )

            await refresh_contacts()

            public_key, contact = resolve_contact(
                contacts,
                sender_prefix,
            )

        if not public_key:
            print(
                f"[{stamp()}] Could not resolve "
                f"full public key."
            )
            return

        advertised_name = contact.get(
            "adv_name",
            "",
        )

        record_device_sighting(
            public_key,
            "DIRECT_MESSAGE",
            advertised_name=advertised_name,
            node_type=contact.get("type"),
            snr=snr,
        )

        # Store reception metadata only, never the incoming command or mail body.
        store_incoming_message(
            public_key,
            snr,
        )

        if get_user_by_key(public_key):
            update_last_seen(
                public_key,
                advertised_name,
            )

        reply = process_command(
            text,
            public_key,
            advertised_name,
        )

        reply_bytes = len(reply.encode("utf-8"))
        print(
            f"[{stamp()}] Reply prepared: {reply_bytes} bytes "
            "(content omitted)."
        )

        send_result = (
            await meshcore.commands.send_msg(
                contact,
                reply,
            )
        )

        if send_result.type == EventType.ERROR:
            print(
                f"[{stamp()}] ERROR sending reply:"
            )
            print(
                f"[{stamp()}] Send error payload omitted "
                "to protect message content."
            )
        else:
            print(
                f"[{stamp()}] Reply queued successfully."
            )

    async def handle_advertisement(event):
        advert_key = event.payload.get(
            "public_key",
            "",
        )

        print(
            f"[{stamp()}] Advertisement: "
            f"{advert_key[:12]}"
        )

        await refresh_contacts()

        public_key, contact = resolve_contact(
            contacts,
            advert_key,
        )

        if not public_key or not contact:
            print(
                f"[{stamp()}] Advertiser not found in "
                f"contact cache after refresh."
            )
            return

        name = contact.get(
            "adv_name",
            public_key[:12],
        )
        node_type = contact.get("type")
        path_len = None

        # The Companion exposes the route taken by the most recently
        # received advert separately from the normal stored route.
        get_advert_path = getattr(
            meshcore.commands,
            "get_advert_path",
            None,
        )

        if get_advert_path is not None:
            path_result = await get_advert_path(contact)

            if (
                path_result is not None
                and path_result.type != EventType.ERROR
            ):
                path_len = path_result.payload.get(
                    "path_len"
                )

        record_device_sighting(
            public_key,
            "ADVERTISEMENT",
            advertised_name=name,
            node_type=node_type,
            advert_path_len=path_len,
        )

        # Courtesy adverts are only useful for client/companion nodes.
        if node_type != MESHCORE_CLIENT_TYPE:
            return

        if path_len is None:
            print(
                f"[{stamp()}] Could not determine advert "
                f"path for {name}; no courtesy advert sent."
            )
            return

        # path_len == 0 means the advertiser was heard directly.
        # A non-zero path arrived through repeaters, so a zero-hop
        # courtesy advert would not reach that remote client.
        if path_len != 0:
            print(
                f"[{stamp()}] {name} advert path is "
                f"{path_len}; not a direct neighbor. "
                f"No zero-hop courtesy advert."
            )
            return

        now = asyncio.get_running_loop().time()
        last_sent = courtesy_advert_times.get(public_key)

        if last_sent is not None:
            elapsed = now - last_sent

            if elapsed < COURTESY_ADVERT_COOLDOWN_SECONDS:
                remaining = int(
                    COURTESY_ADVERT_COOLDOWN_SECONDS
                    - elapsed
                )

                print(
                    f"[{stamp()}] Direct advert from {name}; "
                    f"courtesy advert cooldown has "
                    f"{remaining}s remaining."
                )
                return

        await asyncio.sleep(
            COURTESY_ADVERT_DELAY_SECONDS
        )

        print(
            f"[{stamp()}] Direct client advert from {name}; "
            f"sending NAC-BBS zero-hop courtesy advert..."
        )

        result = await meshcore.commands.send_advert(
            flood=False,
        )

        if result.type == EventType.ERROR:
            print(
                f"[{stamp()}] ERROR sending zero-hop "
                f"courtesy advert for {name}:"
            )
            print(result.payload)
            return

        courtesy_advert_times[public_key] = now

        print(
            f"[{stamp()}] Zero-hop courtesy advert "
            f"sent for {name}."
        )

    async def handle_new_contact(event):
        print(
            f"[{stamp()}] New contact discovered."
        )

        await refresh_contacts()

    meshcore.subscribe(
        EventType.CONTACT_MSG_RECV,
        handle_contact_message,
    )

    meshcore.subscribe(
        EventType.ADVERTISEMENT,
        handle_advertisement,
    )

    meshcore.subscribe(
        EventType.NEW_CONTACT,
        handle_new_contact,
    )

    await meshcore.start_auto_message_fetching()

    print(f"[{stamp()}] Sending NAC-BBS flood advertisement...")

    advert_result = await meshcore.commands.send_advert(
        flood=True,
    )

    if advert_result.type == EventType.ERROR:
        print(
            f"[{stamp()}] ERROR sending advertisement:"
        )
        print(advert_result.payload)
    else:
        print(
            f"[{stamp()}] NAC-BBS flood advertisement sent."
        )

    advert_task = asyncio.create_task(
        scheduled_advertisements()
    )
    notification_task = asyncio.create_task(
        email_notifications.run_worker(DB_FILE)
    )
    chat_notification_task = asyncio.create_task(
        chat_notification_worker()
    )

    print()
    print(f"[{stamp()}] NAC-BBS is online.")
    print(
        "Commands: HELLO, HELP, ABOUT, TIME, WX, "
        "REGISTER, MAIL, READ, SEND, DIR, WHO, PLAN, "
        "BOARDS, BOARD, BREAD, POST, BYE"
    )
    print("Scheduled flood adverts: :00 and :30 each hour.")
    print(
        "Courtesy zero-hop advert: direct clients, "
        "30-minute per-client cooldown."
    )
    print("Press Ctrl+C to stop.")
    print()

    try:
        while True:
            await asyncio.sleep(3600)

    except KeyboardInterrupt:
        print()
        print(f"[{stamp()}] Shutting down...")

    finally:
        advert_task.cancel()
        notification_task.cancel()
        chat_notification_task.cancel()

        for task in (
            advert_task,
            notification_task,
            chat_notification_task,
        ):
            try:
                await task
            except asyncio.CancelledError:
                pass

        await meshcore.stop_auto_message_fetching()
        await meshcore.disconnect()

        print(
            f"[{stamp()}] Disconnected."
        )


if __name__ == "__main__":
    asyncio.run(main())
