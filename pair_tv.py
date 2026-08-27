"""One-off: pair with the TV and stash the client-key in the bot's database.

The TV shows an accept-or-deny prompt on screen. Nobody has to get up for it —
the phone is pointed at the IR receiver, so we press OK with the IR blaster we
already have. Run it once; after that the key does the work.

    python pair_tv.py            # just press OK
    python pair_tv.py left ok    # move the focus first, then press OK

The 2015 sets don't always land the focus on "Yes", so the button sequence is
an argument instead of something hard-coded.
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets

import ir
import store
import tv


async def confirm_with_ir(sequence: list[str]) -> None:
    """Nudge the on-screen dialog with the IR blaster."""
    await asyncio.sleep(3)  # let the dialog draw first
    for button in sequence:
        ok, msg = await ir.send(button)
        print(f"  ИК {button}: {'ушло' if ok else msg}")
        await asyncio.sleep(1.2)


async def main() -> None:
    store.init()
    url = f"ws://{tv.TV_IP}:{tv.PORT}"
    print(f"подключаюсь к {url}")
    async with websockets.connect(url, ping_interval=None, max_size=4 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": "register_0", "type": "register",
            "payload": {"forcePairing": False, "pairingType": "PROMPT",
                        "manifest": tv.MANIFEST},
        }))
        sequence = sys.argv[1:] or ["ok"]
        print("нажму на телевизоре:", " → ".join(sequence))
        presser = asyncio.create_task(confirm_with_ir(sequence))
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
                print("←", msg.get("type"), str(msg.get("payload"))[:120])
                if msg.get("type") == "registered":
                    key = msg["payload"]["client-key"]
                    store.put("tv_client_key", key)
                    print("\nСПАРЕНО. client-key сохранён в home.db")
                    return
                if msg.get("type") == "error":
                    print("\nОШИБКА:", msg.get("error"))
                    return
        except asyncio.TimeoutError:
            print("\nтелевизор не ответил за 60 с — диалог не подтверждён")
        finally:
            presser.cancel()


if __name__ == "__main__":
    asyncio.run(main())
