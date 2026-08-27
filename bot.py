"""Home Asis — a Telegram remote for an old Android phone acting as a home hub.

Buttons only, no typed commands: a persistent reply keyboard for the sections,
inline keyboards for the actual actions.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import actions
import ir
import netscan
import store
import tv

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("homebot")

TOKEN = os.environ["HOMEBOT_TOKEN"]
OWNERS = {int(x) for x in os.environ.get("HOMEBOT_OWNERS", "").replace(" ", "").split(",") if x}

# ---------------------------------------------------------------- keyboard

BTN_CAM, BTN_MIC = "📷 Камера", "🎤 Слушать"
BTN_LIGHT, BTN_SAY = "🔦 Свет", "🗣 Сказать"
BTN_TV, BTN_HOME = "📺 Телевизор", "🏠 Дом"
BTN_SETTINGS = "⚙️ Настройки"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_CAM), KeyboardButton(BTN_MIC)],
        [KeyboardButton(BTN_LIGHT), KeyboardButton(BTN_SAY)],
        [KeyboardButton(BTN_TV), KeyboardButton(BTN_HOME)],
        [KeyboardButton(BTN_SETTINGS)],
    ],
    resize_keyboard=True,
)


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and (not OWNERS or user.id in OWNERS))


async def guard(update: Update) -> bool:
    if allowed(update):
        return True
    who = update.effective_user
    log.warning("отказано: %s (%s)", who.id if who else "?", who.username if who else "?")
    if update.message:
        await update.message.reply_text("Этот бот личный.")
    elif update.callback_query:
        await update.callback_query.answer("Не для вас.", show_alert=True)
    return False


# ---------------------------------------------------------------- sections

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    store.put("chat_id", str(update.effective_chat.id))
    await update.message.reply_text(
        "🏠 <b>Home Asis</b> на связи.\nВыбирай раздел кнопками снизу.",
        reply_markup=MAIN_KB,
        parse_mode=ParseMode.HTML,
    )


async def section_camera(update: Update, ctx) -> None:
    await update.message.reply_text(
        "Какой камерой снять?",
        reply_markup=ikb([
            [("📷 Задняя", "cam:0"), ("🤳 Фронтальная", "cam:1")],
            [("📷🤳 Обе сразу", "cam:both")],
        ]),
    )


async def section_mic(update: Update, ctx) -> None:
    await update.message.reply_text(
        "Что послушать?",
        reply_markup=ikb([
            [("🎤 10 сек", "rec:10"), ("🎤 30 сек", "rec:30")],
            [("📝 Распознать речь", "stt:1")],
        ]),
    )


async def section_light(update: Update, ctx) -> None:
    await update.message.reply_text(
        "Вспышка и сигналы:",
        reply_markup=ikb([
            [("💡 Мигнуть", "torch:blink"), ("🔦 Включить", "torch:on")],
            [("🌑 Выключить", "torch:off"), ("📳 Вибро", "torch:vibe")],
            [("🚨 Найти телефон", "torch:find")],
        ]),
    )


async def section_say(update: Update, ctx) -> None:
    ctx.user_data["await"] = "say"
    await update.message.reply_text("Напиши текст — телефон произнесёт его вслух.")


# ---------------------------------------------------------------- tv

# Two independent remotes for the same set: one over the network (SSAP), one
# over the IR blaster. They never fall back to each other — if the network one
# fails you get told, and you can walk over to the IR one yourself.

_tv_session: tv.TV | None = None


def tv_net() -> tv.TV | None:
    """The SSAP session, or None if we've never been paired with the TV."""
    global _tv_session
    key = store.get("tv_client_key")
    if not key:
        return None
    if _tv_session is None or _tv_session.key != key:
        _tv_session = tv.TV(key=key)
    return _tv_session


def tv_net_kb() -> InlineKeyboardMarkup:
    return ikb([
        [("⏻ Включить", "tvn:on"), ("⏻ Выключить", "tvn:off")],
        [("🔉 Тише", "tvn:vol_down"), ("🔇 Звук", "tvn:mute"), ("🔊 Громче", "tvn:vol_up")],
        [("⬆️", "tvn:btn:UP")],
        [("⬅️", "tvn:btn:LEFT"), ("OK", "tvn:btn:ENTER"), ("➡️", "tvn:btn:RIGHT")],
        [("⬇️", "tvn:btn:DOWN")],
        [("↩️ Назад", "tvn:btn:BACK"), ("🏠 Home", "tvn:btn:HOME"), ("📺 Каналы", "tvn:livetv")],
        [("📱 Приложения", "tvn:apps"), ("🔌 Источник", "tvn:inputs")],
        [("⌨️ Ввести текст", "tvn:type"), ("♻️ Перезапустить YouTube", "tvn:yt_restart")],
        [("💬 Написать на экран", "tvn:toast"), ("ℹ️ Что на экране", "tvn:status")],
        [("📡 Переключиться на ИК-пульт", "tvpick:ir")],
    ])


async def tv_net_message(chat) -> None:
    if tv_net() is None:
        await chat.send_message(
            "Телевизор ещё не спарен по сети. На телефоне: "
            "<code>cd ~/lab/homebot && python pair_tv.py left ok</code>",
            parse_mode=ParseMode.HTML)
        return
    await chat.send_message(
        "🌐 <b>Телевизор по сети</b>\nLG 50LF652V · 192.168.1.100\n"
        "<i>Ссылку на YouTube можно просто прислать сюда — включится на телевизоре.</i>",
        parse_mode=ParseMode.HTML, reply_markup=tv_net_kb())


def tv_kb(recording: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [("⏻ Питание", "tv:power"), ("🔇 Звук", "tv:mute")],
        [("🔊 Громче", "tv:vol_up"), ("🔉 Тише", "tv:vol_down")],
        [("⚙️ Настройки ТВ", "tv:menu"), ("🔌 Источник", "tv:input")],
        [("⬆️", "tv:up")],
        [("⬅️", "tv:left"), ("OK", "tv:ok"), ("➡️", "tv:right")],
        [("⬇️", "tv:down")],
        [("↩️ Назад", "tv:back"), ("🏠 Home", "tv:home"), ("✖️ Выход", "tv:exit")],
    ]
    macros = store.macro_list()
    if macros:
        rows.append([(f"🎬 {m['name']}", f"macro:run:{m['name']}") for m in macros[:3]])
    if recording:
        rows.append([("💾 Сохранить последовательность", "macro:save"),
                     ("✖️ Отменить запись", "macro:cancel")])
    else:
        rows.append([("🎬 Записать кнопку-макрос", "macro:rec")])
    rows.append([("📺 Канал +", "tv:ch_up"), ("📺 Канал −", "tv:ch_down")])
    return ikb(rows)


async def section_tv(update: Update, ctx) -> None:
    await update.message.reply_text(
        "📺 <b>LG 50LF652V</b>\nЧем управлять?",
        parse_mode=ParseMode.HTML,
        reply_markup=ikb([
            [("🌐 По сети (кабель)", "tvpick:net")],
            [("📡 По инфракрасному", "tvpick:ir")],
        ]),
    )


async def section_home(update: Update, ctx) -> None:
    await update.message.reply_text(
        "Что показать по дому?",
        reply_markup=ikb([
            [("👥 Кто дома", "home:devices"), ("📶 Сети рядом", "home:networks")],
            [("🌡 Термометр", "home:temp"), ("🔋 Батарея", "home:batt")],
            [("💡 Освещённость", "home:light"), ("📊 Статус", "home:status")],
            [("📜 Журнал сетей", "home:netlog"), ("📜 Журнал устройств", "home:devlog")],
            [("✏️ Подписать устройство", "label:dev"), ("✏️ Подписать сеть", "label:net")],
        ]),
    )


def settings_kb() -> InlineKeyboardMarkup:
    def mark(key: str, default: bool = True) -> str:
        return "✅" if store.flag(key, default) else "❌"

    return ikb([
        [(f"{mark('notify_devices')} Устройства сети", "set:notify_devices")],
        [(f"{mark('notify_networks', False)} Сети вокруг (по умолчанию тихо)", "set:notify_networks")],
        [(f"{mark('notify_power')} Питание", "set:notify_power")],
        [(f"{mark('notify_internet')} Интернет", "set:notify_internet")],
        [(f"{mark('notify_battery')} Заряд батареи", "set:notify_battery")],
        [(f"{mark('notify_motion', False)} Движение (жрёт батарею)", "set:notify_motion")],
        [(f"{mark('notify_climate', False)} Температура", "set:notify_climate")],
    ])


async def section_settings(update: Update, ctx) -> None:
    await update.message.reply_text(
        "Какие уведомления присылать:", reply_markup=settings_kb()
    )


SECTIONS = {
    BTN_CAM: section_camera,
    BTN_MIC: section_mic,
    BTN_LIGHT: section_light,
    BTN_SAY: section_say,
    BTN_TV: section_tv,
    BTN_HOME: section_home,
    BTN_SETTINGS: section_settings,
}


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()

    if text in ("/start", "/menu"):
        return await start(update, ctx)

    waiting = ctx.user_data.pop("await", None)
    if waiting == "say":
        await update.message.chat.send_action(ChatAction.TYPING)
        ok = await actions.say(text)
        await update.message.reply_text(
            "🗣 Сказал." if ok else "Не смог произнести — проверь громкость и TTS."
        )
        return
    if waiting in ("tv_type", "tv_toast"):
        t = tv_net()
        if t is None:
            await update.message.reply_text("Телевизор не спарен по сети.")
            return
        try:
            if waiting == "tv_type":
                await t.type_text(text)
                await t.enter()
                await update.message.reply_text("⌨️ Набрал и нажал Enter.")
            else:
                await t.toast(text)
                await update.message.reply_text("💬 Показал на экране телевизора.")
        except Exception as e:
            await update.message.reply_text(f"Телевизор не принял: {e}")
        return

    if isinstance(waiting, tuple) and waiting[0] == "label_dev":
        store.label_device(waiting[1], text)
        await update.message.reply_text(f"✏️ Устройство подписано: <b>{html.escape(text)}</b>",
                                        parse_mode=ParseMode.HTML)
        return
    if isinstance(waiting, tuple) and waiting[0] == "macro_name":
        store.save_macro(text, waiting[1])
        await update.message.reply_text(
            f"🎬 Кнопка <b>{html.escape(text)}</b> сохранена — она появилась в разделе «Телевизор».",
            parse_mode=ParseMode.HTML)
        return
    if isinstance(waiting, tuple) and waiting[0] == "label_net":
        store.label_network(waiting[1], text)
        await update.message.reply_text(f"✏️ Сеть подписана: <b>{html.escape(text)}</b>",
                                        parse_mode=ParseMode.HTML)
        return

    handler = SECTIONS.get(text)
    if handler:
        return await handler(update, ctx)

    # A pasted YouTube link goes straight to the TV — this is the way around
    # the app's own menus when they decide to hang on the loading spinner.
    vid = tv.video_id(text)
    if vid:
        await update.message.chat.send_action(ChatAction.TYPING)
        if await tv.youtube_play(vid):
            await update.message.reply_text("▶️ Включаю на телевизоре.")
        else:
            await update.message.reply_text(
                "Телевизор не принял ссылку — он вообще включён?")
        return

    await update.message.reply_text("Не понял. Пользуйся кнопками снизу.", reply_markup=MAIN_KB)


# ---------------------------------------------------------------- callbacks

async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = update.callback_query
    data = q.data or ""
    chat = q.message.chat

    if data.startswith("cam:"):
        which = data.split(":", 1)[1]
        await q.answer("Снимаю…")
        ids = ["0", "1"] if which == "both" else [which]
        for cid in ids:
            await chat.send_action(ChatAction.UPLOAD_PHOTO)
            path = await actions.photo(cid)
            if path:
                with open(path, "rb") as fh:
                    await chat.send_photo(fh, caption="📷 задняя" if cid == "0" else "🤳 фронтальная")
                os.remove(path)
            else:
                await chat.send_message("Камера не отдала снимок (разрешение? занята другим приложением?)")
        return

    if data.startswith("rec:"):
        secs = int(data.split(":", 1)[1])
        await q.answer(f"Пишу {secs} сек…")
        await chat.send_action(ChatAction.RECORD_VOICE)
        raw = await actions.record(secs)
        if not raw:
            return await chat.send_message("Микрофон ничего не отдал (разрешение?).")
        ogg = await actions.to_voice(raw)
        with open(ogg or raw, "rb") as fh:
            if ogg:
                await chat.send_voice(fh, caption=f"🎤 {secs} сек")
            else:
                await chat.send_audio(fh, caption=f"🎤 {secs} сек")
        for p in filter(None, (raw, ogg)):
            try:
                os.remove(p)
            except OSError:
                pass
        return

    if data.startswith("stt:"):
        await q.answer("Слушаю…")
        await chat.send_action(ChatAction.TYPING)
        text = await actions.speech_to_text()
        await chat.send_message(f"📝 {html.escape(text) if text else 'ничего не разобрал'}",
                                parse_mode=ParseMode.HTML)
        return

    if data.startswith("torch:"):
        what = data.split(":", 1)[1]
        if what == "blink":
            await q.answer("Мигаю")
            await actions.blink()
        elif what == "on":
            await q.answer("Фонарь включён")
            await actions.torch(True)
        elif what == "off":
            await q.answer("Фонарь выключен")
            await actions.torch(False)
        elif what == "vibe":
            await q.answer("Вибрирую")
            await actions.vibrate()
        elif what == "find":
            await q.answer("Ищу телефон")
            await actions.set_volume("music", 15)
            await asyncio.gather(
                actions.blink(times=12, on_s=0.2, off_s=0.2),
                actions.say("Я здесь. Я здесь. Я здесь."),
                actions.vibrate(3000),
            )
        return

    if data.startswith("tvpick:"):
        await q.answer()
        if data.endswith(":net"):
            await tv_net_message(chat)
        else:
            await chat.send_message(
                "📡 <b>ИК-пульт</b> — телефон светит в приёмник телевизора.",
                parse_mode=ParseMode.HTML,
                reply_markup=tv_kb(bool(ctx.user_data.get("macro_rec") is not None)))
        return

    if data.startswith("tvn:"):
        await tv_net_cb(q, ctx, data.split(":", 1)[1])
        return

    if data.startswith("tv:"):
        button = data.split(":", 1)[1]
        ok, msg = await ir.send(button)
        rec = ctx.user_data.get("macro_rec")
        if rec is not None and ok:
            rec.append(button)
            await q.answer(f"📺 {button} · записано ({len(rec)})")
            return
        await q.answer("📺 " + (button if ok else msg[:60]), show_alert=not ok)
        return

    if data.startswith("macro:"):
        what = data.split(":", 1)[1]
        if what == "rec":
            ctx.user_data["macro_rec"] = []
            await q.answer("Запись пошла")
            await q.edit_message_text(
                "🎬 <b>Запись макроса</b>\nЖми кнопки как на обычном пульте — каждая уходит "
                "на телевизор и запоминается. Когда дойдёшь куда надо, нажми «Сохранить».",
                parse_mode=ParseMode.HTML, reply_markup=tv_kb(recording=True))
            return
        if what == "cancel":
            ctx.user_data.pop("macro_rec", None)
            await q.answer("Отменил")
            await q.edit_message_text("📺 <b>LG 50LF652V</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=tv_kb())
            return
        if what == "save":
            steps = ctx.user_data.get("macro_rec") or []
            if not steps:
                await q.answer("Пусто — сначала нажми хоть одну кнопку", show_alert=True)
                return
            ctx.user_data["await"] = ("macro_name", steps)
            ctx.user_data.pop("macro_rec", None)
            await q.answer()
            await chat.send_message(
                f"Записал {len(steps)} шагов. Как назвать кнопку? (например «YouTube»)")
            return
        if what.startswith("run:"):
            name = what.split(":", 1)[1]
            steps = store.macro_steps(name)
            await q.answer(f"🎬 {name}: {len(steps)} шагов")
            sent, failed = await ir.play_macro(steps)
            if failed:
                await chat.send_message("🎬 Сбой: " + "; ".join(failed[:3]))
            return
        return

    if data.startswith("home:"):
        what = data.split(":", 1)[1]
        await q.answer("Собираю…")
        # termux-api calls take a few seconds each; say so instead of looking dead
        note = await chat.send_message("⏳ Секунду…")
        try:
            await home_report(chat, what)
        finally:
            try:
                await note.delete()
            except Exception:
                pass
        return

    if data.startswith("set:"):
        key = data.split(":", 1)[1]
        # motion and climate are opt-in, everything else is on unless turned off
        default_on = key not in ("notify_motion", "notify_climate", "notify_networks")
        store.set_flag(key, not store.flag(key, default_on))
        await q.answer("Переключил")
        await q.edit_message_reply_markup(reply_markup=settings_kb())
        return

    if data == "label:dev":
        rows = store.device_list()[:20]
        if not rows:
            await q.answer("Список пуст — сделай «Кто дома»", show_alert=True)
            return
        await q.answer()
        await chat.send_message(
            "Кого подписываем?",
            reply_markup=ikb([[(f"{'🟢' if r['online'] else '⚪️'} {r['label'] or r['mac']}",
                                f"pickdev:{r['mac']}")] for r in rows]),
        )
        return

    if data == "label:net":
        rows = store.network_list()[:20]
        if not rows:
            await q.answer("Список пуст — сделай «Сети рядом»", show_alert=True)
            return
        await q.answer()
        await chat.send_message(
            "Какую сеть подписываем?",
            reply_markup=ikb([[(f"{'🟢' if r['present'] else '⚪️'} {r['label'] or r['ssid'] or r['bssid']}",
                                f"picknet:{r['bssid']}")] for r in rows]),
        )
        return

    if data.startswith("pickdev:"):
        ctx.user_data["await"] = ("label_dev", data.split(":", 1)[1])
        await q.answer()
        await chat.send_message("Напиши название для этого устройства (например «мак Дамира»).")
        return

    if data.startswith("picknet:"):
        ctx.user_data["await"] = ("label_net", data.split(":", 1)[1])
        await q.answer()
        await chat.send_message("Напиши название для этой сети.")
        return

    await q.answer()


async def tv_net_cb(q, ctx, what: str) -> None:
    """Everything the wired remote can do. One place, one error style."""
    chat = q.message.chat
    t = tv_net()
    if what == "on":
        tv.wake()
        await q.answer("⏻ Разбудил — телевизору нужна пара секунд")
        return
    if t is None:
        await q.answer("Телевизор не спарен по сети", show_alert=True)
        return

    try:
        if what == "off":
            await t.turn_off()
            await t.close()
            await q.answer("⏻ Выключил")

        elif what in ("vol_up", "vol_down"):
            await (t.volume_up() if what == "vol_up" else t.volume_down())
            level = (await t.volume()).get("volume", "?")
            await q.answer(f"🔊 {level}")

        elif what == "mute":
            now_muted = bool((await t.volume()).get("muted"))
            await t.mute(not now_muted)
            await q.answer("🔊 Звук вернул" if now_muted else "🔇 Тишина")

        elif what.startswith("btn:"):
            name = what.split(":", 1)[1]
            await t.button(name)
            await q.answer(name)

        elif what == "livetv":
            await t.launch("com.webos.app.livetv")
            await q.answer("📺 Телетрансляция")

        elif what == "apps":
            await q.answer("Спрашиваю телевизор…")
            apps = [a for a in await t.apps() if not a.get("id", "").startswith("com.webos.app.hdmi")]
            rows, pair = [], []
            for a in apps[:20]:
                pair.append((a.get("title") or a.get("id"), f"tvn:app:{a.get('id')}"))
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
            if pair:
                rows.append(pair)
            await chat.send_message("Что запустить?", reply_markup=ikb(rows))

        elif what.startswith("app:"):
            app_id = what.split(":", 1)[1]
            await t.launch(app_id)
            await q.answer("Запускаю…")

        elif what == "inputs":
            await q.answer()
            devices = await t.inputs()
            rows = [[(f"{'🟢' if d.get('connected') else '⚪️'} {d.get('label') or d.get('id')}",
                      f"tvn:in:{d.get('id')}")] for d in devices]
            await chat.send_message("Куда переключить?", reply_markup=ikb(rows))

        elif what.startswith("in:"):
            await t.switch_input(what.split(":", 1)[1])
            await q.answer("🔌 Переключил")

        elif what == "yt_restart":
            await q.answer("Перезапускаю…")
            note = await chat.send_message("♻️ Выгружаю YouTube…")
            await tv.youtube_stop()
            await asyncio.sleep(4)
            await t.launch(tv.YOUTUBE_ID)
            await asyncio.sleep(6)
            state = await tv.dial_state()
            await note.edit_text(
                f"♻️ YouTube перезапущен · сейчас <b>{state or 'не отвечает'}</b>\n"
                "<i>Если снова повиснет — пришли сюда ссылку, видео откроется мимо меню.</i>",
                parse_mode=ParseMode.HTML)

        elif what == "type":
            ctx.user_data["await"] = "tv_type"
            await q.answer()
            await chat.send_message(
                "Открой на телевизоре поле ввода (например поиск в YouTube) и пришли мне текст — "
                "я наберу его и нажму Enter.")

        elif what == "toast":
            ctx.user_data["await"] = "tv_toast"
            await q.answer()
            await chat.send_message("Что написать на экране телевизора?")

        elif what == "status":
            await q.answer("Смотрю…")
            fg = await t.foreground()
            vol = await t.volume()
            yt = await tv.dial_state()
            app_id = fg.get("appId") or "—"
            titles = {a.get("id"): a.get("title") for a in await t.apps()}
            await chat.send_message(
                f"ℹ️ <b>Телевизор</b>\n"
                f"На экране: <b>{html.escape(titles.get(app_id) or app_id)}</b>\n"
                f"Громкость: {vol.get('volume', '?')}{' · без звука' if vol.get('muted') else ''}\n"
                f"YouTube: {yt or 'не отвечает'}",
                parse_mode=ParseMode.HTML)

    except tv.TVError as e:
        await q.answer(f"Телевизор ответил: {e}"[:190], show_alert=True)
    except asyncio.TimeoutError:
        await q.answer("Телевизор молчит — он выключен?", show_alert=True)
    except Exception as e:
        log.warning("сеть ТВ: %s", e)
        await q.answer(f"Не вышло: {type(e).__name__}", show_alert=True)


async def home_report(chat, what: str) -> None:
    if what == "devices":
        found = await netscan.sweep()
        store.seen_devices(found)
        rows = store.device_list()
        online = [r for r in rows if r["online"]]
        offline = [r for r in rows if not r["online"]]
        lines = [f"👥 <b>В сети сейчас: {len(online)}</b>"]
        for r in online:
            name = r["label"] or r["mac"]
            lines.append(f"🟢 {html.escape(name)} · <code>{r['last_ip'] or ''}</code>")
        if offline:
            lines.append(f"\n<i>Не в сети ({len(offline)}):</i>")
            for r in offline[:10]:
                lines.append(f"⚪️ {html.escape(r['label'] or r['mac'])}")
        await chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)

    elif what == "networks":
        found = await netscan.scan_networks()
        store.seen_networks(found)
        rows = [r for r in store.network_list() if r["present"]]
        # strongest first — the ones at the top are the ones in this flat
        rows.sort(key=lambda r: (r["rssi"] if r["rssi"] is not None else -999), reverse=True)
        lines = [f"📶 <b>Сетей в эфире: {len(rows)}</b>"]
        for r in rows[:25]:
            name = r["label"] or r["ssid"] or r["bssid"]
            dist = netscan.distance_label(r["rssi"], r["freq"])
            band = "5" if r["freq"] and int(r["freq"]) > 3000 else "2.4"
            lines.append(
                f"• <b>{html.escape(name)}</b>\n   {r['rssi']} дБм · {band} ГГц"
                + (f" · {dist}" if dist else "")
            )
        lines.append("\n<i>Расстояние прикидочное: считается по силе сигнала, "
                     "стены и мебель врут в обе стороны.</i>")
        await chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)

    elif what == "netlog":
        rows = store.network_list()
        rows.sort(key=lambda r: (r["present"], r["last_seen"] or ""), reverse=True)
        lines = [f"📜 <b>Журнал сетей</b> — всего видели {len(rows)}\n"]
        for r in rows[:60]:
            name = r["label"] or r["ssid"] or r["bssid"]
            mark = "🟢" if r["present"] else "⚪️"
            dist = netscan.distance_label(r["rssi"], r["freq"])
            seen = (r["last_seen"] or "")[5:16]
            first = (r["first_seen"] or "")[5:16]
            lines.append(
                f"{mark} <b>{html.escape(name)}</b> · {r['rssi']} дБм"
                + (f" · {dist}" if dist else "")
                + f"\n   <i>впервые {first} · видели {seen}</i>"
            )
        text = "\n".join(lines)
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
            await chat.send_message(chunk, parse_mode=ParseMode.HTML)

    elif what == "devlog":
        rows = store.device_list()
        rows.sort(key=lambda r: (r["online"], r["last_seen"] or ""), reverse=True)
        lines = [f"📜 <b>Журнал устройств</b> — всего видели {len(rows)}\n"]
        for r in rows[:60]:
            name = r["label"] or r["mac"]
            mark = "🟢" if r["online"] else "⚪️"
            lines.append(
                f"{mark} <b>{html.escape(name)}</b> · <code>{r['last_ip'] or ''}</code>"
                f"\n   <i>впервые {(r['first_seen'] or '')[5:16]} · видели {(r['last_seen'] or '')[5:16]}</i>"
            )
        text = "\n".join(lines)
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
            await chat.send_message(chunk, parse_mode=ParseMode.HTML)

    elif what == "temp":
        b = await actions.battery()
        t = b.get("temperature")
        await chat.send_message(
            f"🌡 <b>{t} °C</b>\n<i>Это температура батареи телефона — комнату показывает "
            f"приблизительно, зато честно ловит тренд.</i>",
            parse_mode=ParseMode.HTML,
        )

    elif what == "batt":
        b = await actions.battery()
        await chat.send_message(
            f"🔋 <b>{b.get('percentage','?')}%</b> · {b.get('status','?')}\n"
            f"Питание: {b.get('plugged','?')}\n"
            f"Напряжение: {b.get('voltage','?')} мВ · {b.get('temperature','?')} °C\n"
            f"Состояние: {b.get('health','?')}",
            parse_mode=ParseMode.HTML,
        )

    elif what == "light":
        lux = await actions.light_level()
        if lux is None:
            await chat.send_message(
                "Датчик освещённости не ответил. Иногда он занят системой — попробуй ещё раз.")
        else:
            mood = "темно" if lux < 10 else ("сумрак" if lux < 80 else "светло")
            await chat.send_message(
                f"💡 <b>{lux:.0f} лк</b> — {mood}\n"
                f"<i>Датчик смотрит вверх рядом с динамиком: если телефон лежит экраном "
                f"вниз или чем-то накрыт, будет темно независимо от лампы.</i>",
                parse_mode=ParseMode.HTML)

    elif what == "status":
        b = await actions.battery()
        wifi = await actions.wifi_info()
        net_up = await netscan.internet_up()
        online = len(store.device_list(online_only=True))
        await chat.send_message(
            f"📊 <b>Статус дома</b>\n"
            f"🔋 {b.get('percentage','?')}% · {b.get('plugged','?')}\n"
            f"🌡 {b.get('temperature','?')} °C\n"
            f"📶 {wifi.get('ssid','?')} · {wifi.get('link_speed_mbps','?')} Мбит/с\n"
            f"🌐 интернет: {'есть' if net_up else 'НЕТ'}\n"
            f"👥 устройств в сети: {online}",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------- wiring

async def post_init(app: Application) -> None:
    store.init()
    import watchers

    async def send(text: str) -> None:
        chat_id = store.get("chat_id") or (str(next(iter(OWNERS))) if OWNERS else None)
        if not chat_id:
            return
        try:
            await app.bot.send_message(int(chat_id), text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("не смог отправить: %s", e)

    app.bot_data["watchers"] = watchers.start_all(send)
    log.info("сторож запущен: %d наблюдателей", len(app.bot_data["watchers"]))


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_cb))
    log.info("Home Asis запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
