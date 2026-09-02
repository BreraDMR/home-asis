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

# Where frames live. Internal storage is always there; the memory card is a
# bonus that may or may not be in the slot right now, so nothing depends on it.
#
# Android only lets an app write to its own corner of a removable card —
# /storage/<UUID>/Android/data/com.termux/files — and termux-setup-storage
# parks a symlink at ~/storage/external-1 pointing exactly there. That symlink
# is the whole detection story; if it isn't there, storage permission was never
# granted and we stay on internal.
INTERNAL = os.path.expanduser("~/lab/homebot/frames")
os.makedirs(INTERNAL, exist_ok=True)

EXTERNAL_LINK = os.path.expanduser("~/storage/external-1")

# Probing the card means touching the filesystem, and the recorder asks where
# to write on every tick. Once a minute is often enough to notice a card that
# was pulled out.
_CARD_RECHECK = 60.0
_card_at = 0.0
_card_path: str | None = None


def _probe_card() -> str | None:
    """The frames directory on the card, if the card is in and we can write.

    Writability is checked by actually writing: the mount can be there and
    still be read-only (card write-protected, or the FUSE view not handed to
    us), and finding that out on the first frame is too late.
    """
    base = EXTERNAL_LINK
    if not os.path.isdir(base):
        # the symlink is the normal route, but a card mounted after the fact
        # shows up under /storage/XXXX-XXXX all the same
        for entry in ("/storage",):
            try:
                for name in os.listdir(entry):
                    if len(name) == 9 and name[4] == "-":
                        cand = os.path.join(entry, name, "Android", "data",
                                            "com.termux", "files")
                        if os.path.isdir(cand):
                            base = cand
                            break
            except OSError:
                pass
        if not os.path.isdir(base):
            return None
    path = os.path.join(base, "frames")
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".writable")
        with open(probe, "w") as fh:
            fh.write("1")
        os.remove(probe)
        return path
    except OSError:
        return None


def card_root(force: bool = False) -> str | None:
    global _card_at, _card_path
    if force or time.time() - _card_at > _CARD_RECHECK:
        _card_path = _probe_card()
        _card_at = time.time()
    return _card_path


def use_card() -> bool:
    """Should new frames go to the card when one is available?"""
    return store.flag("tl_use_card", True)


def ROOT_write() -> str:
    """Where the next frame gets written."""
    if use_card():
        card = card_root()
        if card:
            return card
    return INTERNAL


def _roots() -> list[str]:
    """Every place frames may be read from, so an archive split across a card
    swap still reads as one timeline."""
    out = [INTERNAL]
    card = card_root()
    if card and card not in out:
        out.append(card)
    return [r for r in out if os.path.isdir(r)]

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


def limit_steps() -> list[int]:
    """What the "archive limit" button cycles through.

    The big sizes only appear once a card is actually in — offering 25 GB on a
    phone with 6 GB free would just be a way to fill the thing up.
    """
    steps = [1024, 2048, 5120, 7168]
    card = card_root()
    if card:
        try:
            free_gb = shutil.disk_usage(card).free // (1024 ** 3)
        except OSError:
            free_gb = 0
        for gb in (10, 15, 20, 25, 30, 40, 60):
            if gb + 2 <= free_gb + limit_mb() // 1024:
                steps.append(gb * 1024)
    return steps


def storage_note() -> str:
    """One line for the settings screen: where frames go and how much room is left."""
    card = card_root()
    where = ROOT_write()
    free = _free_mb()
    if where == INTERNAL:
        place = "память телефона"
        if card and not use_card():
            place += " (карта есть, но выключена)"
        elif not card:
            place += " (карты нет)"
    else:
        place = "карта памяти"
    return f"{place}, свободно {free // 1024} ГБ" if free >= 1024 else \
           f"{place}, свободно {free} МБ"


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

def _path_for(ts: float, cam: str, seq: int = 0) -> str:
    """…/2026-08-29/10/3948_1.jpg — the camera id is part of the name.

    Without it, switching cameras silently mixed two different views into one
    archive: a day-long clip pulled mostly yesterday's frames from the back
    camera and looked like the setting hadn't applied at all.

    `seq` only kicks in when two frames land in the same second — a photo taken
    by hand right as the recorder fires. It goes before the camera id so the
    name still parses the same way at both ends.
    """
    t = dt.datetime.fromtimestamp(ts)
    tail = t.strftime("%M%S") + (f"-{seq}" if seq else "")
    return os.path.join(ROOT_write(), t.strftime("%Y-%m-%d"), t.strftime("%H"),
                        f"{tail}_{cam}.jpg")


def _free_path(ts: float, cam: str) -> str:
    """A path for this second that nothing is sitting on yet."""
    for seq in range(20):
        path = _path_for(ts, cam, seq)
        if not os.path.exists(path):
            return path
    return _path_for(ts, cam, 99)


def _cam_of(path: str) -> str:
    """Which camera took this frame. Files from before the rename are back-camera."""
    name = os.path.basename(path)
    if "_" in name:
        return name.split("_", 1)[1].split(".", 1)[0]
    return "0"


def _ts_of(path: str) -> float | None:
    """Rebuild the timestamp from the last three parts of the path.

    Read off the tail rather than relative to a root, because the archive can
    now be spread over two of them — internal storage and the card.
    """
    try:
        parts = path.split(os.sep)
        day, hour, name = parts[-3], parts[-2], parts[-1]
        stamp = f"{day} {hour}:{name[:2]}:{name[2:4]}"
        return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def _hour_dirs() -> list[str]:
    """Every hour directory we have, oldest first, across every root.

    A card swap or a switch back to internal storage leaves frames in both
    places; sorting by (day, hour) rather than by root keeps them one timeline,
    so clips still span the seam and the ring still drops the genuinely oldest
    frames first.
    """
    found: list[tuple[tuple[str, str], str]] = []
    for root in _roots():
        try:
            days = sorted(os.listdir(root))
        except OSError:
            continue
        for day in days:
            dpath = os.path.join(root, day)
            if not os.path.isdir(dpath):
                continue
            try:
                hours = sorted(os.listdir(dpath))
            except OSError:
                continue
            for hour in hours:
                hpath = os.path.join(dpath, hour)
                if os.path.isdir(hpath):
                    found.append(((day, hour), hpath))
    found.sort(key=lambda item: item[0])
    return [p for _, p in found]


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
        return shutil.disk_usage(ROOT_write()).free // (1024 * 1024)
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


async def prepare(src: str, ts: float | None = None) -> str:
    """Run a one-off photo through the same mill as an archive frame.

    Rotation and the timestamp live here, not in the recorder, so a photo you
    asked for by hand comes out the same way up and with the same clock on it
    as the ones the timelapse saves by itself. Falls back to the untouched
    file if anything goes wrong — better a sideways photo than none.
    """
    ts = ts or time.time()
    dst = src.rsplit(".", 1)[0] + "_ready.jpg"
    written = await asyncio.to_thread(_shrink, src, dst, ts)
    if not written:
        return src
    try:
        os.remove(src)
    except OSError:
        pass
    return dst


def keep_manual() -> bool:
    """Do photos taken by hand get filed into the archive too?"""
    return store.flag("tl_keep_manual", True)


def archive_shot(path: str, cam: str, ts: float | None = None) -> str | None:
    """File a hand-taken photo into the archive. Returns where it landed.

    Until this existed, a photo you asked for by hand was sent to the chat and
    deleted — so pressing the camera button twenty times left nothing behind,
    and none of it showed up in a clip. It goes in under the camera that took
    it, which means a shot from the back camera turns up in back-camera clips,
    not in whatever the recorder happens to be filming.

    The file is already through prepare(), so it's the same size, rotation and
    timestamp as everything the recorder saves.
    """
    global _size_bytes
    if not keep_manual():
        return None
    ts = ts or time.time()
    dst = _free_path(ts, cam)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(path, dst)
    except OSError as e:
        log.warning("ручной кадр не лёг в архив: %s", e)
        return None
    archive_size_mb()       # make sure the running total is initialised
    try:
        _size_bytes += os.path.getsize(dst)
    except OSError:
        pass
    return dst


def stamp_existing(cam: str | None = "current") -> tuple[int, int]:
    """Burn the time into frames that were saved before stamping existed.

    Two markers, not one, and the difference matters: `tl_stamp_since` is when
    the recorder started stamping frames as it saved them, and everything
    newer than that is already done. `tl_stamp_retro` is how far back-filling
    has got. Retro work happens strictly between them, so no frame is ever
    re-encoded twice and none is skipped — the first version used a single
    marker, the recorder pushed it to "now", and the old frames it was
    supposed to fix ended up on the wrong side of it.

    Times come from the filename, so they're exact: file mtimes would lie
    after any copy.
    """
    if cam == "current":
        cam = camera()
    since = float(store.get("tl_stamp_since", "0") or 0) or time.time()
    retro_key = f"tl_stamp_retro_{cam or 'all'}"
    from_ts = float(store.get(retro_key, "0") or 0)
    stamped_now, failed, newest = 0, 0, from_ts
    for hour_dir in _hour_dirs():
        for ts, path in _frames_in(hour_dir, cam):
            if ts <= from_ts or ts >= since:
                continue
            try:
                im = Image.open(path).convert("RGB")
                im = _stamp(im, ts)
                im.save(path, "JPEG", quality=quality(), optimize=True)
                stamped_now += 1
                newest = max(newest, ts)
            except Exception:
                failed += 1
    if newest > from_ts:
        store.put(retro_key, f"{newest:.0f}")
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
    dst = _free_path(ts, cam)
    written = await asyncio.to_thread(_shrink, raw, dst, ts)
    try:
        os.remove(raw)
    except OSError:
        pass
    if not written:
        return None
    _last_kept = ts
    if stamped() and not store.get("tl_stamp_since"):
        # the moment stamping went live: everything from here on is stamped as
        # it's saved, everything before it is the back-filler's job
        store.put("tl_stamp_since", f"{ts:.0f}")
    archive_size_mb()   # make sure the running total is initialised
    _size_bytes += written
    return dst


async def recorder(send=None) -> None:
    """The forever loop. Started once at boot alongside the watchers."""
    fails = 0
    where = ROOT_write()
    while True:
        if not enabled():
            await asyncio.sleep(5)
            continue
        started = time.time()
        # a card that falls out mid-recording just quietly stops being the
        # write target, and you'd only find out days later by the archive
        # having gone shallow — say so the moment it happens
        now_where = ROOT_write()
        if now_where != where:
            moved = ("на карту памяти" if now_where != INTERNAL
                     else "во внутреннюю память — карты больше не видно")
            log.warning("таймлапс пишет теперь %s", moved)
            if send:
                await send(f"📹 Таймлапс переехал {moved}.")
            where = now_where
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
        for root in _roots():
            hour_dir = os.path.join(root, cur.strftime("%Y-%m-%d"), cur.strftime("%H"))
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

    # the same hour can exist under two roots at once (a card swap mid-hour),
    # so take the extremes of every directory sharing the edge hour
    def key(h: str) -> tuple[str, str]:
        return tuple(h.split(os.sep)[-2:])

    first = [ts for h in hours if key(h) == key(hours[0]) for ts, _ in _frames_in(h, cam)]
    last = [ts for h in hours if key(h) == key(hours[-1]) for ts, _ in _frames_in(h, cam)]
    if not first or not last:
        return None
    return min(first), max(last)


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


def _clip_shape(frames: list[tuple[float, str]], px: int) -> tuple[int, int]:
    """How big the video should be: the shape most of the frames have.

    ffmpeg takes the geometry of the *first* frame in the sequence and scales
    everything else to it. One leftover frame from an older rotation was
    enough to make a whole day of landscape look squeezed into a portrait
    video, so the majority decides, and the odd one out gets padded below.
    """
    seen: dict[tuple[int, int], int] = {}
    # opening a jpeg only reads its header, but a day is a few hundred frames
    # and the phone is slow — a spread-out sample says the same thing
    step = max(1, len(frames) // 80)
    for _, path in frames[::step]:
        try:
            with Image.open(path) as im:
                seen[im.size] = seen.get(im.size, 0) + 1
        except Exception:
            continue
    if not seen:
        return px, px * 3 // 4
    w, h = max(seen.items(), key=lambda kv: kv[1])[0]
    height = max(2, round(px * h / w))
    return px - px % 2, height - height % 2


async def _build_mp4(frames: list[tuple[float, str]], out: str, px: int, fps: int) -> bool:
    """Hand the frames to ffmpeg as a numbered sequence of symlinks.

    ffmpeg wants %06d.jpg, the archive is laid out by date — symlinks bridge
    that for free, no copying of a few hundred megabytes involved.
    """
    seq = tempfile.mkdtemp(prefix="seq_", dir=actions.TMP)
    w, h = _clip_shape(frames, px)
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
            # fit-and-pad instead of a plain scale: a frame of a different
            # shape gets black bars, it doesn't get stretched.
            # a dark room is mostly sensor noise, and noise is what makes an
            # otherwise tiny timelapse balloon — denoise before encoding
            "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,hqdn3d=4:3:6:4"),
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
