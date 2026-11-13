import asyncio
from pprint import pprint

from meshcore import MeshCore, EventType


DEVICE = "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_E0F5CFB4A54536EA-if00"


async def main():
    meshcore = await MeshCore.create_serial(
        DEVICE,
        115200,
        debug=False
    )

    result = await meshcore.commands.send_appstart()

    if result.type == EventType.ERROR:
        print("APPSTART failed:")
        print(result.payload)
        await meshcore.disconnect()
        return

    contacts = await meshcore.commands.get_contacts()

    print("\nCONTACT RESULT TYPE:")
    print(contacts.type)

    print("\nCONTACT PAYLOAD:")
    pprint(contacts.payload)

    await meshcore.disconnect()


asyncio.run(main())

