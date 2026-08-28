"""Home Asis — a Telegram remote for an old Android phone acting as a home hub.

Buttons only, no typed commands: a persistent reply keyboard for the sections,
inline keyboards for the actual actions.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import time

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
import timelapse
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

# The two cameras sit right on the main keyboard: picking "camera" and then
# picking which camera was two taps for the thing you do most often.
BTN_BACK_CAM, BTN_FRONT_CAM = "📷 Задняя", "🤳 Фронтальная"
BTN_TIMELAPSE, BTN_STATUS = "📹 Таймлапс", "📊 Статус"
BTN_TV, BTN_WHO = "📺 Телевизор", "👥 Кто дома"
BTN_HOME, BTN_NETS = "🏠 Дом", "📶 Сети"
BTN_SETTINGS = "⚙️ Настройки"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_BACK_CAM), KeyboardButton(BTN_FRONT_CAM)],
        [KeyboardButton(BTN_TIMELAPSE), KeyboardButton(BTN_STATUS)],
        [KeyboardButton(BTN_TV), KeyboardButton(BTN_WHO)],
        [KeyboardButton(BTN_HOME), KeyboardButton(BTN_NETS)],
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


async def shoot(chat, camera_id: str) -> None:
    """One photo, straight away — no menu in between."""
    await chat.send_action(ChatAction.UPLOAD_PHOTO)
    path = await actions.photo(camera_id)
    if not path:
        await chat.send_message(
            "Камера не отдала снимок. Обычно это разрешение или занятая камера — "
            "таймлапс снимает раз в несколько секунд, попробуй ещё раз.")
        return
    with open(path, "rb") as fh:
        await chat.send_photo(fh, caption="📷 задняя" if camera_id == "0" else "🤳 фронтальная")
    try:
        os.remove(path)
    except OSError:
        pass


async def section_back_cam(update: Update, ctx) -> None:
    await shoot(update.message.chat, "0")


async def section_front_cam(update: Update, ctx) -> None:
    await shoot(update.message.chat, "1")


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
        [("⏻ Включить (ИК)", "tvn:on"), ("⏻ Выключить", "tvn:off")],
        [("🔉 Тише", "tvn:vol_down"), ("🔇 Звук", "tvn:mute"), ("🔊 Громче", "tvn:vol_up")],
        [("⬆️", "tvn:btn:UP")],
        [("⬅️", "tvn:btn:LEFT"), ("OK", "tvn:btn:ENTER"), ("➡️", "tvn:btn:RIGHT")],
        [("⬇️", "tvn:btn:DOWN")],
        [("↩️ Назад", "tvn:btn:BACK"), ("🏠 Home", "tvn:btn:HOME"), ("📺 Каналы", "tvn:livetv")],
        [("▶️ YouTube", "tvn:youtube"), ("📱 Приложения", "tvn:apps")],
        [("🌐 Браузер", "tvn:browser"), ("🔌 Источник", "tvn:inputs")],
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
        "<i>Ссылку на YouTube можно просто прислать сюда — включится на телевизоре.\n"
        "Включение идёт по ИК: спящий телевизор глушит сетевой порт и разбудить "
        "его по кабелю невозможно.</i>",
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
            [("🎛 Обычный (ИК питание + сеть)", "tvpick:mix")],
            [("🌐 Только по сети", "tvpick:net")],
            [("📡 Только по инфракрасному", "tvpick:ir")],
        ]),
    )


# The third remote: питание уходит по ИК (сеть его разбудить не может — в
# дежурном режиме телевизор глушит порт), всё остальное — по сети. Каналов
# и настроек ТВ здесь нет: с них Дамир всё равно не пользуется, а лишние
# кнопки только мешают попасть в нужную.
def tv_mix_kb() -> InlineKeyboardMarkup:
    return ikb([
        [("⏻ Включить (ИК)", "tvc:on"), ("⏻ Выключить (ИК)", "tvc:off")],
        [("🔉 Тише", "tvn:vol_down"), ("🔇 Звук", "tvn:mute"), ("🔊 Громче", "tvn:vol_up")],
        [("⬆️", "tvn:btn:UP")],
        [("⬅️", "tvn:btn:LEFT"), ("OK", "tvn:btn:ENTER"), ("➡️", "tvn:btn:RIGHT")],
        [("⬇️", "tvn:btn:DOWN")],
        [("↩️ Назад", "tvn:btn:BACK"), ("🏠 Home", "tvn:btn:HOME")],
        [("▶️ YouTube", "tvn:youtube"), ("📱 Приложения", "tvn:apps")],
        [("🌐 Браузер", "tvn:browser"), ("ℹ️ Что на экране", "tvn:status")],
    ])


def tv_pointer_kb(step: int) -> InlineKeyboardMarkup:
    """A trackpad made of buttons — webOS only moves its cursor by deltas."""
    return ikb([
        [("↖️", f"tvp:m:-{step}:-{step}"), ("⬆️", f"tvp:m:0:-{step}"),
         ("↗️", f"tvp:m:{step}:-{step}")],
        [("⬅️", f"tvp:m:-{step}:0"), ("👆 Клик", "tvp:click"),
         ("➡️", f"tvp:m:{step}:0")],
        [("↙️", f"tvp:m:-{step}:{step}"), ("⬇️", f"tvp:m:0:{step}"),
         ("↘️", f"tvp:m:{step}:{step}")],
        [("🔼 Прокрутка вверх", "tvp:s:1"), ("🔽 Прокрутка вниз", "tvp:s:-1")],
        [(f"📏 Шаг курсора: {step} px", "tvp:step"), ("⌨️ Ввести текст", "tvn:type")],
        [("↩️ К пульту", "tvpick:mix")],
    ])


def pointer_step() -> int:
    try:
        return int(store.get("tv_pointer_step", "60") or 60)
    except ValueError:
        return 60


async def section_home(update: Update, ctx) -> None:
    """Everything that isn't needed several times a day lives in here."""
    await update.message.reply_text(
        "🏠 <b>Дом</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=ikb([
            [("🎤 Слушать", "home:mic"), ("🗣 Сказать", "home:say")],
            [("🔦 Свет", "home:torch"), ("🚨 Найти телефон", "torch:find")],
            [("📈 График «Кто дома»", "home:chart")],
        ]),
    )


async def section_nets(update: Update, ctx) -> None:
    await update.message.reply_text(
        "📶 <b>Сети</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=ikb([
            [("📡 Сети рядом", "home:networks")],
            [("📜 Журнал устройств", "home:devlog"), ("📜 Журнал сетей", "home:netlog")],
            [("✏️ Подписать устройство", "label:dev"), ("✏️ Подписать сеть", "label:net")],
        ]),
    )


async def section_status(update: Update, ctx) -> None:
    chat = update.message.chat
    note = await chat.send_message("⏳ Опрашиваю датчики…")
    try:
        await home_report(chat, "status")
    finally:
        try:
            await note.delete()
        except Exception:
            pass


async def section_who(update: Update, ctx) -> None:
    chat = update.message.chat
    note = await chat.send_message("⏳ Сканирую сеть…")
    try:
        await home_report(chat, "devices")
    finally:
        try:
            await note.delete()
        except Exception:
            pass


# ---------------------------------------------------------------- timelapse

def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} ч {m} мин"
    return f"{h // 24} сут {h % 24} ч"


def timelapse_kb() -> InlineKeyboardMarkup:
    on = timelapse.enabled()
    return ikb([
        [("⏸ Остановить запись" if on else "▶️ Включить запись", "tl:toggle")],
        [("30 сек", "tl:clip:30"), ("1 мин", "tl:clip:60"),
         ("2 мин", "tl:clip:120"), ("3 мин", "tl:clip:180")],
        [("10 мин", "tl:clip:600"), ("30 мин", "tl:clip:1800"), ("1 час", "tl:clip:3600")],
        [("🕐 Кадр по времени", "tl:attime"), ("🖼 Последний кадр", "tl:last")],
        [(f"⏱ Интервал: {timelapse.interval()} сек", "tl:interval")],
        [("⚙️ Настройки записи", "tl:setup"), ("📊 Архив", "tl:stats")],
    ])


def timelapse_text() -> str:
    span = timelapse.bounds()
    if span:
        oldest, newest = span
        depth = (f"глубина {_ago(time.time() - oldest)} · "
                 f"последний кадр {time.strftime('%H:%M:%S', time.localtime(newest))}")
    else:
        depth = "архив пока пуст"
    state = "🔴 пишет" if timelapse.enabled() else "⏸ остановлен"
    return (f"📹 <b>Таймлапс</b> — {state}\n"
            f"Кадр раз в {timelapse.interval()} сек · {depth}\n"
            f"<i>Старое стирается само, когда архив дорастает до "
            f"{timelapse.limit_mb() // 1024} ГБ.</i>")


async def section_timelapse(update: Update, ctx) -> None:
    await update.message.reply_text(timelapse_text(), parse_mode=ParseMode.HTML,
                                    reply_markup=timelapse_kb())


def tl_setup_kb() -> InlineKeyboardMarkup:
    cam = "задняя" if timelapse.camera() == "0" else "фронтальная"
    px = timelapse.width()
    quality = {640: "экономно", 960: "средне", 1280: "детально", 1600: "максимум"}
    return ikb([
        [(f"📷 Камера: {cam}", "tl:set:cam")],
        [(f"🖼 Качество: {quality.get(px, px)} ({px}px)", "tl:set:px")],
        [(f"🔄 Поворот кадра: {timelapse.rotate()}°", "tl:set:rot")],
        [(f"💾 Лимит архива: {timelapse.limit_mb() // 1024} ГБ", "tl:set:limit")],
        [("↩️ Назад", "tl:menu")],
    ])


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
    BTN_BACK_CAM: section_back_cam,
    BTN_FRONT_CAM: section_front_cam,
    BTN_TIMELAPSE: section_timelapse,
    BTN_STATUS: section_status,
    BTN_TV: section_tv,
    BTN_WHO: section_who,
    BTN_HOME: section_home,
    BTN_NETS: section_nets,
    BTN_SETTINGS: section_settings,
}


def parse_moment(text: str) -> float | None:
    """Turn '15:10', '27.08 15:10' or '27.08.2026 15:10:30' into a timestamp.

    A bare time means today, unless that's still in the future — then it means
    yesterday, which is what you actually mean when you ask at one in the
    morning what the kitchen looked like at 23:40.
    """
    text = text.strip().replace(",", " ")
    now = dt.datetime.now()
    for fmt, has_date in (("%d.%m.%Y %H:%M:%S", True), ("%d.%m.%Y %H:%M", True),
                          ("%d.%m %H:%M:%S", True), ("%d.%m %H:%M", True),
                          ("%H:%M:%S", False), ("%H:%M", False)):
        try:
            got = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if has_date:
            if got.year == 1900:
                got = got.replace(year=now.year)
        else:
            got = now.replace(hour=got.hour, minute=got.minute,
                              second=got.second, microsecond=0)
            if got > now:
                got -= dt.timedelta(days=1)
        return got.timestamp()
    return None


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
    if waiting == "tl_time":
        moment = parse_moment(text)
        if moment is None:
            await update.message.reply_text(
                "Не разобрал время. Жду что-то вроде <code>15:10</code> "
                "или <code>27.08 15:10</code>.", parse_mode=ParseMode.HTML)
            ctx.user_data["await"] = "tl_time"
            return
        frames = await asyncio.to_thread(timelapse.nearest, moment, 3)
        if not frames:
            span = timelapse.bounds()
            have = (f"Архив держит период с "
                    f"{time.strftime('%d.%m %H:%M', time.localtime(span[0]))} по "
                    f"{time.strftime('%d.%m %H:%M', time.localtime(span[1]))}."
                    if span else "Архив пока пуст.")
            await update.message.reply_text(f"На это время кадров нет. {have}")
            return
        asked = time.strftime("%d.%m %H:%M:%S", time.localtime(moment))
        await update.message.reply_text(f"🕐 Ближайшее к <b>{asked}</b>:",
                                        parse_mode=ParseMode.HTML)
        await send_frames(update.message.chat, frames, "🕐")
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
        # a link on a sleeping TV used to just fail; now it turns the set on first
        if not await tv_wake_if_asleep(update.message.chat):
            return
        if await tv.youtube_play(vid):
            await update.message.reply_text("▶️ Включаю на телевизоре.")
        else:
            await update.message.reply_text(
                "Телевизор на связи, но ссылку не принял — попробуй ещё раз.")
        return

    await update.message.reply_text("Не понял. Пользуйся кнопками снизу.", reply_markup=MAIN_KB)


# ---------------------------------------------------------------- callbacks

async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = update.callback_query
    data = q.data or ""
    chat = q.message.chat

    if data.startswith("tl:"):
        await timelapse_cb(q, ctx, data.split(":", 1)[1])
        return

    if data.startswith("chart:"):
        await q.answer("Рисую…")
        await presence_chart(chat, int(data.split(":", 1)[1]))
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
        if data.endswith(":mix"):
            await chat.send_message(
                "🎛 <b>Телевизор</b>\nLG 50LF652V · 192.168.1.100\n"
                "<i>Питание идёт по ИК, всё остальное — по сети. Ссылку на YouTube "
                "можно просто прислать в чат, включится сразу на видео.</i>",
                parse_mode=ParseMode.HTML, reply_markup=tv_mix_kb())
        elif data.endswith(":net"):
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

    if data.startswith("tvc:"):
        # power, and only power, on the combined remote
        what = data.split(":", 1)[1]
        ok, msg = await ir.send("power")
        if not ok:
            await q.answer(f"ИК не сработал: {msg}"[:190], show_alert=True)
            return
        if what == "on":
            await q.answer("⏻ Включаю по ИК…")
            if await tv.wait_awake(45):
                await chat.send_message("📺 Телевизор проснулся, сеть на связи.")
            else:
                await chat.send_message(
                    "📺 Экран должен был включиться, но по сети телевизор пока молчит — "
                    "дай ему полминуты.")
        else:
            await q.answer("⏻ Выключаю по ИК")
        return

    if data.startswith("tvp:"):
        await tv_pointer_cb(q, ctx, data.split(":", 1)[1])
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

    # these three just open another keyboard — no sensors to wait for
    if data in ("home:mic", "home:torch", "home:say"):
        what = data.split(":", 1)[1]
        await q.answer()
        if what == "mic":
            await chat.send_message("Что послушать?", reply_markup=ikb([
                [("🎤 10 сек", "rec:10"), ("🎤 30 сек", "rec:30")],
                [("📝 Распознать речь", "stt:1")],
            ]))
        elif what == "torch":
            await chat.send_message("Вспышка и сигналы:", reply_markup=ikb([
                [("💡 Мигнуть", "torch:blink"), ("🔦 Включить", "torch:on")],
                [("🌑 Выключить", "torch:off"), ("📳 Вибро", "torch:vibe")],
            ]))
        else:
            ctx.user_data["await"] = "say"
            await chat.send_message("Напиши текст — телефон произнесёт его вслух.")
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


async def send_frames(chat, frames: list, header: str) -> None:
    """Post a handful of archive frames, each captioned with its own time."""
    for ts, path in frames:
        try:
            with open(path, "rb") as fh:
                await chat.send_photo(
                    fh, caption=f"{header} {time.strftime('%H:%M:%S', time.localtime(ts))}")
        except OSError:
            await chat.send_message("Кадр уже стёрт кольцевой записью.")


async def timelapse_cb(q, ctx, what: str) -> None:
    chat = q.message.chat

    if what == "toggle":
        store.set_flag("tl_on", not timelapse.enabled())
        await q.answer("Запись пошла" if timelapse.enabled() else "Запись остановлена")
        await q.edit_message_text(timelapse_text(), parse_mode=ParseMode.HTML,
                                  reply_markup=timelapse_kb())
        return

    if what == "menu":
        await q.answer()
        await q.edit_message_text(timelapse_text(), parse_mode=ParseMode.HTML,
                                  reply_markup=timelapse_kb())
        return

    if what.startswith("clip:"):
        seconds = int(what.split(":", 1)[1])
        await q.answer("Собираю…")
        note = await chat.send_message("⏳ Склеиваю кадры…")
        try:
            path, used, available = await timelapse.clip(seconds)
            if not path:
                await note.edit_text(
                    "За этот промежуток кадров нет. Запись включена? "
                    f"Интервал сейчас {timelapse.interval()} сек — "
                    "за 30 секунд в него попадает всего пара кадров.")
                return
            thinned = (f" из {available}" if available > used else "")
            await chat.send_action(ChatAction.UPLOAD_VIDEO)
            with open(path, "rb") as fh:
                await chat.send_animation(
                    fh, caption=f"📹 последние {_ago(seconds) if seconds >= 60 else f'{seconds} сек'} "
                                f"· {used} кадров{thinned}")
            os.remove(path)
            await note.delete()
        except Exception as e:
            log.warning("клип: %s", e)
            await note.edit_text(f"Не собралось: {type(e).__name__}")
        return

    if what == "last":
        await q.answer()
        span = timelapse.bounds()
        if not span:
            await chat.send_message("Архив пуст — запись ещё не сделала ни одного кадра.")
            return
        frames = timelapse.nearest(span[1], 1)
        await send_frames(chat, frames, "🖼")
        return

    if what == "attime":
        ctx.user_data["await"] = "tl_time"
        await q.answer()
        await chat.send_message(
            "Во сколько посмотреть? Напиши время — например <code>15:10</code>.\n"
            "<i>Верну три ближайших кадра. Можно и с датой: <code>27.08 15:10</code>.</i>",
            parse_mode=ParseMode.HTML)
        return

    if what == "interval":
        await q.answer()
        cur = timelapse.interval()
        row = [(f"{'▶️ ' if s == cur else ''}{s} сек", f"tl:iv:{s}")
               for s in timelapse.INTERVALS]
        await q.edit_message_text(
            "⏱ <b>Как часто снимать?</b>\n"
            "<i>Съёмка кадра занимает около трёх секунд, так что 5 сек — это почти "
            "непрерывная работа камеры: греется и ест батарею. 20 сек — спокойный режим.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=ikb([row[:3], row[3:], [("↩️ Назад", "tl:menu")]]))
        return

    if what.startswith("iv:"):
        store.put("tl_interval", what.split(":", 1)[1])
        await q.answer(f"Теперь раз в {timelapse.interval()} сек")
        await q.edit_message_text(timelapse_text(), parse_mode=ParseMode.HTML,
                                  reply_markup=timelapse_kb())
        return

    if what == "setup":
        await q.answer()
        await q.edit_message_text("⚙️ <b>Настройки записи</b>", parse_mode=ParseMode.HTML,
                                  reply_markup=tl_setup_kb())
        return

    if what.startswith("set:"):
        # every one of these just steps to the next value in a short list
        key = what.split(":", 1)[1]
        if key == "cam":
            store.put("tl_camera", "1" if timelapse.camera() == "0" else "0")
        elif key == "px":
            steps = [640, 960, 1280, 1600]
            store.put("tl_width", steps[(steps.index(timelapse.width()) + 1) % len(steps)]
                      if timelapse.width() in steps else 1280)
        elif key == "rot":
            store.put("tl_rotate", (timelapse.rotate() + 90) % 360)
        elif key == "limit":
            steps = [1024, 2048, 5120, 7168]
            store.put("tl_limit_mb", steps[(steps.index(timelapse.limit_mb()) + 1) % len(steps)]
                      if timelapse.limit_mb() in steps else 5120)
        await q.answer("Поменял")
        await q.edit_message_reply_markup(reply_markup=tl_setup_kb())
        return

    if what == "stats":
        await q.answer("Считаю…")
        span = await asyncio.to_thread(timelapse.bounds)
        frames = await asyncio.to_thread(timelapse.count)
        size = await asyncio.to_thread(timelapse.archive_size_mb)
        if span:
            oldest, newest = span
            depth = (f"с {time.strftime('%d.%m %H:%M', time.localtime(oldest))} "
                     f"по {time.strftime('%d.%m %H:%M', time.localtime(newest))}")
        else:
            depth = "пусто"
        per_day = 86400 // max(1, timelapse.interval())
        mb_day = (size / max(1, frames)) * per_day if frames else 0
        await chat.send_message(
            f"📊 <b>Архив таймлапса</b>\n"
            f"Кадров: {frames} · {size} МБ из {timelapse.limit_mb()} МБ\n"
            f"Охват: {depth}\n"
            f"Расход: ~{mb_day:.0f} МБ в сутки при интервале {timelapse.interval()} сек\n"
            f"<i>Когда упрёмся в лимит, самые старые кадры начнут стираться сами.</i>",
            parse_mode=ParseMode.HTML)
        return

    await q.answer()


async def tv_pointer_cb(q, ctx, what: str) -> None:
    """The browser trackpad. Every press nudges the Magic Remote cursor."""
    t = tv_net()
    if t is None:
        await q.answer("Телевизор не спарен по сети", show_alert=True)
        return
    if what == "step":
        steps = [20, 60, 120, 250]
        cur = pointer_step()
        store.put("tv_pointer_step", steps[(steps.index(cur) + 1) % len(steps)]
                  if cur in steps else 60)
        await q.answer(f"Шаг {pointer_step()} px")
        await q.edit_message_reply_markup(reply_markup=tv_pointer_kb(pointer_step()))
        return
    try:
        if what.startswith("m:"):
            _, dx, dy = what.split(":")
            await t.move(int(dx), int(dy))
            await q.answer(f"{dx}, {dy}")
        elif what == "click":
            await t.click()
            await q.answer("👆")
        elif what.startswith("s:"):
            await t.scroll(int(what.split(":", 1)[1]))
            await q.answer("прокрутил")
    except Exception as e:
        await q.answer(f"Курсор не поехал: {type(e).__name__}", show_alert=True)


async def tv_wake_if_asleep(chat) -> bool:
    """IR power-on, but only if the set isn't already up. Returns True if it's awake."""
    if await tv.reachable():
        return True
    ok, msg = await ir.send("power")
    if not ok:
        await chat.send_message(f"Телевизор спит, а ИК не сработал: {msg}")
        return False
    note = await chat.send_message("⏻ Телевизор был выключен — включаю и жду сеть…")
    awake = await tv.wait_awake(50)
    try:
        await note.delete()
    except Exception:
        pass
    if not awake:
        await chat.send_message("📺 Экран включился, но по сети телевизор ещё не отвечает.")
    return awake


async def tv_net_cb(q, ctx, what: str) -> None:
    """Everything the wired remote can do. One place, one error style."""
    chat = q.message.chat
    t = tv_net()
    if what == "on":
        # The set kills its Ethernet port in standby — it doesn't even answer ARP,
        # so there is nothing left to receive a magic packet. Measured, not guessed.
        # The IR blaster is the only way in while it's asleep, so the button says so.
        tv.wake()  # harmless, and works if the TV ever gains a network-standby mode
        ok, msg = await ir.send("power")
        await q.answer("⏻ Включаю по ИК…" if ok else f"ИК не сработал: {msg}"[:190],
                       show_alert=not ok)
        if ok:
            for _ in range(12):
                await asyncio.sleep(3)
                if await tv.reachable():
                    await chat.send_message("📺 Телевизор проснулся, сеть на связи.")
                    return
            await chat.send_message(
                "📺 Экран должен был включиться, но по сети телевизор пока не отвечает — "
                "дай ему полминуты.")
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

        elif what == "youtube":
            # The one-tap macro: wake the set if it's off, then go straight
            # into the app. Before, that was power, wait, apps, scroll, OK.
            await q.answer("Открываю YouTube…")
            if not await tv_wake_if_asleep(chat):
                return
            await t.launch(tv.YOUTUBE_ID)
            await chat.send_message(
                "▶️ YouTube запущен.\n<i>Повиснет на загрузке — пришли ссылку "
                "на видео сюда, оно откроется мимо меню.</i>",
                parse_mode=ParseMode.HTML)

        elif what == "browser":
            await q.answer("Открываю браузер…")
            if not await tv_wake_if_asleep(chat):
                return
            await t.launch(tv.BROWSER_ID)
            await chat.send_message(
                "🌐 <b>Браузер</b> — управление курсором\n"
                "<i>У webOS нет «поставить курсор сюда»: он двигается только "
                "рывками, как по тачпаду. Шаг переключается кнопкой снизу — "
                "крупным добираешься до места, мелким целишься.</i>",
                parse_mode=ParseMode.HTML, reply_markup=tv_pointer_kb(pointer_step()))

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


def _presence_bars(hours: int) -> tuple[list[str], list[list[tuple[float, float]]], float, float]:
    """Turn the event log into (names, [(start, width) …] per device, t0, t1).

    Widths are in hours, measured from t0, which is what broken_barh wants.
    """
    now = time.time()
    t0 = now - hours * 3600
    state = store.presence_state_at(int(t0))
    events = store.presence_since(int(t0))
    labels = {d["mac"]: (d["label"] or d["mac"]) for d in store.device_list()}

    macs = list(dict.fromkeys(list(state) + [e["mac"] for e in events]))
    spans: dict[str, list[tuple[float, float]]] = {m: [] for m in macs}
    open_since = {m: (t0 if state.get(m) else None) for m in macs}

    for e in events:
        mac, ts = e["mac"], float(e["ts"])
        if e["online"]:
            if open_since.get(mac) is None:
                open_since[mac] = ts
        elif open_since.get(mac) is not None:
            spans.setdefault(mac, []).append(((open_since[mac] - t0) / 3600,
                                              (ts - open_since[mac]) / 3600))
            open_since[mac] = None
    for mac, started in open_since.items():
        if started is not None:
            spans.setdefault(mac, []).append(((started - t0) / 3600, (now - started) / 3600))

    # busiest devices first, and never more rows than fit on a phone screen
    ranked = sorted(spans.items(), key=lambda kv: sum(w for _, w in kv[1]), reverse=True)[:12]
    ranked.reverse()  # matplotlib draws the first row at the bottom
    names = [labels.get(m, m) for m, _ in ranked]
    return names, [s for _, s in ranked], t0, now


def _draw_presence(hours: int, path: str) -> bool:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names, spans, t0, now = _presence_bars(hours)
    if not names:
        return False
    fig, ax = plt.subplots(figsize=(9, 0.5 * len(names) + 1.6), dpi=110)
    for i, bars in enumerate(spans):
        ax.broken_barh(bars, (i - 0.35, 0.7), facecolors="#3aa675")
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names, fontsize=9)
    ax.set_xlim(0, hours)
    ticks = list(range(0, hours + 1, max(1, hours // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [time.strftime("%H:%M", time.localtime(t0 + t * 3600)) for t in ticks], fontsize=8)
    ax.grid(axis="x", alpha=0.25, linestyle=":")
    ax.set_title(f"Кто дома — последние {hours} ч", fontsize=11)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True


async def presence_chart(chat, hours: int = 24) -> None:
    path = os.path.join(actions.TMP, f"presence_{int(time.time())}.png")
    try:
        ok = await asyncio.to_thread(_draw_presence, hours, path)
    except Exception as e:
        log.warning("график присутствия: %s", e)
        await chat.send_message(f"График не нарисовался: {type(e).__name__}")
        return
    if not ok:
        await chat.send_message(
            "Истории присутствия пока нет — она копится с этого момента, "
            "по мере того как устройства приходят и уходят.")
        return
    with open(path, "rb") as fh:
        await chat.send_photo(fh, caption=f"📈 Кто дома, последние {hours} ч",
                              reply_markup=ikb([[("24 ч", "chart:24"), ("3 дня", "chart:72"),
                                                 ("неделя", "chart:168")]]))
    try:
        os.remove(path)
    except OSError:
        pass


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
        # One screen instead of four buttons: thermometer, battery, light and
        # the network line all come from here now.
        b, wifi, lux, net_up = await asyncio.gather(
            actions.battery(), actions.wifi_info(),
            actions.light_level(), netscan.internet_up(),
        )
        plugged = b.get("plugged", "") != "UNPLUGGED"
        pct = b.get("percentage", "?")
        power = "от сети" if plugged else "от батареи"

        if lux is None:
            light_line = "💡 датчик освещённости не ответил"
        else:
            mood = "темно" if lux < 10 else ("сумрак" if lux < 80 else "светло")
            light_line = f"💡 {lux:.0f} лк — {mood}"

        ssid = wifi.get("ssid") or ""
        # Android hands out "<unknown ssid>" when the location permission is
        # missing, and an empty string when wifi is off. Neither is a network
        # name, so don't print either of them as if it were one.
        if ssid in ("", "<unknown ssid>", "0x", "null"):
            wifi_line = "📶 Wi-Fi: <b>Отключено</b>"
        else:
            wifi_line = (f"📶 Wi-Fi: <b>Подключено</b> · {html.escape(ssid)}\n"
                         f"   {wifi.get('link_speed_mbps','?')} Мбит/с · "
                         f"{wifi.get('rssi','?')} дБм · "
                         f"{'5' if (wifi.get('frequency_mhz') or 0) > 3000 else '2.4'} ГГц")

        span = timelapse.bounds()
        tl_line = ("📹 таймлапс пишет, кадр раз в "
                   f"{timelapse.interval()} сек" if timelapse.enabled()
                   else "📹 таймлапс остановлен")
        if span:
            tl_line += f" · глубина {_ago(time.time() - span[0])}"

        await chat.send_message(
            f"📊 <b>Статус</b>\n"
            f"🌡 {b.get('temperature','?')} °C\n"
            f"🔋 {pct}% · {power} · {b.get('health','?')}\n"
            f"{light_line}\n"
            f"{wifi_line}\n"
            f"🌐 Интернет: <b>{'Подключено' if net_up else 'Отключено'}</b>\n"
            f"👥 Устройств в сети: {len(store.device_list(online_only=True))}\n"
            f"{tl_line}",
            parse_mode=ParseMode.HTML,
        )

    elif what == "chart":
        await presence_chart(chat)


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
    app.bot_data["timelapse"] = asyncio.create_task(timelapse.recorder(send))
    store.presence_trim()
    log.info("сторож запущен: %d наблюдателей, таймлапс раз в %d с",
             len(app.bot_data["watchers"]), timelapse.interval())


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_cb))
    log.info("Home Asis запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
