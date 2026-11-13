import asyncio
from meshcore import MeshCore, EventType

DEVICE = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_E0F5CFB4A54536EA-if00"

async def main():
    print(f"Connecting to: {DEVICE}")

    meshcore = await MeshCore.create_serial(
        DEVICE,
        115200,
        debug=True
    )

    print("\nConnected.")

    result = await meshcore.commands.send_appstart()

    print("\nAPPSTART / SELF INFO")
    print("Type:", result.type)
    print("Payload:", result.payload)

    result = await meshcore.commands.send_device_query()

    print("\nDEVICE INFO")
    print("Type:", result.type)
    print("Payload:", result.payload)

    await meshcore.disconnect()

asyncio.run(main())
