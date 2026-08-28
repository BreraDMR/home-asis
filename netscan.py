"""Who is on the home network, and which access points are in range.

No nmap here on purpose — a plain ping sweep fills the kernel ARP table and
/proc/net/arp is readable without root, which is all we need.
"""
from __future__ import annotations

import asyncio
import re

from actions import run, wifi_info, wifi_scan

ARP = "/proc/net/arp"
# 02:00:00:00:00:00 is what Android reports for itself when it hides its mac
BAD_MAC = {"00:00:00:00:00:00", "02:00:00:00:00:00"}


async def _own_subnet() -> tuple[str, str] | None:
    """Returns (prefix like '192.168.0', our own ip) or None if wifi is off."""
    info = await wifi_info()
    ip = info.get("ip") or ""
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        return None
    return ip.rsplit(".", 1)[0], ip


async def _ping(host: str) -> None:
    # -c 1 one packet, -W 1 wait one second, -q quiet. We ignore the result:
    # the point is the ARP entry it leaves behind, not whether it replied.
    await run(["ping", "-c", "1", "-W", "1", "-q", host], timeout=4)


async def sweep(concurrency: int = 48) -> dict[str, str]:
    """Ping the whole /24, then read the ARP table. Returns {mac: ip}."""
    sub = await _own_subnet()
    if not sub:
        return {}
    prefix, own_ip = sub
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int):
        async with sem:
            await _ping(f"{prefix}.{i}")

    await asyncio.gather(*(one(i) for i in range(1, 255)))
    return await arp_table(prefix, own_ip)


async def arp_table(prefix: str | None = None, own_ip: str | None = None) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        with open(ARP) as fh:
            next(fh, None)  # header
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip, flags, mac = parts[0], parts[2], parts[3]
                if flags == "0x0" or mac.lower() in BAD_MAC:
                    continue  # incomplete entry, host never answered
                if prefix and not ip.startswith(prefix + "."):
                    continue
                found[mac.lower()] = ip
    except Exception:
        return {}
    # the phone itself never shows up in its own ARP table — add it by hand
    if own_ip:
        info = await wifi_info()
        mac = (info.get("mac_address") or info.get("bssid_self") or "").lower()
        if mac and mac not in BAD_MAC:
            found[mac] = own_ip
    return found


def distance_m(rssi: int | float | None, freq_mhz: int | float | None) -> float | None:
    """Very rough free-space distance estimate from signal strength.

    Walls, bodies and cheap antennas all lie to this formula, so treat it as
    "same room / next room / far", not as a tape measure.
    """
    if rssi is None or not freq_mhz:
        return None
    import math
    try:
        return round(10 ** ((27.55 - (20 * math.log10(float(freq_mhz))) + abs(float(rssi))) / 20), 1)
    except Exception:
        return None


def distance_label(rssi, freq) -> str:
    d = distance_m(rssi, freq)
    if d is None:
        return ""
    if d < 5:
        near = "рядом"
    elif d < 15:
        near = "в квартире"
    elif d < 40:
        near = "у соседей"
    else:
        near = "далеко"
    return f"~{d} м, {near}"


async def scan_networks() -> dict[str, dict]:
    """Access points in range: {bssid: {ssid, rssi, freq}}."""
    out: dict[str, dict] = {}
    for ap in await wifi_scan():
        bssid = (ap.get("bssid") or "").lower()
        if not bssid or bssid in BAD_MAC:
            continue
        out[bssid] = {
            "ssid": ap.get("ssid") or "(скрытая)",
            "rssi": ap.get("rssi"),
            "freq": ap.get("frequency_mhz"),
        }
    return out


async def internet_up() -> bool:
    """Is there a way out to the internet?

    Used to be a ping to 1.1.1.1, and that lied: ICMP echo replies never make
    it back to Termux on this phone (measured 28.08.2026 — even the router
    itself doesn't answer), so the bot reported "no internet" while happily
    talking to Telegram over the same wifi. A plain TCP connect tells the
    truth, and doesn't depend on ICMP being allowed anywhere along the way.
    """
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("api.telegram.org", 443)):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=4
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False
