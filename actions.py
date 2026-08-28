"""Thin wrappers around the termux-api command line tools.

Everything here shells out to a termux-* binary. They're slow-ish (each call
spins up an Android intent), so nothing here is called in a tight loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time

TMP = os.path.expanduser("~/lab/homebot/tmp")
os.makedirs(TMP, exist_ok=True)


async def run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command, never raise. Returns (code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:  # missing binary, permission, whatever
        return -1, "", str(e)


async def run_json(cmd: list[str], timeout: int = 30):
    code, out, err = await run(cmd, timeout)
    if code != 0 and not out.strip():
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


# ---------- camera ----------

# Android hands the camera to one caller at a time. Now that the timelapse
# recorder is asking for a frame every few seconds, a manual photo would land
# right on top of it and both would come back empty — so everyone queues here.
CAMERA = asyncio.Lock()


async def photo(camera_id: str = "0") -> str | None:
    """Snap a picture. Returns path or None. camera 0 = back, 1 = front."""
    path = os.path.join(TMP, f"shot_{camera_id}_{int(time.time() * 1000)}.jpg")
    async with CAMERA:
        code, _, err = await run(["termux-camera-photo", "-c", camera_id, path], timeout=45)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return None


# ---------- microphone ----------

async def record(seconds: int = 15) -> str | None:
    """Record from the mic. Returns path to the raw file (m4a)."""
    path = os.path.join(TMP, f"rec_{int(time.time())}.m4a")
    # -l limits the recording so it stops on its own; -f is the output file
    code, _, err = await run(
        ["termux-microphone-record", "-f", path, "-l", str(seconds), "-e", "aac"],
        timeout=15,
    )
    # the command returns immediately, recording happens in the background
    await asyncio.sleep(seconds + 2)
    await run(["termux-microphone-record", "-q"], timeout=15)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return None


async def to_voice(path: str) -> str | None:
    """Convert to ogg/opus so Telegram shows it as a real voice message."""
    if not shutil.which("ffmpeg"):
        return None
    out = path.rsplit(".", 1)[0] + ".ogg"
    code, _, _ = await run(
        ["ffmpeg", "-y", "-i", path, "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", out],
        timeout=120,
    )
    return out if os.path.exists(out) and os.path.getsize(out) > 0 else None


async def speech_to_text(timeout: int = 25) -> str:
    code, out, err = await run(["termux-speech-to-text"], timeout=timeout)
    return (out or err or "").strip()


# ---------- voice out ----------

async def say(text: str, lang: str = "ru") -> bool:
    code, _, _ = await run(
        ["termux-tts-speak", "-l", lang, "-s", "NOTIFICATION", text], timeout=90
    )
    return code == 0


# ---------- torch / attention ----------

async def torch(on: bool) -> None:
    await run(["termux-torch", "on" if on else "off"], timeout=15)


async def blink(times: int = 5, on_s: float = 0.35, off_s: float = 0.35) -> None:
    for _ in range(times):
        await torch(True)
        await asyncio.sleep(on_s)
        await torch(False)
        await asyncio.sleep(off_s)


async def vibrate(ms: int = 1500) -> None:
    await run(["termux-vibrate", "-d", str(ms), "-f"], timeout=15)


async def set_volume(stream: str = "music", level: int = 15) -> None:
    await run(["termux-volume", stream, str(level)], timeout=15)


async def play(path: str) -> None:
    await run(["termux-media-player", "play", path], timeout=20)


async def notify(title: str, content: str) -> None:
    await run(
        ["termux-notification", "--title", title, "--content", content, "--id", "homebot"],
        timeout=20,
    )


# ---------- sensors ----------

async def battery() -> dict:
    return await run_json(["termux-battery-status"], timeout=20) or {}


# This phone reports THREE sensors all called "LTR579 ALSPS". Asking by the
# bare name gets you the proximity one (a constant 5.0 cm), which is why the
# light reading looked frozen. The "-Wakeup Secondary" variant is the real
# ambient light channel, and its name is unique enough to match exactly.
LIGHT_SENSOR = "LTR579 ALSPS -Wakeup Secondary"


async def light_level() -> float | None:
    """Ambient light in lux, or None if the sensor really won't answer.

    termux-sensor keeps one listener per process and leaves it registered if a
    previous call died halfway; the next call then comes back empty and the bot
    used to just shrug and say "sensor busy". So: clean up any stale listener
    first, and give it a couple of tries before giving up.
    """
    for attempt in (1, 2, 3):
        data = await run_json(["termux-sensor", "-s", LIGHT_SENSOR, "-n", "3"], timeout=30)
        if data:
            for key, vals in data.items():
                if isinstance(vals, dict) and vals.get("values"):
                    return float(vals["values"][0])
        if attempt < 3:
            await run(["termux-sensor", "-c"], timeout=15)
            await asyncio.sleep(1.0)
    return None


async def motion_sample() -> float | None:
    """Magnitude of linear acceleration — near 0 when the phone sits still."""
    data = await run_json(["termux-sensor", "-s", "Linear Acceleration", "-n", "1"], timeout=25)
    if not data:
        return None
    for key, vals in data.items():
        if isinstance(vals, dict) and "values" in vals and len(vals["values"]) >= 3:
            x, y, z = vals["values"][:3]
            return (x * x + y * y + z * z) ** 0.5
    return None


async def wifi_info() -> dict:
    return await run_json(["termux-wifi-connectioninfo"], timeout=20) or {}


async def wifi_scan() -> list:
    return await run_json(["termux-wifi-scaninfo"], timeout=30) or []
