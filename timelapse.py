"""Round-the-clock timelapse recorder — the phone as a dashcam for the room.

One frame every N seconds, forever, oldest frames dropped once the archive
hits its size cap. That's the whole idea: you never start a recording, you
just go back and look at what was already there.

Frames live on disk, not in the database:

    frames/2026-08-28/23/1405_1.jpg   <- 23:14:05, front camera

The filesystem is the index. Picking a time window means reading one or two
hour directories, which is cheap, and nothing goes stale if the bot is killed
mid-write. home.db stays small and backup-friendly.

The camera id in the name matters: the two cameras look at opposite sides of
the room, and a clip that silently mixed them was the first real bug here.
Everything that reads frames back filters by camera, defaulting to the one
being recorded now.

Frames are resized with Pillow on the way in; clips are h264, encoded by
ffmpeg on the phone.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

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

# Ceiling for a video clip. A day at 20 s is 4300 frames; at 900 the day still
# plays for a minute at 15 fps and encodes in about half the time.
MAX_CLIP_FRAMES = 900

# Motion mode: how different two frames have to be before we call it movement.
# Compared on a 64-px greyscale thumbnail, so this is "average brightness
# change per pixel, 0-255" — a person walking through moves it well past 6,
# while sensor noise in a dark room sits under 2.
MOTION_LEVELS = {"low": 3.0, "mid": 6.0, "high": 12.0}
DEFAULT_MOTION = "mid"

# Even with nothing moving, keep one frame every so often. Otherwise a quiet
# night leaves a hole in the archive and you can't tell "nothing happened"
# from "the camera died".
KEEPALIVE_SECONDS = 600


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


def motion_only() -> bool:
    """Keep every frame, or only the ones where something changed."""
    return store.flag("tl_motion_only", False)


def motion_level() -> str:
    v = store.get("tl_motion_level", DEFAULT_MOTION)
    return v if v in MOTION_LEVELS else DEFAULT_MOTION


# ---------------------------------------------------------------- paths

def _path_for(ts: float, cam: str) -> str:
    """…/2026-08-29/10/3948_1.jpg — the camera id is part of the name.

    Without it, switching cameras silently mixed two different views into one
    archive: a day-long clip pulled mostly yesterday's frames from the back
    camera and looked like the setting hadn't applied at all.
    """
    t = dt.datetime.fromtimestamp(ts)
    return os.path.join(ROOT, t.strftime("%Y-%m-%d"), t.strftime("%H"),
                        f"{t.strftime('%M%S')}_{cam}.jpg")


def _cam_of(path: str) -> str:
    """Which camera took this frame. Files from before the rename are back-camera."""
    name = os.path.basename(path)
    if "_" in name:
        return name.split("_", 1)[1].split(".", 1)[0]
    return "0"


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


def _frames_in(hour_dir: str, cam: str | None = None) -> list[tuple[float, str]]:
    out = []
    try:
        for entry in os.scandir(hour_dir):
            if not entry.name.endswith(".jpg"):
                continue
            if cam is not None and _cam_of(entry.path) != cam:
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

# ---------------------------------------------------------------- timestamp

# Termux has no fontconfig, but matplotlib ships DejaVu and it's already here
# for the presence chart.
def _font_path() -> str | None:
    try:
        import matplotlib
        p = os.path.join(os.path.dirname(matplotlib.__file__),
                         "mpl-data", "fonts", "ttf", "DejaVuSans-Bold.ttf")
        return p if os.path.exists(p) else None
    except Exception:
        return None


_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int):
    if size not in _font_cache:
        path = _font_path()
        try:
            _font_cache[size] = (ImageFont.truetype(path, size) if path
                                 else ImageFont.load_default())
        except Exception:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def stamped() -> bool:
    return store.flag("tl_stamp", True)


def _stamp(im: Image.Image, ts: float) -> Image.Image:
    """Burn the date and time into the bottom-right corner.

    Burnt in rather than added as a caption on purpose: once these frames
    become a video, a caption is gone and only the pixels remain.
    """
    text = time.strftime("%d.%m.%Y  %H:%M:%S", time.localtime(ts))
    w, h = im.size
    size = max(12, int(h * 0.035))
    font = _font(size)
    draw = ImageDraw.Draw(im, "RGBA")
    try:
        box = draw.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
    except Exception:
        tw, th = len(text) * size // 2, size
    pad = max(4, size // 3)
    x, y = w - tw - pad * 2, h - th - pad * 2
    # a dark plate underneath, or midday sunlight on a white wall eats the text
    draw.rectangle([x - pad, y - pad, w - pad // 2, h - pad // 2], fill=(0, 0, 0, 140))
    draw.text((x, y - pad // 2), text, font=font, fill=(255, 255, 255, 235))
    return im


def _shrink(src: str, dst: str, ts: float) -> int:
    """Resize, stamp and re-encode one frame. Returns bytes written, 0 on failure.

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
        if stamped():
            im = _stamp(im, ts)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        im.save(dst, "JPEG", quality=quality(), optimize=True)
        return os.path.getsize(dst)
    except Exception as e:
        log.warning("кадр не обработался: %s", e)
        return 0


def stamp_existing(cam: str | None = "current") -> tuple[int, int]:
    """Burn the time into frames that were saved before stamping existed.

    Works off the timestamp in the filename, so it's exact — no guessing from
    file mtimes, which a copy or an rsync would have destroyed. Re-encoding
    costs a little quality, so each frame is only ever done once: the marker
    file next to the archive remembers where we got to.
    """
    if cam == "current":
        cam = camera()
    done_key = f"tl_stamped_upto_{cam or 'all'}"
    already = float(store.get(done_key, "0") or 0)
    stamped_now, failed = 0, 0
    newest = already
    for hour_dir in _hour_dirs():
        for ts, path in _frames_in(hour_dir, cam):
            if ts <= already:
                continue
            try:
                im = Image.open(path).convert("RGB")
                im = _stamp(im, ts)
                im.save(path, "JPEG", quality=quality(), optimize=True)
                stamped_now += 1
                newest = max(newest, ts)
            except Exception:
                failed += 1
    if newest > already:
        store.put(done_key, f"{newest:.0f}")
    return stamped_now, failed


# The last frame we looked at, as a tiny greyscale thumbnail. Kept in memory
# only — after a restart the first frame is always saved, which is fine.
_last_thumb: Image.Image | None = None
_last_kept: float = 0.0
_last_cam: str | None = None


def _thumb(path: str) -> Image.Image | None:
    """A 64-px grey thumbnail — small enough that sensor noise averages out."""
    try:
        im = Image.open(path)
        im.draft("L", (64, 64))
        im = im.convert("L")
        im.thumbnail((64, 64), Image.BILINEAR)
        return im
    except Exception:
        return None


def _difference(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute brightness change per pixel between two thumbnails."""
    if a.size != b.size:
        return 999.0
    pa, pb = a.tobytes(), b.tobytes()
    if len(pa) != len(pb) or not pa:
        return 999.0
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


def _worth_keeping(raw: str) -> tuple[bool, float]:
    """Motion mode: is this frame different enough from the previous one?"""
    global _last_thumb, _last_kept
    thumb = _thumb(raw)
    if thumb is None:
        return True, 0.0            # can't tell — keep it rather than lose it
    previous, _last_thumb = _last_thumb, thumb
    if previous is None:
        return True, 0.0
    diff = _difference(previous, thumb)
    if diff >= MOTION_LEVELS[motion_level()]:
        return True, diff
    # nothing moved, but don't leave a hole in the timeline either
    if time.time() - _last_kept >= KEEPALIVE_SECONDS:
        return True, diff
    return False, diff


# capture_one's answer when the camera worked fine and we chose not to keep
# the frame — that must not look like a camera failure to the recorder loop.
SKIPPED = "skipped"


async def capture_one() -> str | None:
    """Take one frame into the archive. Returns its path, SKIPPED, or None.

    In motion mode the camera still fires on every tick — there's no other way
    to see whether anything moved — but the frame is only written to disk when
    it differs from the one before. That doesn't save battery; it saves the
    archive from being 90% empty room, which is what makes a whole day
    watchable in a few seconds.
    """
    global _size_bytes, _last_kept, _last_thumb, _last_cam
    cam = camera()
    if cam != _last_cam:
        # the two cameras face opposite ways — comparing across a switch would
        # read as one enormous movement
        _last_thumb, _last_cam = None, cam
    raw = await actions.photo(cam)
    if not raw:
        return None
    if motion_only():
        keep, _ = await asyncio.to_thread(_worth_keeping, raw)
        if not keep:
            try:
                os.remove(raw)
            except OSError:
                pass
            return SKIPPED
    ts = time.time()
    dst = _path_for(ts, cam)
    written = await asyncio.to_thread(_shrink, raw, dst, ts)
    try:
        os.remove(raw)
    except OSError:
        pass
    if not written:
        return None
    _last_kept = ts
    if stamped():
        # tell the retro-stamper this one is already done, so it never
        # re-encodes a frame twice
        store.put(f"tl_stamped_upto_{cam}", f"{ts:.0f}")
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
            if path == SKIPPED:
                fails = 0       # camera is fine, the room just sat still
            elif path:
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

def frames_between(t0: float, t1: float, cam: str | None = "current") -> list[tuple[float, str]]:
    """Every frame in [t0, t1], oldest first.

    Defaults to the camera currently being recorded — mixing two viewpoints in
    one clip is never what you wanted.
    """
    if cam == "current":
        cam = camera()
    out: list[tuple[float, str]] = []
    cur = dt.datetime.fromtimestamp(t0).replace(minute=0, second=0, microsecond=0)
    end = dt.datetime.fromtimestamp(t1)
    while cur <= end:
        hour_dir = os.path.join(ROOT, cur.strftime("%Y-%m-%d"), cur.strftime("%H"))
        if os.path.isdir(hour_dir):
            out.extend((ts, p) for ts, p in _frames_in(hour_dir, cam) if t0 <= ts <= t1)
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


def bounds(cam: str | None = "current") -> tuple[float, float] | None:
    """(oldest, newest) frame timestamps, or None if the archive is empty."""
    if cam == "current":
        cam = camera()
    hours = [h for h in _hour_dirs() if _frames_in(h, cam)]
    if not hours:
        return None
    first = _frames_in(hours[0], cam)
    last = _frames_in(hours[-1], cam)
    if not first or not last:
        return None
    return first[0][0], last[-1][0]


def count(cam: str | None = "current") -> int:
    if cam == "current":
        cam = camera()
    return sum(len(_frames_in(h, cam)) for h in _hour_dirs())


def drop_camera(cam: str) -> tuple[int, int]:
    """Delete every frame taken with one camera. Returns (files, megabytes)."""
    global _size_bytes
    files = freed = 0
    for hour_dir in _hour_dirs():
        for _, path in _frames_in(hour_dir, cam):
            try:
                size = os.path.getsize(path)
                os.remove(path)
                files += 1
                freed += size
            except OSError:
                pass
        for d in (hour_dir, os.path.dirname(hour_dir)):
            try:
                os.rmdir(d)
            except OSError:
                pass
    if _size_known:
        _size_bytes = max(0, _size_bytes - freed)
    return files, freed // (1024 * 1024)


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


async def _build_mp4(frames: list[tuple[float, str]], out: str, px: int, fps: int) -> bool:
    """Hand the frames to ffmpeg as a numbered sequence of symlinks.

    ffmpeg wants %06d.jpg, the archive is laid out by date — symlinks bridge
    that for free, no copying of a few hundred megabytes involved.
    """
    seq = tempfile.mkdtemp(prefix="seq_", dir=actions.TMP)
    try:
        for i, (_, path) in enumerate(frames, 1):
            try:
                os.symlink(path, os.path.join(seq, f"{i:06d}.jpg"))
            except OSError:
                pass
        code, _, err = await actions.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", str(fps), "-i", os.path.join(seq, "%06d.jpg"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
            "-pix_fmt", "yuv420p",
            # a dark room is mostly sensor noise, and noise is what makes an
            # otherwise tiny timelapse balloon — denoise before encoding
            "-vf", f"scale={px}:-2,hqdn3d=4:3:6:4",
            "-movflags", "+faststart", out,
        ], timeout=900)
        if code != 0:
            log.warning("ffmpeg: %s", (err or "")[:200])
        return os.path.exists(out) and os.path.getsize(out) > 0
    finally:
        shutil.rmtree(seq, ignore_errors=True)


def _thin(frames: list, limit: int) -> list:
    if len(frames) <= limit:
        return frames
    step = len(frames) / limit
    return [frames[int(i * step)] for i in range(limit)]


async def clip(seconds: int, px: int | None = None) -> tuple[str | None, int, int, int]:
    """A video of the last `seconds` of the archive.

    Returns (path, frames used, frames available, seconds of playback). A day
    is thinned down so the clip stays watchable — nobody scrubs through four
    thousand frames in real time.
    """
    now = time.time()
    frames = frames_between(now - seconds, now)
    available = len(frames)
    if not frames:
        return None, 0, 0, 0
    frames = _thin(frames, MAX_CLIP_FRAMES)

    # aim for something you'd actually sit through: a few seconds for a short
    # window, at most a minute and a half for a whole day
    fps = 15 if len(frames) > 60 else max(2, min(8, len(frames) // 4 or 1))
    if px is None:
        px = 960 if len(frames) <= 120 else 640
    out = os.path.join(actions.TMP, f"clip_{int(now)}.mp4")
    ok = await _build_mp4(frames, out, px, fps)
    return (out if ok else None), len(frames), available, round(len(frames) / fps)
