import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from meshcore import MeshCore, EventType


DEVICE = (
    "/dev/serial/by-id/"
    "usb-RAKwireless_WisCore_RAK4631_Board_"
    "E0F5CFB4A54536EA-if00"
)

BASE_DIR = Path.home() / "meshcore-bbs"
DB_FILE = BASE_DIR / "bbs.db"
WEATHER_CACHE = BASE_DIR / "weather_cache.json"


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
            last_seen_at TEXT NOT NULL
        )
    """)

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

    conn.commit()
    conn.close()


def store_incoming_message(sender_key, message, snr):
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
            message,
            snr,
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

    conn.commit()
    conn.close()

    return (
        True,
        f"{display_name} {action} with NAC-BBS."
    )


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

    conn.execute(
        """
        INSERT INTO mail (
            sender_key,
            recipient_key,
            message,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            sender_key,
            recipient_key,
            message,
            datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
        ),
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


# ---------------------------------------------------------
# Commands
# ---------------------------------------------------------

def process_command(
    text,
    public_key,
    advertised_name,
):
    stripped = text.strip()
    upper = stripped.upper()

    if upper == "HELP":
        return (
            "NAC-BBS: HELP ABOUT TIME WX "
            "REGISTER <name> MAIL READ "
            "SEND <name>|<message>"
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

    user = get_user_by_key(public_key)

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

    async def refresh_contacts():
        nonlocal contacts

        result = (
            await meshcore.commands.get_contacts()
        )

        if result.type != EventType.ERROR:
            contacts = result.payload

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
        print(f"Text: {text}")
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

        store_incoming_message(
            public_key,
            text,
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

        print(
            f"[{stamp()}] Replying: {reply}"
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
            print(send_result.payload)
        else:
            print(
                f"[{stamp()}] Reply queued successfully."
            )

    async def handle_advertisement(event):
        print(
            f"[{stamp()}] Advertisement: "
            f"{event.payload.get('public_key', '')[:12]}"
        )

        await refresh_contacts()

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

    print()
    print(f"[{stamp()}] NAC-BBS is online.")

    print()
    print(f"[{stamp()}] NAC-BBS is online.")
    print(
        "Commands: HELP, ABOUT, TIME, WX, "
        "REGISTER, MAIL, READ, SEND"
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
        await meshcore.stop_auto_message_fetching()
        await meshcore.disconnect()

        print(
            f"[{stamp()}] Disconnected."
        )


asyncio.run(main())
