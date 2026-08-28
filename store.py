"""SQLite state: device/network labels, last seen state, watcher settings.

Small enough that a single file with plain sqlite3 beats dragging in an ORM.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("HOMEBOT_DB", os.path.expanduser("~/lab/homebot/home.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS devices (
            mac TEXT PRIMARY KEY,
            label TEXT,
            last_ip TEXT,
            first_seen TEXT,
            last_seen TEXT,
            online INTEGER DEFAULT 0,
            notify INTEGER DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS networks (
            bssid TEXT PRIMARY KEY,
            ssid TEXT,
            label TEXT,
            first_seen TEXT,
            last_seen TEXT,
            present INTEGER DEFAULT 0,
            notify INTEGER DEFAULT 1,
            rssi INTEGER,
            freq INTEGER
        )""")
        # older databases predate the signal columns
        for col, ddl in (("rssi", "INTEGER"), ("freq", "INTEGER")):
            try:
                c.execute(f"ALTER TABLE networks ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
        c.execute("""CREATE TABLE IF NOT EXISTS macros (
            name TEXT PRIMARY KEY,
            steps TEXT NOT NULL,
            created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        # One row per came/went event. That's all the "who's home" chart needs:
        # the bars between two events are just the state that held in between.
        c.execute("""CREATE TABLE IF NOT EXISTS presence (
            ts INTEGER NOT NULL,
            mac TEXT NOT NULL,
            online INTEGER NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS presence_ts ON presence(ts)")


def get(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def put(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def flag(key: str, default: bool = True) -> bool:
    v = get(f"flag:{key}")
    return default if v is None else v == "1"


def set_flag(key: str, on: bool) -> None:
    put(f"flag:{key}", "1" if on else "0")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------- devices on the LAN ----------

def seen_devices(found: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Feed in {mac: ip}. Returns (appeared, disappeared) as row dicts."""
    appeared, gone = [], []
    stamp = int(time.time())
    with conn() as c:
        known = {r["mac"]: dict(r) for r in c.execute("SELECT * FROM devices")}
        for mac, ip in found.items():
            row = known.get(mac)
            if row is None:
                c.execute(
                    "INSERT INTO devices(mac,label,last_ip,first_seen,last_seen,online) VALUES(?,?,?,?,?,1)",
                    (mac, None, ip, now(), now()),
                )
                c.execute("INSERT INTO presence(ts,mac,online) VALUES(?,?,1)", (stamp, mac))
                appeared.append({"mac": mac, "label": None, "last_ip": ip, "is_new": True})
            else:
                if not row["online"]:
                    c.execute("INSERT INTO presence(ts,mac,online) VALUES(?,?,1)", (stamp, mac))
                    appeared.append({**row, "last_ip": ip, "is_new": False})
                c.execute(
                    "UPDATE devices SET last_ip=?, last_seen=?, online=1 WHERE mac=?",
                    (ip, now(), mac),
                )
        for mac, row in known.items():
            if row["online"] and mac not in found:
                c.execute("UPDATE devices SET online=0 WHERE mac=?", (mac,))
                c.execute("INSERT INTO presence(ts,mac,online) VALUES(?,?,0)", (stamp, mac))
                gone.append(row)
    return appeared, gone


def presence_since(since_ts: int) -> list[dict]:
    """Came/went events newer than a timestamp, oldest first."""
    with conn() as c:
        rows = c.execute(
            "SELECT ts,mac,online FROM presence WHERE ts>=? ORDER BY ts", (since_ts,)
        )
        return [dict(r) for r in rows]


def presence_state_at(ts: int) -> dict[str, bool]:
    """Who was online at a given moment — the last event before it, per device.

    Needed because a device that has been home for three days has no event
    inside the chart window, and would otherwise be drawn as absent.
    """
    with conn() as c:
        rows = c.execute(
            "SELECT mac, online FROM presence p WHERE ts = ("
            "  SELECT MAX(ts) FROM presence WHERE mac = p.mac AND ts < ?"
            ") GROUP BY mac", (ts,)
        )
        return {r["mac"]: bool(r["online"]) for r in rows}


def presence_trim(keep_days: int = 30) -> None:
    cutoff = int(time.time()) - keep_days * 86400
    with conn() as c:
        c.execute("DELETE FROM presence WHERE ts < ?", (cutoff,))


def device_list(online_only: bool = False) -> list[dict]:
    with conn() as c:
        q = "SELECT * FROM devices"
        if online_only:
            q += " WHERE online=1"
        q += " ORDER BY online DESC, COALESCE(label,mac)"
        return [dict(r) for r in c.execute(q)]


def label_device(mac: str, label: str) -> None:
    with conn() as c:
        c.execute("UPDATE devices SET label=? WHERE mac=?", (label, mac))


# ---------- wifi networks around ----------

def seen_networks(found: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Feed in {bssid: {ssid, rssi, freq}}. Same contract as seen_devices."""
    appeared, gone = [], []
    with conn() as c:
        known = {r["bssid"]: dict(r) for r in c.execute("SELECT * FROM networks")}
        for bssid, info in found.items():
            ssid, rssi, freq = info.get("ssid"), info.get("rssi"), info.get("freq")
            row = known.get(bssid)
            if row is None:
                c.execute(
                    "INSERT INTO networks(bssid,ssid,label,first_seen,last_seen,present,rssi,freq)"
                    " VALUES(?,?,?,?,?,1,?,?)",
                    (bssid, ssid, None, now(), now(), rssi, freq),
                )
                appeared.append({"bssid": bssid, "ssid": ssid, "label": None,
                                 "rssi": rssi, "freq": freq, "is_new": True})
            else:
                if not row["present"]:
                    appeared.append({**row, "ssid": ssid, "rssi": rssi, "freq": freq, "is_new": False})
                c.execute(
                    "UPDATE networks SET ssid=?, last_seen=?, present=1, rssi=?, freq=? WHERE bssid=?",
                    (ssid, now(), rssi, freq, bssid),
                )
        for bssid, row in known.items():
            if row["present"] and bssid not in found:
                c.execute("UPDATE networks SET present=0 WHERE bssid=?", (bssid,))
                gone.append(row)
    return appeared, gone


def network_list(present_only: bool = False) -> list[dict]:
    with conn() as c:
        q = "SELECT * FROM networks"
        if present_only:
            q += " WHERE present=1"
        q += " ORDER BY present DESC, COALESCE(label,ssid,bssid)"
        return [dict(r) for r in c.execute(q)]


def label_network(bssid: str, label: str) -> None:
    with conn() as c:
        c.execute("UPDATE networks SET label=? WHERE bssid=?", (label, bssid))


# ---------- IR macros ----------

def save_macro(name: str, steps: list[str]) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO macros(name,steps,created_at) VALUES(?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET steps=excluded.steps",
            (name, ",".join(steps), now()),
        )


def macro_list() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM macros ORDER BY name")]


def macro_steps(name: str) -> list[str]:
    with conn() as c:
        row = c.execute("SELECT steps FROM macros WHERE name=?", (name,)).fetchone()
        return row["steps"].split(",") if row else []


def delete_macro(name: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM macros WHERE name=?", (name,))
