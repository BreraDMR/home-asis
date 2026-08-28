"""Round-the-clock timelapse recorder — the phone as a dashcam for the room.

One frame every N seconds, forever, oldest frames dropped once the archive
hits its size cap. That's the whole idea: you never start a recording, you
just go back and look at what was already there.

Frames live on disk, not in the database:

    frames/2026-08-28/23/1405.jpg     <- 23:14:05

The filesystem is the index. Picking a time window means reading one or two
hour directories, which is cheap, and nothing goes stale if the bot is killed
mid-write. home.db stays small and backup-friendly.

Why not video: ffmpeg on this phone is installed but refuses to run at all
(Termux's 8.1.2 build against Android 7 — it exits silently, no output, not
even for -version). So frames are resized with Pillow, and a "clip" is a GIF
built with Pillow too. Same result, one less broken dependency.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import time

from PIL import Image

import actions
import store

log = logging.getLogger("homebot.timelapse")

ROOT = os.path.expanduser("~/lab/homebot/frames")
os.makedirs(ROOT, exist_ok=True)

# what the buttons offer, in seconds
INTERVALS = (5, 10, 15, 20, 30, 60)
DEFAULT_INTERVAL = 20

# Sizes the archive is allowed to take. 5 GB at 20 s and 1280 px is roughly
# two and a half weeks of history; the phone has ~8 GB free.
DEFAULT_LIMIT_MB = 5120
DEFAULT_WIDTH = 1280
DEFAULT_QUALITY = 75

# Never fill the card completely — if the phone drops below this much free
# space we trim harder than the configured cap, whatever the setting says.
MIN_FREE_MB = 800

# A GIF with hundreds of frames is unusable and slow to build, so long windows
# get thinned out to this many frames.
GIF_MAX_FRAMES = 60


# ---------------------------------------------------------------- settings

def _int(key: str, default: int) -> int:
    try:
        return int(store.get(key) or default)
    except (TypeError, ValueError):
        return default


def interval() -> int:
    v = _int("tl_interval", DEFAULT_INTERVAL)
    return v if v in INTERVALS else DEFAULT_INTERVAL


def camera() -> str:
    return store.get("tl_camera", "0") or "0"


def width() -> int:
    return _int("tl_width", DEFAULT_WIDTH)


def quality() -> int:
    return _int("tl_quality", DEFAULT_QUALITY)


def limit_mb() -> int:
    return _int("tl_limit_mb", DEFAULT_LIMIT_MB)


def rotate() -> int:
    """Degrees to turn each frame. The phone usually lies on its side."""
    return _int("tl_rotate", 0) % 360


def enabled() -> bool:
    return store.flag("tl_on", True)


# ---------------------------------------------------------------- paths

def _path_for(ts: float) -> str:
    t = dt.datetime.fromtimestamp(ts)
    return os.path.join(ROOT, t.strftime("%Y-%m-%d"), t.strftime("%H"),
                        t.strftime("%M%S") + ".jpg")


def _ts_of(path: str) -> float | None:
    """Rebuild the timestamp from the path we wrote it to."""
    try:
        rel = os.path.relpath(path, ROOT)
        day, hour, name = rel.split(os.sep)
        stamp = f"{day} {hour}:{name[:2]}:{name[2:4]}"
        return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def _hour_dirs() -> list[str]:
    """Every hour directory we have, oldest first."""
    out = []
    for day in sorted(os.listdir(ROOT)) if os.path.isdir(ROOT) else []:
        dpath = os.path.join(ROOT, day)
        if not os.path.isdir(dpath):
            continue
        for hour in sorted(os.listdir(dpath)):
            hpath = os.path.join(dpath, hour)
            if os.path.isdir(hpath):
                out.append(hpath)
    return out


def _frames_in(hour_dir: str) -> list[tuple[float, str]]:
    out = []
    try:
        for entry in os.scandir(hour_dir):
            if not entry.name.endswith(".jpg"):
                continue
            ts = _ts_of(entry.path)
            if ts is not None:
                out.append((ts, entry.path))
    except OSError:
        return []
    out.sort()
    return out


# ---------------------------------------------------------------- the ring

_size_bytes = 0        # running total, recomputed at startup
_size_known = False


def _measure() -> int:
    total = 0
    for hour_dir in _hour_dirs():
        try:
            for entry in os.scandir(hour_dir):
                if entry.is_file():
                    total += entry.stat().st_size
        except OSError:
            pass
    return total


def _free_mb() -> int:
    try:
        return shutil.disk_usage(ROOT).free // (1024 * 1024)
    except OSError:
        return 10_000  # can't tell — assume there's room, the cap still applies


def archive_size_mb() -> int:
    global _size_bytes, _size_known
    if not _size_known:
        _size_bytes = _measure()
        _size_known = True
    return _size_bytes // (1024 * 1024)


def _trim() -> int:
    """Drop the oldest frames until we're back under the cap. Returns how many."""
    global _size_bytes
    cap = limit_mb() * 1024 * 1024
    dropped = 0
    hours = _hour_dirs()
    while hours and (_size_bytes > cap or _free_mb() < MIN_FREE_MB):
        oldest = hours.pop(0)
        for _, path in _frames_in(oldest):
            try:
                _size_bytes -= os.path.getsize(path)
                os.remove(path)
                dropped += 1
            except OSError:
                pass
        # tidy up the empty hour, and the day once its last hour is gone
        for d in (oldest, os.path.dirname(oldest)):
            try:
                os.rmdir(d)
            except OSError:
                pass
    return dropped


# ---------------------------------------------------------------- capture

def _shrink(src: str, dst: str) -> int:
    """Resize and re-encode one frame. Returns the size written, 0 on failure.

    draft() lets libjpeg decode straight to a smaller size, which on this
    phone is the difference between ~1.5 s and ~0.5 s per frame.
    """
    try:
        target = width()
        im = Image.open(src)
        im.draft("RGB", (target, target))
        im = im.convert("RGB")
        im.thumbnail((target, target), Image.LANCZOS)
        deg = rotate()
        if deg:
            im = im.rotate(-deg, expand=True)  # clockwise, like the phone lies
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        im.save(dst, "JPEG", quality=quality(), optimize=True)
        return os.path.getsize(dst)
    except Exception as e:
        log.warning("кадр не обработался: %s", e)
        return 0


async def capture_one() -> str | None:
    """Take one frame into the archive. Returns its path, or None."""
    global _size_bytes
    raw = await actions.photo(camera())
    if not raw:
        return None
    ts = time.time()
    dst = _path_for(ts)
    written = await asyncio.to_thread(_shrink, raw, dst)
    try:
        os.remove(raw)
    except OSError:
        pass
    if not written:
        return None
    archive_size_mb()   # make sure the running total is initialised
    _size_bytes += written
    return dst


async def recorder(send=None) -> None:
    """The forever loop. Started once at boot alongside the watchers."""
    fails = 0
    while True:
        if not enabled():
            await asyncio.sleep(5)
            continue
        started = time.time()
        try:
            path = await capture_one()
            if path:
                fails = 0
                if _trim():
                    log.info("кольцо: подчистил старые кадры, архив %d МБ", archive_size_mb())
            else:
                fails += 1
                # The camera is a shared, exclusive resource: a manual photo, a
                # phone call, anything can hold it. A few misses are normal.
                if fails == 20 and send:
                    await send("📹 Таймлапс не получает кадры уже 20 попыток подряд — "
                               "камера чем-то занята или отозвано разрешение.")
        except Exception as e:
            log.warning("таймлапс: %s", e)
            fails += 1
        # keep the grid even when a shot took three seconds
        rest = interval() - (time.time() - started)
        await asyncio.sleep(max(1.0, rest))


# ---------------------------------------------------------------- reading back

def frames_between(t0: float, t1: float) -> list[tuple[float, str]]:
    """Every frame in [t0, t1], oldest first."""
    out: list[tuple[float, str]] = []
    cur = dt.datetime.fromtimestamp(t0).replace(minute=0, second=0, microsecond=0)
    end = dt.datetime.fromtimestamp(t1)
    while cur <= end:
        hour_dir = os.path.join(ROOT, cur.strftime("%Y-%m-%d"), cur.strftime("%H"))
        if os.path.isdir(hour_dir):
            out.extend((ts, p) for ts, p in _frames_in(hour_dir) if t0 <= ts <= t1)
        cur += dt.timedelta(hours=1)
    out.sort()
    return out


def nearest(target: float, count: int = 3) -> list[tuple[float, str]]:
    """The `count` frames closest to a moment in time, in time order.

    Searches an ever-wider window instead of walking the whole archive — at a
    60 s interval the third-nearest frame can still be two minutes away.
    """
    for span in (120, 600, 3600, 6 * 3600):
        window = frames_between(target - span, target + span)
        if len(window) >= count or span == 6 * 3600:
            window.sort(key=lambda f: abs(f[0] - target))
            picked = window[:count]
            picked.sort()
            return picked
    return []


def bounds() -> tuple[float, float] | None:
    """(oldest, newest) frame timestamps, or None if the archive is empty."""
    hours = _hour_dirs()
    if not hours:
        return None
    first = _frames_in(hours[0])
    last = _frames_in(hours[-1])
    if not first or not last:
        return None
    return first[0][0], last[-1][0]


def count() -> int:
    return sum(len(_frames_in(h)) for h in _hour_dirs())


# ---------------------------------------------------------------- clips

def _build_gif(frames: list[tuple[float, str]], out: str, px: int, ms: int) -> bool:
    imgs = []
    for _, path in frames:
        try:
            im = Image.open(path)
            im.draft("RGB", (px, px))
            im = im.convert("RGB")
            im.thumbnail((px, px), Image.LANCZOS)
            imgs.append(im.convert("P", palette=Image.ADAPTIVE, colors=64))
        except Exception:
            continue
    if not imgs:
        return False
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=ms,
                 loop=0, optimize=True)
    return os.path.getsize(out) > 0


async def clip(seconds: int, px: int | None = None, fps: int = 6) -> tuple[str | None, int, int]:
    """A GIF of the last `seconds` of the archive.

    Returns (path, frames used, frames available). The two counts differ when
    the window held more frames than a sane GIF can carry.
    """
    now = time.time()
    frames = frames_between(now - seconds, now)
    available = len(frames)
    if not frames:
        return None, 0, 0
    if available > GIF_MAX_FRAMES:
        step = available / GIF_MAX_FRAMES
        frames = [frames[int(i * step)] for i in range(GIF_MAX_FRAMES)]
    if px is None:
        # GIF has no interframe compression worth the name, so a long clip has
        # to trade resolution for size or it won't fit through Telegram.
        px = 640 if len(frames) <= 20 else (512 if len(frames) <= 40 else 400)
    out = os.path.join(actions.TMP, f"clip_{int(now)}.gif")
    ok = await asyncio.to_thread(_build_gif, frames, out, px, max(40, 1000 // fps))
    return (out if ok else None), len(frames), available
