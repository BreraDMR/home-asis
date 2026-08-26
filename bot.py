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


async def section_tv(update: Update, ctx) -> None:
    await update.message.reply_text(
        "📺 <b>LG 50LF652V</b>\n<i>Раздел в разработке: коды стандартные для LG, "
        "но на твоём телевизоре ещё не проверялись. Начни с «Питание».</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=ikb([
            [("⏻ Питание", "tv:power"), ("🔇 Звук", "tv:mute")],
            [("🔊 Громче", "tv:vol_up"), ("🔉 Тише", "tv:vol_down")],
            [("📺 Канал +", "tv:ch_up"), ("📺 Канал −", "tv:ch_down")],
            [("⬆️", "tv:up")],
            [("⬅️", "tv:left"), ("OK", "tv:ok"), ("➡️", "tv:right")],
            [("⬇️", "tv:down")],
            [("🔌 Источник", "tv:input"), ("🏠 Меню", "tv:home")],
            [("↩️ Назад", "tv:back"), ("✖️ Выход", "tv:exit")],
        ]),
    )


async def section_home(update: Update, ctx) -> None:
    await update.message.reply_text(
        "Что показать по дому?",
        reply_markup=ikb([
            [("👥 Кто дома", "home:devices"), ("📶 Сети рядом", "home:networks")],
            [("🌡 Термометр", "home:temp"), ("🔋 Батарея", "home:batt")],
            [("💡 Освещённость", "home:light"), ("📊 Статус", "home:status")],
            [("✏️ Подписать устройство", "label:dev"), ("✏️ Подписать сеть", "label:net")],
        ]),
    )


def settings_kb() -> InlineKeyboardMarkup:
    def mark(key: str, default: bool = True) -> str:
        return "✅" if store.flag(key, default) else "❌"

    return ikb([
        [(f"{mark('notify_devices')} Устройства сети", "set:notify_devices")],
        [(f"{mark('notify_networks')} Сети вокруг", "set:notify_networks")],
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
    if isinstance(waiting, tuple) and waiting[0] == "label_dev":
        store.label_device(waiting[1], text)
        await update.message.reply_text(f"✏️ Устройство подписано: <b>{html.escape(text)}</b>",
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

    if data.startswith("tv:"):
        button = data.split(":", 1)[1]
        ok, msg = await ir.send(button)
        await q.answer("📺 " + (button if ok else msg[:60]), show_alert=not ok)
        return

    if data.startswith("home:"):
        what = data.split(":", 1)[1]
        await q.answer()
        await chat.send_action(ChatAction.TYPING)
        await home_report(chat, what)
        return

    if data.startswith("set:"):
        key = data.split(":", 1)[1]
        # motion and climate are opt-in, everything else is on unless turned off
        default_on = key not in ("notify_motion", "notify_climate")
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
            await chat.send_message("Датчик освещённости не ответил.")
        else:
            mood = "темно" if lux < 10 else ("сумрак" if lux < 80 else "светло")
            await chat.send_message(f"💡 <b>{lux:.0f} лк</b> — {mood}", parse_mode=ParseMode.HTML)

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
