# Home Asis

A Telegram bot that turns an old Android phone into a small home hub.

The phone sits on a charger somewhere in the flat with [Termux](https://termux.dev)
running. This bot talks to the phone's hardware through `termux-api`, so from
Telegram you can look through its cameras, listen to the room, make it speak,
flash its torch, and — the part that actually earns its keep — get told when
something at home changes: the power went out, the internet died, a device
joined or left the network.

No root, no custom ROM. Everything here works on stock Android 7 with Termux
installed from the F-Droid/GitHub builds.

## What it does

**Remote (you press a button, the phone acts)**

| Button | What happens |
| --- | --- |
| 📷 Camera | Photo from the back camera, the front one, or both |
| 🎤 Listen | Record 10 or 30 seconds of room audio and send it back as a voice message |
| 📝 Speech to text | Listen and reply with a transcript instead of audio |
| 🗣 Speak | Type text, the phone says it out loud in the room |
| 🔦 Torch | Blink, hold the light on, vibrate |
| 🚨 Find phone | Torch + full-volume speech + vibration at once |
| 📺 TV | Two separate remotes for the LG set: over the network, or over infrared |
| 🏠 Home | Who's on the network, access points in range, thermometer, battery, light level, overall status |

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

## Two TV remotes, on purpose

The living-room LG (50LF652V, 2015) can be driven two ways, and the bot keeps
them apart. Pick one in the 📺 section; neither ever silently falls back to the
other, so a failure tells you which channel is broken.

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

**Waking it up is IR-only.** Measured on this set: in standby it powers down the
Ethernet port completely — no ping, not even an ARP reply — so a magic packet has
nothing to arrive at, and Wake-on-LAN cannot work no matter what is enabled in the
menus (LG Connect Apps is on; there is no "Mobile TV On" item on this model).
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
pkg install python termux-api ffmpeg
pip install python-telegram-bot websockets
```

`ffmpeg` is optional — without it, recordings are sent as audio files instead of
proper voice messages.

The Termux:API companion app must be installed from the **same source** as
Termux itself (both from GitHub releases, or both from F-Droid). Mixed signatures
silently fail to talk to each other.

## Configuration

Copy `.env.example` to `.env` and fill it in:

```
HOMEBOT_TOKEN=<token from @BotFather>
HOMEBOT_OWNERS=<your telegram id>[,<second id>]
```

`HOMEBOT_OWNERS` is a hard allowlist: anyone else gets "this bot is private"
and nothing else. The file is git-ignored and never leaves the phone.

## Running it

```sh
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

## Layout

```
bot.py       menus, handlers, the button UI
actions.py   thin wrappers over the termux-* binaries
watchers.py  background loops that message you on events
netscan.py   ping sweep + ARP + access point scan
ir.py        NEC encoder and the LG code table (infrared remote)
tv.py        SSAP client for the same TV over the network
pair_tv.py   one-off pairing helper, writes the client-key into home.db
store.py     SQLite: labels, last-seen state, settings
```
