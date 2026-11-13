import asyncio
import sqlite3
from datetime import datetime

from meshcore import MeshCore, EventType


DEVICE = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_E0F5CFB4A54536EA-if00"
DB_FILE = "bbs.db"


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            sender_key TEXT NOT NULL,
            message TEXT NOT NULL,
            snr REAL
        )
    """)

    conn.commit()
    conn.close()


def store_message(sender_key, message, snr):
    conn = sqlite3.connect(DB_FILE)

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
            datetime.now().isoformat(timespec="seconds"),
            sender_key,
            message,
            snr
        )
    )

    conn.commit()
    conn.close()


import json
from pathlib import Path
from datetime import datetime

WEATHER_CACHE = Path.home() / "meshcore-bbs" / "weather_cache.json"


def get_weather_reply():
    if not WEATHER_CACHE.exists():
        return (
            "WX unavailable. No cached NWS forecast yet."
        )

    try:
        data = json.loads(
            WEATHER_CACHE.read_text()
        )
    except Exception:
        return (
            "WX unavailable. Weather cache could not be read."
        )

    updated_at = data.get("updated_at")
    current = data.get("current", {})
    forecast = data.get("forecast", [])

    lines = ["NAC WX"]

    temp = current.get("temperature_f")
    desc = current.get("description")

    if temp is not None or desc:
        current_parts = []

        if temp is not None:
            current_parts.append(f"{temp}F")

        if desc:
            current_parts.append(desc)

        lines.append(
            "Now: " + ", ".join(current_parts)
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
            update_time = datetime.fromisoformat(updated_at)
            age = (
                datetime.now().astimezone() - update_time
            )

            lines.append(
                "Updated: "
                + update_time.strftime(
                    "%b %d %I:%M %p"
                ).lstrip("0")
            )

            if age.total_seconds() > 8 * 3600:
                lines.append(
                    "Forecast may be stale; "
                    "Internet update unavailable."
                )

        except Exception:
            pass

    return "\n".join(lines)

def process_command(text):
    command = text.strip().upper()

    if command == "HELP":
        return (
            "NAC-BBS commands: "
            "HELP, ABOUT, TIME, WX"
        )

    if command == "ABOUT":
        return (
            "NAC-BBS is an experimental EastTexasMeshNet "
            "MeshCore BBS in Nacogdoches, Texas."
        )

    if command == "TIME":
        return datetime.now().strftime(
            "NAC-BBS time: %Y-%m-%d %H:%M:%S"
        )

    if command == "WX":
        return get_weather_reply()

    return (
        "Unknown command. Send HELP for available commands."
    )


async def main():
    init_db()

    print(f"[{stamp()}] Starting NAC-BBS")
    print(f"[{stamp()}] Connecting to {DEVICE}")

    meshcore = await MeshCore.create_serial(
        DEVICE,
        115200,
        debug=False
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

    contacts_result = await meshcore.commands.get_contacts()

    if contacts_result.type == EventType.ERROR:
        print(f"[{stamp()}] Could not load contacts.")
    else:
        print(
            f"[{stamp()}] Loaded "
            f"{len(contacts_result.payload)} contacts."
        )

    async def handle_contact_message(event):
        msg = event.payload

        sender_prefix = msg.get("pubkey_prefix", "")
        text = msg.get("text", "")
        snr = msg.get("SNR")

        print()
        print("----------------------------------------")
        print(f"[{stamp()}] DIRECT MESSAGE")
        print(f"From: {sender_prefix}")
        print(f"SNR:  {snr}")
        print(f"Text: {text}")
        print("----------------------------------------")

        store_message(
            sender_prefix,
            text,
            snr
        )

        contact = meshcore.get_contact_by_key_prefix(
            sender_prefix
        )

        if not contact:
            print(
                f"[{stamp()}] Contact not found for "
                f"{sender_prefix}"
            )
            return

        reply = process_command(text)

        print(
            f"[{stamp()}] Replying: {reply}"
        )

        result = await meshcore.commands.send_msg(
            contact,
            reply
        )

        if result.type == EventType.ERROR:
            print(
                f"[{stamp()}] ERROR sending reply:"
            )
            print(result.payload)
        else:
            print(
                f"[{stamp()}] Reply queued successfully."
            )

    async def handle_advertisement(event):
        print(
            f"[{stamp()}] Advertisement: "
            f"{event.payload.get('public_key', '')[:12]}"
        )

    async def handle_new_contact(event):
        print(
            f"[{stamp()}] New contact discovered."
        )

    meshcore.subscribe(
        EventType.CONTACT_MSG_RECV,
        handle_contact_message
    )

    meshcore.subscribe(
        EventType.ADVERTISEMENT,
        handle_advertisement
    )

    meshcore.subscribe(
        EventType.NEW_CONTACT,
        handle_new_contact
    )

    await meshcore.start_auto_message_fetching()

    print()
    print(f"[{stamp()}] NAC-BBS is online.")
    print("Commands: HELP, ABOUT, TIME, WX")
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
        print(f"[{stamp()}] Disconnected.")


asyncio.run(main())
