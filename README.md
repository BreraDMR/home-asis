<div align="center">

# 🏠 Home Asis

**An old Android phone becomes a home hub: cameras, a rolling timelapse, a room mic, two TV remotes and watchers that message you when the power, the internet or a device changes.**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white&style=for-the-badge)](requirements.txt)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-22.x%20async-2CA5E0?logo=telegram&logoColor=white&style=for-the-badge)](requirements.txt)
[![Termux](https://img.shields.io/badge/Termux-termux--api-000000?logo=gnubash&logoColor=white&style=for-the-badge)](https://termux.dev)
[![Android](https://img.shields.io/badge/Android%207-no%20root-3DDC84?logo=android&logoColor=white&style=for-the-badge)](#requirements)
[![SQLite](https://img.shields.io/badge/SQLite-storage-003B57?logo=sqlite&logoColor=white&style=for-the-badge)](store.py)
[![webOS](https://img.shields.io/badge/webOS-SSAP%20remote-A50034?logo=lg&logoColor=white&style=for-the-badge)](tv.py)

</div>

A Telegram bot that turns an old Android phone into a small home hub.

The phone sits on a charger somewhere in the flat with [Termux](https://termux.dev)
running. This bot talks to the phone's hardware through `termux-api`, so from
Telegram you can look through its cameras, listen to the room, make it speak,
flash its torch, and — the part that actually earns its keep — get told when
something at home changes: the power went out, the internet died, a device
joined or left the network.

No root, no custom ROM. Everything here works on stock Android 7 with Termux
installed from the F-Droid/GitHub builds.

The bot's own interface is in Russian — that is the language of the people who
use it. Code, comments and this file are in English.

## What it does

**Remote (you press a button, the phone acts)**

| Button | What happens |
| --- | --- |
| 📷 Back / 🤳 Front | A photo, straight away — the cameras are on the main keyboard, not behind a menu |
| 📹 Timelapse | The rolling recording: clips of the last N minutes, a frame from a given time, interval and quality |
| 📊 Status | Thermometer, battery, light level, wifi and internet on one screen |
| 👥 Who's home | Devices on the LAN right now |
| 📺 TV | Three remotes for the LG set: IR power + network for the rest, network only, or IR only |
| 🏠 Home | Listen, speak, torch, find the phone, and the presence chart |
| 📶 Networks | Access points in range, device and network logs, naming things |

**The timelapse**

The phone films the room the way a dashcam films the road: one frame every N
seconds, around the clock, oldest frames deleted once the archive hits its
size cap. Nothing to start, nothing to stop — you only ever look backwards.

- interval 5 / 10 / 15 / 20 / 30 / 60 s, 20 by default
- clips of the last 30 s, 1, 2, 3, 10, 30 minutes, an hour, 3, 6, 12 hours or
  a whole day, as an h264 video
- long windows are thinned before encoding, so a day is a minute of playback
  in a few megabytes rather than four thousand frames nobody will watch
- "what did it look like at 15:10" returns the three nearest frames
- **motion filtering**, and you pick *when* it applies: always, at night only,
  or never
- photos taken by hand from the camera button land in the archive too, under
  whichever camera took them
- the date and time are burnt into the corner of every frame — not added as a
  caption, because a caption is gone the moment the frames become a video.
  Frames saved before this existed can be back-filled from the settings
- the settings screen carries a forecast — megabytes a day, and how long the
  cap will last — recalculated on every press, so you see what a setting costs
  before you walk away

Frames live in `frames/<date>/<hour>/<mmss>_<camera>.jpg` — the filesystem is
the index, so a window of time is one or two directory reads and nothing goes
stale if the process is killed mid-write.

Motion detection is a 64-px greyscale thumbnail compared against the previous
one: mean brightness change per pixel, thresholded. Small enough that sensor
noise averages out — an empty room sits around 2, someone walking through
pushes it past 30. The camera still fires on every tick, because there is no
other way to notice movement without a hardware PIR sensor, so this saves
archive rather than battery. One frame every ten minutes is kept whatever
happens, so a quiet night still proves the camera was alive.

Filtering the daylight hours is usually the wrong trade: it throws away most
of the day and plays back as a stack of jump cuts. Night is the opposite —
the room is dark and still, and unfiltered it is nine hours of identical
black that costs real space. Hence "night only", with a pickable window.

**Where the frames go**

Internal storage by default. If a memory card is in the phone and Termux has
been given storage permission, the archive moves to the card instead — Android
only lets an app write to its own corner of a removable card, so the target is
`/storage/<UUID>/Android/data/com.termux/files/frames`, which is what
`termux-setup-storage` links to as `~/storage/external-1`.

Nothing depends on the card being there. Both locations are read together and
sorted by hour, so swapping a card does not cut the timeline in two, and the
ring still deletes the genuinely oldest frames first wherever they live. If
the card disappears mid-recording the archive falls back to internal storage
and the bot says so.

**Watchers (the phone messages you on its own)**

- **Power cut** — the charger stopped feeding it. The battery inside is the UPS,
  so the phone survives the outage and reports it.
- **Internet down / back**
- **Device joined or left the LAN** — with a grace period, because phones sleep
  their Wi-Fi and a single missed sweep doesn't mean somebody left the house
- **New access point in range**, or a known one disappearing
- **Battery low** while unplugged
- **Motion** near the phone (accelerometer, off by default — it costs battery)
- **Temperature** out of range (battery sensor, a rough room proxy)

Everything is toggled from ⚙️ Settings, and every device or network can be
given a human name with ✏️ *Подписать* so alerts read "the Mac left" instead
of a MAC address.

## How presence detection works

There is no Bluetooth scanning here, and there won't be: `termux-api` exposes
no Bluetooth commands, BlueZ isn't in the Termux repos, and `/sys/class/bluetooth`
is closed to unprivileged apps on Android. Presence is done over Wi-Fi only:

1. ping sweep across the local /24, which fills the kernel ARP table
2. read `/proc/net/arp` — readable without root
3. a device must miss several consecutive sweeps before it counts as gone

That last step is what makes it usable. Phones drop off the ARP table when
they doze; without the grace period you'd get "left home" alerts all night.

The sweep survives a quirk of this phone: ICMP echo replies never come back
to Termux here — not from the internet, not even from the router — so `ping`
reports 100% loss for everything. The ARP entries it leaves behind are still
real, because the kernel has to resolve the address before it can send at
all, and that is the only part the sweep needs. Anything that judged the
network by a ping exit code was wrong, which is why the internet check is a
TCP connect now.

## Three TV remotes, on purpose

The living-room LG (50LF652V, 2015) can be driven two ways, and the bot keeps
the channels apart. Pick a remote in the 📺 section; the network one never
silently falls back to IR, so a failure tells you which channel is broken.

The default is the **combined** remote, and it exists because of one hard
fact below: IR is the only thing that can wake this set, and the network is
better at everything else. So power goes over IR, the rest over the network,
and the channel-zapping and TV-settings buttons are left out entirely.
The ▶️ YouTube button on it is a macro — if the set is off it fires IR power,
waits for the TV to come back on the network, and then launches the app.
There is no source list on it either: the only input anyone ever picks is the
computer, so it's a plain **🖥 HDMI 1** button. The id of that socket is asked
from the TV the first time and kept in `home.db` — webOS calls it `HDMI_1`
here, but that isn't worth hardcoding sight unseen.

**Over the network — `tv.py`.** The set is on the router by cable, so we speak
SSAP, LG's own WebSocket protocol on port 3000. This is the good one: launching
apps by name, arrow keys and OK, volume with the level read back, input
switching, typing into on-screen fields, and toasts on the TV. Pair once with

```sh
python pair_tv.py left ok
```

The TV puts an accept/deny dialog on screen; the arguments are the buttons to
press on it through the IR blaster, because the focus does not start on "Yes".
The client-key it returns is stored in `home.db` and every later connection is
silent.

Stuck YouTube gets its own two answers, both in the 📺 menu: *restart* stops
the app properly through DIAL and relaunches it, and any YouTube link pasted
into the chat opens straight in the player, skipping the app's own menus — on
a set this old, those menus are usually what's hanging.

**Waking it up is IR-only.** Measured on this set, confirmed from the router's own
status page: with the TV on, its port reads "Active 100 Mbps"; twenty seconds after
a network power-off it reads "Inactive". Standby cuts power to the Ethernet chip, so
a magic packet has nothing to arrive at and Wake-on-LAN cannot work whatever the menus
say (LG Connect Apps is on; there is no "Mobile TV On" item on this model).
After an IR power-on the TV is back on the network in about 40 seconds.
The power button in the network remote therefore fires the IR blaster and says so
on the button. Everything else stays on the network.

**Over infrared — `ir.py`.** The standard LG NEC code set (address `0x20DF`)
plus a NEC encoder that expands a code into the microsecond mark/space pattern
`termux-infrared-transmit` wants. Confirmed working on the real set: power,
mute, volume, OK, menu, input. There are no IR codes for apps, which is why
recorded macros exist here. Keep it around for when the TV is off the network
and for the pairing dialog above.

## Requirements

On the phone, inside Termux:

```sh
pkg install python termux-api python-pillow matplotlib ffmpeg
pip install python-telegram-bot websockets
```

Pillow resizes the timelapse frames, matplotlib draws the presence chart and
ffmpeg encodes the clips; all three install as Termux packages, not through
pip.

If ffmpeg exits silently with status 1 — no output at all, not even for
`-version` — its libraries are fine and the problem is one symbol: `libplacebo`
wants `__from_chars_floating_point` from a newer `libc++` than is installed.
`pkg upgrade libc++` fixes it. `ldd` doesn't exist here to tell you that;
`python -c 'import ctypes; ctypes.CDLL("libavfilter.so.11")'` does, because
dlopen prints the missing symbol.

The Termux:API companion app must be installed from the **same source** as
Termux itself (both from GitHub releases, or both from F-Droid). Mixed signatures
silently fail to talk to each other.

## Configuration

Copy `.env.example` to `.env` and fill it in:

```
HOMEBOT_TOKEN=<token from @BotFather>
HOMEBOT_OWNERS=<your telegram id>[,<second id>]
HOMEBOT_GUESTS=<telegram id>[,<second id>]

HOMEBOT_TV_IP=<the TV's address on your LAN>
HOMEBOT_TV_MAC=<its MAC, for Wake-on-LAN over cable>
HOMEBOT_TV_NAME=<whatever you want it called in the messages>
```

The three TV lines are optional — leave them empty and the network remote is
simply unavailable; everything else works.

`HOMEBOT_OWNERS` is a hard allowlist: anyone else gets "this bot is private"
and nothing else. The file is git-ignored and never leaves the phone.

`HOMEBOT_GUESTS` hands someone the TV remote and nothing else. A guest sees a
keyboard with a single 📺 button, all three remotes behind it, and can paste a
YouTube link like the owner can. Cameras, the archive, the network scans and
the settings are not just refused — they are never drawn, and a callback from
outside the remote is turned away even if the button is somehow forged. A
guest's `/start` also leaves the notification chat alone, so alerts keep going
to the owner.

## Running it

The bot reads its settings from the environment, so export `.env` before
starting it:

```sh
set -a; . ./.env; set +a
python bot.py
```

For unattended operation, start it from `~/.termux/boot/` (Termux:Boot) and
keep a `termux-wake-lock` held, otherwise Android will eventually suspend it.
On MIUI you also need to allow autostart for Termux and exempt it from battery
optimisation, or the process gets killed within minutes.

## Permissions

The first camera, microphone or location call raises an Android permission
dialog that has to be accepted on the phone itself — it cannot be granted
remotely. Do it once, up front, or the first remote photo will silently fail.

Storing frames on a memory card needs one more: run `termux-setup-storage` and
accept the dialog. Until that happens the card is invisible to the app —
writing anywhere on it, including `Android/data/`, comes back as permission
denied — and the recorder quietly stays on internal storage.

## Layout

```
bot.py       menus, handlers, the button UI
actions.py   thin wrappers over the termux-* binaries
timelapse.py the rolling recorder, the frame archive, and h264 clips
watchers.py  background loops that message you on events
netscan.py   ping sweep + ARP + access point scan
ir.py        NEC encoder and the LG code table (infrared remote)
tv.py        SSAP client for the same TV over the network
pair_tv.py   one-off pairing helper, writes the client-key into home.db
store.py     SQLite: labels, last-seen state, presence log, settings
```
