"""
max_bot.py
──────────
Слой интеграции с мессенджером MAX (platform-api.max.ru).

Переиспользует всю бизнес-логику и БД из bot.py:
    load, free_slots, save_event, delete_event, fmt_date, m2t, t2m, uid,
    next_working_days, user_events, is_subscription_active, OWNER, ...

У MAX нет ConversationHandler, поэтому состояние диалога каждого пользователя
храним в памяти процесса (dict). Если бот будет работать в нескольких
репликах одновременно — стоит перенести STATE в Redis/Postgres, но для
старта (1 replica на Railway, как у вас сейчас) словаря в памяти достаточно.

Подключение в основной run_all() из bot.py:

    from max_bot import handle_max_webhook, max_set_subscription
    web_app.router.add_post("/max", handle_max_webhook)
    await max_set_subscription(webhook_url + "/max")
"""

import os
import logging
import aiohttp
from datetime import date

from bot import (
    load, free_slots, save_event, delete_event, fmt_date, m2t, t2m, uid,
    next_working_days, user_events, is_subscription_active, OWNER,
)

logger = logging.getLogger("max_bot")

MAX_TOKEN = os.environ["MAX_BOT_TOKEN"]          # токен из MAX Developer Console
MAX_API   = "https://platform-api.max.ru"
HEADERS   = {"Authorization": MAX_TOKEN, "Content-Type": "application/json"}

# ── состояние диалога (chat_id -> dict) ────────────────────────────────────
STATE: dict[int, dict] = {}

ASK_DATE, ASK_SLOT, ASK_NAME, ASK_PHONE, CONFIRM = range(5)


# ── низкоуровневые вызовы MAX API ──────────────────────────────────────────

async def max_request(method: str, path: str, **kwargs):
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, MAX_API + path, headers=HEADERS, **kwargs
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                logger.error("MAX API error %s %s -> %s: %s",
                             method, path, resp.status, data)
            return data


async def max_send(chat_id: int, text: str, buttons: list[list[dict]] | None = None,
                    fmt: str = "markdown"):
    """Отправляет сообщение. buttons — список рядов кнопок [[{...}, {...}], ...]."""
    body = {"text": text, "format": fmt}
    if buttons:
        body["attachments"] = [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons},
        }]
    return await max_request(
        "POST", "/messages", params={"chat_id": chat_id}, json=body
    )


async def max_answer_callback(callback_id: str, text: str | None = None):
    body = {"callback_id": callback_id}
    if text:
        body["notification"] = text
    return await max_request("POST", "/answers", json=body)


def btn_cb(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text, "payload": payload}


async def max_set_subscription(webhook_url: str):
    """Регистрирует webhook при старте приложения (вызывать один раз)."""
    return await max_request("POST", "/subscriptions", json={
        "url": webhook_url,
        "update_types": ["message_created", "message_callback", "bot_started"],
    })


# ── UI ──────────────────────────────────────────────────────────────────────

def main_menu_buttons():
    return [
        [btn_cb("📅 Записаться", "book")],
        [btn_cb("🔄 Перенести запись", "reschedule")],
        [btn_cb("📋 Мои записи", "my_bookings")],
        [btn_cb("❌ Отменить запись", "cancel_booking")],
    ]


async def show_main_menu(chat_id: int, greeting: str = "Выберите действие:"):
    await max_send(chat_id, greeting, main_menu_buttons())


# ── сценарий брони ───────────────────────────────────────────────────────

async def start_booking(chat_id: int):
    d = load()
    days = next_working_days(d, 7)
    if not days:
        await max_send(chat_id, "Свободных дат на ближайшую неделю нет 🙁")
        return
    buttons = [[btn_cb(fmt_date(s), f"date:{s}")] for s in days]
    STATE[chat_id] = {"step": ASK_DATE}
    await max_send(chat_id, "Выберите дату:", buttons)


async def show_slots(chat_id: int, datestr: str):
    d = load()
    slots = free_slots(d, datestr)
    if not slots:
        await max_send(chat_id, "На эту дату свободных слотов нет, выберите другую дату.")
        return
    buttons = [[btn_cb(s, f"slot:{s}")] for s in slots]
    STATE[chat_id] = {"step": ASK_SLOT, "date": datestr}
    await max_send(chat_id, f"Свободное время на {fmt_date(datestr)}:", buttons)


async def ask_name(chat_id: int, slot: str):
    st = STATE.get(chat_id, {})
    st.update({"step": ASK_NAME, "slot": slot})
    STATE[chat_id] = st
    await max_send(chat_id, "Как вас зовут?")


async def ask_phone(chat_id: int, name: str):
    st = STATE.get(chat_id, {})
    st.update({"step": ASK_PHONE, "name": name})
    STATE[chat_id] = st
    await max_send(chat_id, "Оставьте номер телефона для связи:")


async def confirm_booking(chat_id: int, phone: str):
    st = STATE.get(chat_id, {})
    st["phone"] = phone
    STATE[chat_id] = st
    text = (
        f"Подтвердите запись:\n"
        f"📅 {fmt_date(st['date'])} в {st['slot']}\n"
        f"👤 {st['name']}\n"
        f"📞 {st['phone']}"
    )
    buttons = [[btn_cb("✅ Подтвердить", "confirm_book"),
                btn_cb("❌ Отмена", "cancel_conv")]]
    st["step"] = CONFIRM
    await max_send(chat_id, text, buttons)


async def finalize_booking(chat_id: int):
    st = STATE.pop(chat_id, None)
    if not st:
        await max_send(chat_id, "Сессия истекла, начните заново: /start")
        return
    d = load()
    slot = st["slot"]
    end = m2t(t2m(slot) + int(d["sd"]))
    ev = {
        "id": uid(), "t": "c", "n": st["name"], "d": st["date"],
        "st": slot, "en": end, "ph": st["phone"], "no": "", "tgid": chat_id,
    }
    save_event(ev)
    await max_send(chat_id, "✅ Запись подтверждена! Спасибо.")
    await show_main_menu(chat_id, "Что-нибудь ещё?")


# ── мои записи / отмена ──────────────────────────────────────────────────

async def show_my_bookings(chat_id: int):
    evs = user_events(chat_id)
    if not evs:
        await max_send(chat_id, "У вас пока нет записей.")
        return
    lines = [f"📅 {fmt_date(e['d'])} в {e['st']}" for e in evs]
    await max_send(chat_id, "Ваши записи:\n" + "\n".join(lines))


async def start_cancel(chat_id: int):
    evs = user_events(chat_id)
    if not evs:
        await max_send(chat_id, "У вас нет активных записей для отмены.")
        return
    buttons = [[btn_cb(f"{fmt_date(e['d'])} {e['st']}", f"del:{e['id']}")] for e in evs]
    await max_send(chat_id, "Какую запись отменить?", buttons)


# ── главный обработчик webhook ─────────────────────────────────────────────

async def handle_max_webhook(request):
    from aiohttp import web
    data = await request.json()
    update_type = data.get("update_type")

    # ── подписка / приветствие ──
    if update_type == "bot_started":
        chat_id = data["chat_id"]
        await show_main_menu(chat_id, "Здравствуйте! Это бот для записи. Выберите действие:")
        return web.Response(status=200)

    # ── нажатие кнопки ──
    if update_type == "message_callback":
        cb = data["callback"]
        chat_id = data["message"]["recipient"]["chat_id"]
        payload = cb["payload"]
        callback_id = cb["callback_id"]
        await max_answer_callback(callback_id)

        if not is_subscription_active() and chat_id != OWNER:
            await max_send(chat_id, "Бот временно недоступен.")
            return web.Response(status=200)

        if payload == "book":
            await start_booking(chat_id)
        elif payload.startswith("date:"):
            await show_slots(chat_id, payload.split(":", 1)[1])
        elif payload.startswith("slot:"):
            await ask_name(chat_id, payload.split(":", 1)[1])
        elif payload == "confirm_book":
            await finalize_booking(chat_id)
        elif payload == "cancel_conv":
            STATE.pop(chat_id, None)
            await show_main_menu(chat_id, "Отменено.")
        elif payload == "my_bookings":
            await show_my_bookings(chat_id)
        elif payload == "cancel_booking":
            await start_cancel(chat_id)
        elif payload.startswith("del:"):
            delete_event(payload.split(":", 1)[1])
            await max_send(chat_id, "Запись отменена.")
        return web.Response(status=200)

    # ── текстовые сообщения (имя/телефон в рамках диалога) ──
    if update_type == "message_created":
        msg = data["message"]
        chat_id = msg["recipient"]["chat_id"]
        text = (msg.get("body") or {}).get("text", "").strip()

        if not is_subscription_active() and chat_id != OWNER:
            await max_send(chat_id, "Бот временно недоступен.")
            return web.Response(status=200)

        if text == "/start":
            STATE.pop(chat_id, None)
            await show_main_menu(chat_id, "Здравствуйте! Выберите действие:")
            return web.Response(status=200)

        st = STATE.get(chat_id)
        if st and st.get("step") == ASK_NAME:
            await ask_phone(chat_id, text)
        elif st and st.get("step") == ASK_PHONE:
            await confirm_booking(chat_id, text)
        else:
            await show_main_menu(chat_id, "Не поняла команду, выберите действие:")
        return web.Response(status=200)

    return web.Response(status=200)
