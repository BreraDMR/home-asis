"""Network remote for the living-room LG TV (50LF652V) over SSAP.

The set is wired to the router now, so we can talk to webOS directly instead
of blinking at it with the IR LED. SSAP is LG's own protocol: a WebSocket on
port 3000 carrying JSON requests. First connection triggers a prompt on the
screen; once it's accepted the TV hands back a client-key and every later
connection is silent.

This lives next to ir.py on purpose and never falls back to it — two separate
remotes, each one honest about whether it worked.
"""
from __future__ import annotations

import asyncio
import json
import socket
import uuid

import websockets

TV_IP = "192.168.1.100"
TV_MAC = "00:00:00:00:00:00"
PORT = 3000

# Everything we might ever want to do, asked for once at pairing time.
MANIFEST = {
    "manifestVersion": 1,
    "appVersion": "1.1",
    "signed": {
        "created": "20140509",
        "appId": "com.lge.test",
        "vendorId": "com.lge",
        "localizedAppNames": {"": "Home Asis"},
        "localizedVendorNames": {"": "LG Electronics"},
        "permissions": ["TEST_SECURE", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
                        "READ_INSTALLED_APPS", "READ_LGE_SDX", "READ_NOTIFICATIONS",
                        "SEARCH", "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT",
                        "CONTROL_POWER", "READ_CURRENT_CHANNEL", "READ_RUNNING_APPS",
                        "READ_UPDATE_INFO", "UPDATE_FROM_REMOTE_APP",
                        "READ_LGE_TV_INPUT_EVENTS", "READ_TV_CURRENT_TIME"],
        "serial": "2f930e2d2cfe083771f68e4fe7bb07",
    },
    "permissions": [
        "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "TEST_OPEN", "TEST_PROTECTED",
        "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK",
        "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_MEDIA_PLAYBACK",
        "CONTROL_INPUT_TV", "CONTROL_POWER", "READ_APP_STATUS", "READ_CURRENT_CHANNEL",
        "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE", "READ_RUNNING_APPS",
        "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE",
        "READ_COUNTRY_INFO", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
        "READ_INSTALLED_APPS", "WRITE_SETTINGS",
    ],
    "signatures": [{
        "signatureVersion": 1,
        "signature": "eyJhbGdvcml0aG0iOiJSU0EtU0hBMjU2Iiwia2V5SWQiOiJ0ZXN0LXNpZ25pbmctY2VydCIsIn"
                     "NpZ25hdHVyZVZlcnNpb24iOjF9.hrVRgjCwXVvE2OOSpDZ58hR+59aFNwYDyjQgKk3auukd7pcegm"
                     "E2CzPCa0bJ0ZsRAcKkCTJrWo5iDzNhMBWRyaMOv5zWSrthlf7G128qvIlpMT0YNY+n/FaOHE73uLr"
                     "S/g7swl3/qH/BGFG2Hu4RlL48eb3lLKqTt2xKHdCs6Cd4RMfJPYnzgvI4BNrFUKsjkcu+WD4OO2A2"
                     "7Pq1n50cMchmcaXadJhGrOqH5YmHdOCj5NSHzJYrsW0HPlpuAx/ECMeIZYDh6RMqaFM2DXzdKX9NmmyqzJ3o/0lkk/N97gfVRtSJw==",
    }],
}


def _pkt(uri: str, payload: dict | None = None) -> dict:
    return {"id": uuid.uuid4().hex[:8], "type": "request", "uri": uri,
            "payload": payload or {}}


class TVError(Exception):
    pass


class TV:
    """One lazy connection, reopened whenever the TV drops us."""

    def __init__(self, ip: str = TV_IP, key: str | None = None):
        self.ip = ip
        self.key = key
        self.ws = None
        self.pointer = None
        self._lock = asyncio.Lock()

    # ---------- plumbing ----------

    async def _open(self, timeout: float = 6.0) -> None:
        if self.ws is not None:
            return
        url = f"ws://{self.ip}:{PORT}"
        ws = await asyncio.wait_for(
            websockets.connect(url, ping_interval=None, max_size=4 * 1024 * 1024), timeout
        )
        payload = {"forcePairing": False, "pairingType": "PROMPT", "manifest": MANIFEST}
        if self.key:
            payload["client-key"] = self.key
        await ws.send(json.dumps({"id": "register_0", "type": "register", "payload": payload}))
        # With a key the TV answers "registered" straight away; without one it first
        # says "response" and only registers after somebody accepts on screen.
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if msg.get("type") == "registered":
                self.key = msg["payload"]["client-key"]
                self.ws = ws
                return
            if msg.get("type") == "error":
                await ws.close()
                raise TVError(msg.get("error", "телевизор отказал в подключении"))

    async def close(self) -> None:
        for sock in (self.pointer, self.ws):
            if sock is not None:
                try:
                    await sock.close()
                except Exception:
                    pass
        self.ws = self.pointer = None

    async def request(self, uri: str, payload: dict | None = None, timeout: float = 6.0) -> dict:
        """Send one SSAP request. Reconnects once if the socket went stale."""
        async with self._lock:
            for attempt in (1, 2):
                try:
                    await self._open(timeout)
                    pkt = _pkt(uri, payload)
                    await self.ws.send(json.dumps(pkt))
                    while True:
                        raw = await asyncio.wait_for(self.ws.recv(), timeout)
                        msg = json.loads(raw)
                        if msg.get("id") != pkt["id"]:
                            continue  # subscription chatter, not ours
                        if msg.get("type") == "error":
                            raise TVError(msg.get("error") or "ошибка телевизора")
                        return msg.get("payload") or {}
                except (TVError, asyncio.TimeoutError):
                    raise
                except Exception:
                    await self.close()
                    if attempt == 2:
                        raise
        raise TVError("не дозвался")

    # ---------- buttons (pointer socket) ----------

    async def _pointer_socket(self, timeout: float = 6.0):
        if self.pointer is not None:
            return self.pointer
        info = await self.request("ssap://com.webos.service.networkinput/getPointerInputSocket")
        path = info.get("socketPath")
        if not path:
            raise TVError("телевизор не дал сокет для кнопок")
        self.pointer = await asyncio.wait_for(
            websockets.connect(path, ping_interval=None), timeout
        )
        return self.pointer

    async def button(self, name: str) -> None:
        """Arrow keys, OK, BACK, HOME… go over a separate little socket."""
        for attempt in (1, 2):
            try:
                sock = await self._pointer_socket()
                await sock.send(f"type:button\nname:{name.upper()}\n\n")
                return
            except Exception:
                self.pointer = None
                if attempt == 2:
                    raise

    # ---------- the actual remote ----------

    async def volume_up(self) -> None:
        await self.request("ssap://audio/volumeUp")

    async def volume_down(self) -> None:
        await self.request("ssap://audio/volumeDown")

    async def set_volume(self, level: int) -> None:
        await self.request("ssap://audio/setVolume", {"volume": max(0, min(100, level))})

    async def volume(self) -> dict:
        return await self.request("ssap://audio/getVolume")

    async def mute(self, on: bool) -> None:
        await self.request("ssap://audio/setMute", {"mute": on})

    async def turn_off(self) -> None:
        await self.request("ssap://system/turnOff")

    async def toast(self, text: str) -> None:
        await self.request("ssap://system.notifications/createToast", {"message": text})

    async def apps(self) -> list[dict]:
        data = await self.request("ssap://com.webos.applicationManager/listLaunchPoints",
                                  timeout=10)
        return data.get("launchPoints") or []

    async def foreground(self) -> dict:
        return await self.request("ssap://com.webos.applicationManager/getForegroundAppInfo")

    async def launch(self, app_id: str, params: dict | None = None) -> None:
        payload: dict = {"id": app_id}
        if params:
            payload["params"] = params
        await self.request("ssap://system.launcher/launch", payload, timeout=10)

    async def close_app(self, app_id: str) -> None:
        await self.request("ssap://system.launcher/close", {"id": app_id}, timeout=10)

    async def inputs(self) -> list[dict]:
        data = await self.request("ssap://tv/getExternalInputList")
        return data.get("devices") or []

    async def switch_input(self, input_id: str) -> None:
        await self.request("ssap://tv/switchInput", {"inputId": input_id})

    async def channel_up(self) -> None:
        await self.request("ssap://tv/channelUp")

    async def channel_down(self) -> None:
        await self.request("ssap://tv/channelDown")

    async def media(self, what: str) -> None:
        await self.request(f"ssap://media.controls/{what}")

    async def type_text(self, text: str) -> None:
        await self.request("ssap://com.webos.service.ime/insertText",
                           {"text": text, "replace": 0})

    async def enter(self) -> None:
        await self.request("ssap://com.webos.service.ime/sendEnterKey")

    async def sw_info(self) -> dict:
        return await self.request("ssap://com.webos.service.update/getCurrentSWInformation")


# ---------- helpers that don't need a session ----------

def wake(mac: str = TV_MAC, repeat: int = 3) -> None:
    """Wake-on-LAN. Only possible now that the set is on cable — over Wi-Fi it never was.

    Some routers drop the all-ones broadcast but happily forward the subnet one,
    so we send to both, on both of the usual ports, a few times over. The TV
    still has to have "Mobile TV On" enabled or none of this reaches anything.
    """
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + raw * 16
    targets = [("255.255.255.255", 9), ("255.255.255.255", 7),
               ("192.168.1.255", 9), ("192.168.1.255", 7)]
    for _ in range(repeat):
        for host, port in targets:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                s.sendto(packet, (host, port))
            except OSError:
                pass  # no route for that broadcast, try the next one
            finally:
                s.close()


async def reachable(ip: str = TV_IP, timeout: float = 1.5) -> bool:
    """Is the TV answering on its control port at all (i.e. is it awake)?"""
    try:
        fut = asyncio.open_connection(ip, PORT)
        reader, writer = await asyncio.wait_for(fut, timeout)
        writer.close()
        return True
    except Exception:
        return False


# DIAL sits on its own port and needs no pairing, so it can report app state
# even before we're registered.
DIAL_APPS = f"http://{TV_IP}:36866/apps"

YOUTUBE_ID = "youtube.leanback.v4"


async def _dial(method: str, path: str, body: str | None = None, timeout: float = 10.0):
    """Returns (status, text) or (None, error). Runs in a thread — urllib blocks."""
    import urllib.request

    def call():
        req = urllib.request.Request(
            f"{DIAL_APPS}/{path}", method=method,
            data=body.encode() if body else None,
            headers={"Content-Type": "text/plain; charset=utf-8"} if body else {},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")

    try:
        return await asyncio.to_thread(call)
    except Exception as e:
        return None, str(e)


async def dial_state(app: str = "YouTube", timeout: float = 4.0) -> str | None:
    """'running' / 'stopped' / None if the TV didn't answer."""
    status, body = await _dial("GET", app, timeout=timeout)
    if status is None or "<state>" not in body:
        return None
    return body.split("<state>", 1)[1].split("</state>", 1)[0]


VIDEO_HOSTS = ("youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com")


def video_id(url: str) -> str | None:
    """Pull the video id out of whatever YouTube link got pasted."""
    from urllib.parse import parse_qs, urlparse

    try:
        u = urlparse(url.strip())
    except Exception:
        return None
    if u.hostname not in VIDEO_HOSTS:
        return None
    if u.hostname == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
    elif u.path.startswith(("/shorts/", "/live/", "/embed/")):
        vid = u.path.split("/")[2]
    else:
        vid = (parse_qs(u.query).get("v") or [""])[0]
    vid = vid.split("&")[0].strip()
    return vid or None


async def youtube_play(vid: str) -> bool:
    """Start the YouTube app straight on a video, skipping its slow menus."""
    status, _ = await _dial("POST", "YouTube", f"v={vid}")
    return status in (200, 201)


async def youtube_stop() -> bool:
    """Kill the app properly — this is what actually clears a stuck loader."""
    status, _ = await _dial("DELETE", "YouTube/run")
    return status in (200, 201, 204)
