"""Enable location in NAC-BBS radio advertisements.

Run only while nac-bbs.service is stopped; the USB radio supports one
serial client at a time. Does not modify the BBS database or coordinates.
"""
import asyncio
from meshcore import MeshCore, EventType

DEVICE = (
    "/dev/serial/by-id/"
    "usb-RAKwireless_WisCore_RAK4631_Board_"
    "E0F5CFB4A54536EA-if00"
)
TARGET_POLICY = 1  # Companion firmware: 0 = none, 1 = share stored location.


async def main():
    radio = await MeshCore.create_serial(DEVICE, 115200, debug=False)
    try:
        before = await radio.commands.send_appstart()
        if before.type == EventType.ERROR:
            raise RuntimeError(f"Cannot read radio configuration: {before.payload}")

        info = before.payload
        print(f"Radio: {info.get('name')}")
        print(f"Stored location: {info.get('adv_lat')}, {info.get('adv_lon')}")
        current = info.get("adv_loc_policy")
        print(f"Current advertisement location policy: {current}")
        if current == TARGET_POLICY:
            print("Location sharing is already enabled. No change needed.")
            return
        if current != 0:
            raise RuntimeError(f"Unexpected policy {current!r}; no change made")

        result = await radio.commands.set_advert_loc_policy(TARGET_POLICY)
        if result.type == EventType.ERROR:
            raise RuntimeError(f"Failed to update location policy: {result.payload}")

        after = await radio.commands.send_appstart()
        if after.type == EventType.ERROR:
            raise RuntimeError(f"Cannot verify updated policy: {after.payload}")
        updated = after.payload.get("adv_loc_policy")
        print(f"Verified advertisement location policy: {updated}")
        if updated != TARGET_POLICY:
            raise RuntimeError("Radio did not report policy 1 after update")
        print("Success. Restart NAC-BBS to send its startup flood advert.")
    finally:
        await radio.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
