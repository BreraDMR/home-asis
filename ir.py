"""Infrared remote for the living-room LG TV (50LF652V, 2015).

LG uses the NEC protocol at 38 kHz with address 0x20DF, so every button is a
32-bit code. termux-infrared-transmit doesn't take codes though — it wants the
raw on/off durations in microseconds, so we expand the code into a burst
pattern ourselves.

STATUS: confirmed working on 2026-08-26 — power, mute, volume, OK, menu and
input all responded on the real set. Power sometimes needs a second press,
which is the TV's own quirk, not ours.
"""
from __future__ import annotations

import asyncio

from actions import run

CARRIER_HZ = 38000

# NEC timings in microseconds
HDR_MARK, HDR_SPACE = 9000, 4500
BIT_MARK = 560
ONE_SPACE, ZERO_SPACE = 1690, 560
STOP_MARK = 560

# Standard LG TV codes (address 0x20DF). Same set works across most LG sets
# of that era, including the LF6xx series.
LG_CODES: dict[str, int] = {
    "power":      0x20DF10EF,
    "vol_up":     0x20DF40BF,
    "vol_down":   0x20DFC03F,
    "mute":       0x20DF906F,
    "ch_up":      0x20DF00FF,
    "ch_down":    0x20DF807F,
    "input":      0x20DFD02F,
    "menu":       0x20DFC23D,
    "home":       0x20DF3EC1,
    "back":       0x20DF14EB,
    "exit":       0x20DFDA25,
    "ok":         0x20DF22DD,
    "up":         0x20DF02FD,
    "down":       0x20DF827D,
    "left":       0x20DFE01F,
    "right":      0x20DF609F,
    "play":       0x20DFBD42,
    "pause":      0x20DF5DA2,
    "stop":       0x20DF8D72,
    "energy":     0x20DFA956,
}


def nec_pattern(code: int) -> list[int]:
    """Expand a 32-bit NEC code into alternating mark/space durations."""
    out = [HDR_MARK, HDR_SPACE]
    for i in range(31, -1, -1):
        bit = (code >> i) & 1
        out.append(BIT_MARK)
        out.append(ONE_SPACE if bit else ZERO_SPACE)
    out.append(STOP_MARK)
    return out


async def send(button: str) -> tuple[bool, str]:
    """Fire one button. Returns (ok, message)."""
    code = LG_CODES.get(button)
    if code is None:
        return False, f"неизвестная кнопка: {button}"
    pattern = ",".join(str(x) for x in nec_pattern(code))
    rc, out, err = await run(
        ["termux-infrared-transmit", "-f", str(CARRIER_HZ), pattern], timeout=20
    )
    if rc != 0:
        return False, (err or out or "ИК-передатчик не ответил").strip()[:200]
    return True, "отправлено"


async def play_macro(steps: list[str], gap: float = 0.7) -> tuple[int, list[str]]:
    """Fire a saved sequence of buttons. Returns (sent, failures)."""
    sent, failed = 0, []
    for step in steps:
        ok, msg = await send(step)
        if ok:
            sent += 1
        else:
            failed.append(f"{step}: {msg}")
        await asyncio.sleep(gap)
    return sent, failed
