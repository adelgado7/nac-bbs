import asyncio
from datetime import datetime

from meshcore import MeshCore, EventType


DEVICE = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_E0F5CFB4A54536EA-if00"


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def main():
    print(f"[{stamp()}] Connecting to NAC-BBS...")
    print(f"[{stamp()}] Device: {DEVICE}")

    meshcore = await MeshCore.create_serial(
        DEVICE,
        115200,
        debug=False
    )

    print(f"[{stamp()}] Connected.")

    # Initialize the companion session.
    self_info = await meshcore.commands.send_appstart()

    if self_info.type == EventType.ERROR:
        print(f"[{stamp()}] ERROR during appstart:")
        print(self_info.payload)
        await meshcore.disconnect()
        return

    print(
        f"[{stamp()}] Radio: "
        f"{self_info.payload.get('name', 'Unknown')}"
    )

    print(
        f"[{stamp()}] Location: "
        f"{self_info.payload.get('adv_lat')}, "
        f"{self_info.payload.get('adv_lon')}"
    )

    async def handle_contact_message(event):
        msg = event.payload

        print("\n----------------------------------------")
        print(f"[{stamp()}] DIRECT MESSAGE")
        print(f"From key: {msg.get('pubkey_prefix', 'unknown')}")
        print(f"Text:     {msg.get('text', '')}")
        print(f"Payload:  {msg}")
        print("----------------------------------------")

    async def handle_channel_message(event):
        msg = event.payload

        print("\n----------------------------------------")
        print(f"[{stamp()}] CHANNEL MESSAGE")
        print(f"Channel:  {msg.get('channel_idx', 'unknown')}")
        print(f"Text:     {msg.get('text', '')}")
        print(f"Payload:  {msg}")
        print("----------------------------------------")

    async def handle_advertisement(event):
        print(
            f"[{stamp()}] Advertisement received: "
            f"{event.payload}"
        )

    async def handle_new_contact(event):
        print(
            f"[{stamp()}] New contact: "
            f"{event.payload}"
        )

    meshcore.subscribe(
        EventType.CONTACT_MSG_RECV,
        handle_contact_message
    )

    meshcore.subscribe(
        EventType.CHANNEL_MSG_RECV,
        handle_channel_message
    )

    meshcore.subscribe(
        EventType.ADVERTISEMENT,
        handle_advertisement
    )

    meshcore.subscribe(
        EventType.NEW_CONTACT,
        handle_new_contact
    )

    print(f"[{stamp()}] Starting automatic message fetching...")

    await meshcore.start_auto_message_fetching()

    print(f"[{stamp()}] NAC-BBS listener is running.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            await asyncio.sleep(3600)

    except KeyboardInterrupt:
        print(f"\n[{stamp()}] Shutting down...")

    finally:
        await meshcore.stop_auto_message_fetching()
        await meshcore.disconnect()
        print(f"[{stamp()}] Disconnected.")


asyncio.run(main())
