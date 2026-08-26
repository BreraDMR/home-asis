"""Background watchers — the part that writes to you instead of waiting for a tap.

Each watcher is a plain asyncio loop started at boot. They all funnel through
one `send` callback so the bot owns all the Telegram plumbing.
"""
from __future__ import annotations

import asyncio
import time

import actions
import netscan
import store

# Damir wants to see every network, however faint — so nothing is filtered out.
# The anti-spam trick is batching: one message per scan, not one per network.
BATCH_LIMIT = 12  # if more than this appear at once, summarise instead

# how long a device may stay silent before we call it "gone".
# phones sleep their wifi, so a single missed sweep means nothing.
MISS_LIMIT = int(store.get("presence_miss_limit", "3") or 3)

_miss: dict[str, int] = {}


def _name(row: dict) -> str:
    label = row.get("label")
    if label:
        return label
    ssid = row.get("ssid")
    if ssid:
        return f"{ssid} ({row.get('bssid','')[-8:]})"
    mac = row.get("mac", "")
    ip = row.get("last_ip") or ""
    return f"{mac}{(' · ' + ip) if ip else ''}"


def _signal(row: dict) -> str:
    rssi, freq = row.get("rssi"), row.get("freq")
    if rssi is None:
        return ""
    dist = netscan.distance_label(rssi, freq)
    band = "5 ГГц" if freq and int(freq) > 3000 else "2.4 ГГц"
    return f"\n   📡 {rssi} дБм · {band}" + (f" · {dist}" if dist else "")


async def power_watcher(send, period: int = 30) -> None:
    """Charger yanked = the lights went out. The battery is our UPS."""
    last = None
    while True:
        try:
            b = await actions.battery()
            plugged = b.get("plugged", "UNPLUGGED") != "UNPLUGGED"
            if last is not None and plugged != last and store.flag("notify_power"):
                if plugged:
                    await send("⚡ Питание вернулось — телефон снова на зарядке.")
                else:
                    await send(
                        f"🔌 <b>Питание пропало!</b>\nТелефон на батарее, заряд {b.get('percentage','?')}%."
                    )
            last = plugged
            pct = b.get("percentage")
            if not plugged and isinstance(pct, int) and pct <= 15 and store.flag("notify_battery"):
                mark = store.get("low_batt_mark")
                if mark != str(pct // 5):
                    store.put("low_batt_mark", str(pct // 5))
                    await send(f"🪫 Заряд {pct}%, питания нет.")
        except Exception:
            pass
        await asyncio.sleep(period)


async def internet_watcher(send, period: int = 60) -> None:
    last = None
    while True:
        try:
            up = await netscan.internet_up()
            if last is not None and up != last and store.flag("notify_internet"):
                await send("🌐 Интернет вернулся." if up else "🌐 <b>Интернет пропал.</b>")
            last = up
        except Exception:
            pass
        await asyncio.sleep(period)


async def devices_watcher(send, period: int = 180) -> None:
    """Who is on the LAN. Appearing is instant; leaving needs MISS_LIMIT misses."""
    while True:
        try:
            found = await netscan.sweep()
            if found:
                # hold back "gone" until a device has missed several sweeps in a row
                known_online = {d["mac"] for d in store.device_list(online_only=True)}
                for mac in known_online:
                    if mac in found:
                        _miss.pop(mac, None)
                    else:
                        _miss[mac] = _miss.get(mac, 0) + 1
                still_here = {m: ip for m, ip in found.items()}
                for mac in known_online:
                    if mac not in found and _miss.get(mac, 0) < MISS_LIMIT:
                        still_here[mac] = ""  # pretend it's here for now

                appeared, gone = store.seen_devices(still_here)
                if store.flag("notify_devices"):
                    for d in appeared:
                        tag = " <i>(новое устройство)</i>" if d.get("is_new") else ""
                        await send(f"🟢 Подключилось: <b>{_name(d)}</b>{tag}")
                    for d in gone:
                        _miss.pop(d["mac"], None)
                        await send(f"🔴 Отключилось: <b>{_name(d)}</b>")
        except Exception:
            pass
        await asyncio.sleep(period)


async def networks_watcher(send, period: int = 300) -> None:
    """Access points in range — new neighbours, or your own router vanishing."""
    while True:
        try:
            found = await netscan.scan_networks()
            if found:
                appeared, gone = store.seen_networks(found)
                # Off by default now: Damir found the neighbours' routers coming
                # and going all day genuinely annoying. The log keeps everything,
                # the button shows it on demand.
                if store.flag("notify_networks", False):
                    fresh = [n for n in appeared if n.get("is_new")]
                    back = [n for n in appeared if not n.get("is_new")]
                    for title, group in (("📶 <b>Новые сети в эфире</b>", fresh),
                                         ("📶 <b>Сети вернулись</b>", back),
                                         ("📵 <b>Сети пропали</b>", gone)):
                        if not group:
                            continue
                        # strongest first, so the interesting ones are on top
                        group.sort(key=lambda n: (n.get("rssi") or -999), reverse=True)
                        if len(group) > BATCH_LIMIT:
                            head = group[:BATCH_LIMIT]
                            tail = f"\n…и ещё {len(group) - BATCH_LIMIT} — смотри 📜 Журнал сетей"
                        else:
                            head, tail = group, ""
                        body = "\n".join(f"• <b>{_name(n)}</b>{_signal(n)}" for n in head)
                        await send(f"{title}\n{body}{tail}")
        except Exception:
            pass
        await asyncio.sleep(period)


async def climate_watcher(send, period: int = 900) -> None:
    """Battery temperature as a rough room thermometer — trend, not accuracy."""
    while True:
        try:
            b = await actions.battery()
            t = b.get("temperature")
            if isinstance(t, (int, float)):
                store.put("last_temp", f"{t:.1f}")
                store.put("last_temp_at", store.now())
                hi = float(store.get("temp_alert_high", "45") or 45)
                lo = float(store.get("temp_alert_low", "5") or 5)
                if store.flag("notify_climate", False) and (t >= hi or t <= lo):
                    await send(f"🌡 Температура телефона {t:.1f} °C — вне нормы.")
        except Exception:
            pass
        await asyncio.sleep(period)


async def motion_watcher(send, period: int = 20) -> None:
    """Off by default: polling the accelerometer costs battery."""
    while True:
        try:
            if store.flag("notify_motion", False):
                m = await actions.motion_sample()
                thr = float(store.get("motion_threshold", "1.2") or 1.2)
                if m is not None and m >= thr:
                    last = float(store.get("motion_last_ts", "0") or 0)
                    if time.time() - last > 120:  # don't spam on one event
                        store.put("motion_last_ts", str(time.time()))
                        await send(f"🚶 Движение рядом с телефоном (сила {m:.1f}).")
        except Exception:
            pass
        await asyncio.sleep(period)


def start_all(send) -> list:
    return [
        asyncio.create_task(power_watcher(send)),
        asyncio.create_task(internet_watcher(send)),
        asyncio.create_task(devices_watcher(send)),
        asyncio.create_task(networks_watcher(send)),
        asyncio.create_task(climate_watcher(send)),
        asyncio.create_task(motion_watcher(send)),
    ]
