import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
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

# Courtesy advert behavior. When NAC-BBS hears a direct client advert,
# it answers with one zero-hop advert so the client also learns NAC-BBS.
# The per-client cooldown prevents advert ping-pong or repeated chatter.
COURTESY_ADVERT_COOLDOWN_SECONDS = 30 * 60
COURTESY_ADVERT_DELAY_SECONDS = 1.0
MESHCORE_CLIENT_TYPE = 1

# Community directory behavior.
DIR_PAGE_SIZE = 10
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

    conn.commit()
    conn.close()

    return (
        True,
        f"{display_name} {action} with NAC-BBS."
    )


def get_directory_page(page=1):
    """Return one alphabetized page of registered mailbox names."""
    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) AS count FROM users"
    ).fetchone()["count"]

    total_pages = max(
        1,
        (total + DIR_PAGE_SIZE - 1) // DIR_PAGE_SIZE,
    )

    if page < 1 or page > total_pages:
        conn.close()
        return None, total, total_pages

    offset = (page - 1) * DIR_PAGE_SIZE

    rows = conn.execute(
        """
        SELECT display_name
        FROM users
        ORDER BY display_name COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,
        (DIR_PAGE_SIZE, offset),
    ).fetchall()

    conn.close()
    return rows, total, total_pages


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

    start_number = ((page - 1) * DIR_PAGE_SIZE) + 1

    for index, row in enumerate(rows, start=start_number):
        lines.append(f"{index}. {row['display_name']}")

    if total_pages > 1:
        lines.append("Use DIR <page> for more.")

    return "\n".join(lines)


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

def process_command(
    text,
    public_key,
    advertised_name,
):
    stripped = text.strip()
    upper = stripped.upper()
    user = get_user_by_key(public_key)

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

    if upper == "HELP":
        return (
            "NAC-BBS: HELP ABOUT TIME WX "
            "REGISTER <name> MAIL READ "
            "SEND <name>|<message> DIR [page] "
            "WHO <name> PLAN [text|CLEAR]"
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

    # Track the last courtesy advert sent for each client public key.
    courtesy_advert_times = {}

    async def refresh_contacts():
        nonlocal contacts

        result = (
            await meshcore.commands.get_contacts()
        )

        if result.type != EventType.ERROR:
            contacts = result.payload

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

        record_device_sighting(
            public_key,
            "DIRECT_MESSAGE",
            advertised_name=advertised_name,
            node_type=contact.get("type"),
            snr=snr,
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

    print()
    print(f"[{stamp()}] NAC-BBS is online.")
    print(
        "Commands: HELLO, HELP, ABOUT, TIME, WX, "
        "REGISTER, MAIL, READ, SEND, DIR, WHO, PLAN"
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

        try:
            await advert_task
        except asyncio.CancelledError:
            pass

        await meshcore.stop_auto_message_fetching()
        await meshcore.disconnect()

        print(
            f"[{stamp()}] Disconnected."
        )


asyncio.run(main())
