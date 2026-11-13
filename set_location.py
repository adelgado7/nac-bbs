import asyncio
from meshcore import MeshCore, EventType

DEVICE = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_E0F5CFB4A54536EA-if00"

LAT = 31.605964561819555
LON = -94.64340711642238


async def main():
    print(f"Connecting to {DEVICE}")

    meshcore = await MeshCore.create_serial(
        DEVICE,
        115200,
        debug=False
    )

    print("Connected.")

    result = await meshcore.commands.set_coords(LAT, LON)

    if result.type == EventType.ERROR:
        print("ERROR setting coordinates:")
        print(result.payload)
        await meshcore.disconnect()
        return

    print("Coordinates set successfully.")

    # Read the radio's self info back to verify.
    result = await meshcore.commands.send_appstart()

    print("\nVerified SELF_INFO:")
    print(result.payload)

    await meshcore.disconnect()


asyncio.run(main())
