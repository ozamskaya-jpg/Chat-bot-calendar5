import os
import json
import logging
import random
import signal
import string
import asyncio
from io import BytesIO
from datetime import datetime, date, timedelta
 
import psycopg2
import psycopg2.extras
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
    LabeledPrice,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
    PreCheckoutQueryHandler, ApplicationHandlerStop,
)
 
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
 
TOKEN     = os.environ["BOT_TOKEN"]
OWNER     = int(os.environ["OWNER_ID"])
DB_URL    = os.environ["DATABASE_URL"]
API_TOKEN = os.environ.get("API_TOKEN", "changemesecrettoken")
PORT      = int(os.environ.get("PORT", 8080))
 
# ── Подписка на бота (оплата владельцем через ЮKassa) ──────────────────────────
DEVELOPER_ID   = int(os.environ["DEVELOPER_ID"])           # ваш Telegram ID (не владельца)
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN", "")       # провайдер-токен ЮKassa из BotFather (можно добавить позже)
TRIAL_DAYS     = int(os.environ.get("TRIAL_DAYS", 90))      # 3 месяца пробного периода
DEFAULT_PRICE  = int(os.environ.get("SUB_PRICE", 900))      # цена по умолчанию, руб/мес
 
ASK_DATE, ASK_SLOT, ASK_NAME, ASK_PHONE, CONFIRM = range(5)
RSCH_PICK, RSCH_DATE, RSCH_SLOT = range(5, 8)
SET_MENU, SET_WD, SET_WS, SET_LS, SET_SD, SET_VAC, SET_DELVAC = range(10, 17)
 
RU_MONTHS = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]
RU_DAYS   = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
 
# ── DB ────────────────────────────────────────────────────────────────────────
 
def get_conn():
    return psycopg2.connect(DB_URL, sslmode="require")
 
 
def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS settings
                           (key TEXT PRIMARY KEY, value JSONB NOT NULL)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS events
                           (id TEXT PRIMARY KEY, type TEXT NOT NULL,
                            name TEXT NOT NULL, date TEXT NOT NULL,
                            start_time TEXT NOT NULL, end_time TEXT NOT NULL,
                            phone TEXT DEFAULT '', note TEXT DEFAULT '',
                            tgid BIGINT,
                            reminded_24h BOOLEAN DEFAULT FALSE,
                            reminded_1h  BOOLEAN DEFAULT FALSE)""")
            for col, col_type in [
                ("tgid",         "BIGINT"),
                ("phone",        "TEXT DEFAULT ''"),
                ("note",         "TEXT DEFAULT ''"),
                ("reminded_24h", "BOOLEAN DEFAULT FALSE"),
                ("reminded_1h",  "BOOLEAN DEFAULT FALSE"),
            ]:
                cur.execute(
                    f"ALTER TABLE events ADD COLUMN IF NOT EXISTS {col} {col_type}"
                )
            cur.execute("""CREATE TABLE IF NOT EXISTS vacations
                           (id SERIAL PRIMARY KEY,
                            date_start TEXT,
                            date_end   TEXT)""")
            # Миграция: гарантируем наличие колонок date_start/date_end
            cur.execute("ALTER TABLE vacations ADD COLUMN IF NOT EXISTS date_start TEXT")
            cur.execute("ALTER TABLE vacations ADD COLUMN IF NOT EXISTS date_end   TEXT")
            # Подписка на бота (владелец платит за доступ)
            cur.execute("""CREATE TABLE IF NOT EXISTS subscription
                           (id INTEGER PRIMARY KEY DEFAULT 1,
                            trial_start TEXT NOT NULL,
                            price INTEGER NOT NULL DEFAULT 900,
                            paid_until TEXT,
                            CHECK (id = 1))""")
            cur.execute(
                "INSERT INTO subscription (id, trial_start, price) "
                "VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING",
                (date.today().strftime("%Y-%m-%d"), DEFAULT_PRICE),
            )
            cur.execute("""INSERT INTO settings (key, value) VALUES
                           ('wd','[1,2,3,4,5]'),
                           ('ws','"09:00"'),
                           ('we','"18:00"'),
                           ('ls','"13:00"'),
                           ('le','"14:00"'),
                           ('sd','60')
                           ON CONFLICT (key) DO NOTHING""")
        conn.commit()
    finally:
        conn.close()
 
def load():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT key, value FROM settings")
            d = {r["key"]: r["value"] for r in cur.fetchall()}
            cur.execute("SELECT * FROM events ORDER BY date, start_time")
            def rget(row, key, default=None):
                try:
                    return row[key]
                except (KeyError, IndexError):
                    return default
            evs = [
                {"id": r["id"], "t": r["type"], "n": r["name"],
                 "d": r["date"], "st": r["start_time"], "en": r["end_time"],
                 "ph": rget(r, "phone", ""), "no": rget(r, "note", ""),
                 "tgid": rget(r, "tgid"),
                 "r24": rget(r, "reminded_24h", False),
                 "r1": rget(r, "reminded_1h", False)}
                for r in cur.fetchall()
            ]
            cur.execute("SELECT id, date_start AS s, date_end AS e FROM vacations")
            vacs = [{"id": r["id"], "s": r["s"], "e": r["e"]}
                    for r in cur.fetchall()]
        return {
            "wd": d.get("wd", [1,2,3,4,5]),
            "ws": d.get("ws", "09:00"),
            "we": d.get("we", "18:00"),
            "ls": d.get("ls", "13:00"),
            "le": d.get("le", "14:00"),
            "sd": d.get("sd", 60),
            "vacs": vacs,
            "evs":  evs,
        }
    finally:
        conn.close()
 
def save_settings(d):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for key in ["wd", "ws", "we", "ls", "le", "sd"]:
                cur.execute(
                    "UPDATE settings SET value=%s WHERE key=%s",
                    (json.dumps(d[key]), key),
                )
        conn.commit()
    finally:
        conn.close()
 
def add_vacation(datestart: str, dateend: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO vacations (date_start, date_end) VALUES (%s, %s)",
                (datestart, dateend),
            )
        conn.commit()
    finally:
        conn.close()
 
def del_vacation(vac_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vacations WHERE id=%s", (vac_id,))
        conn.commit()
    finally:
        conn.close()
 
# ── Подписка на бота ─────────────────────────────────────────────────────────
 
def load_subscription():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT trial_start, price, paid_until FROM subscription WHERE id=1")
            r = cur.fetchone()
            return {
                "trial_start": r["trial_start"],
                "price": r["price"],
                "paid_until": r["paid_until"],
            }
    finally:
        conn.close()
 
def is_subscription_active():
    """True, если пробный период ещё идёт ИЛИ подписка оплачена и не истекла."""
    sub   = load_subscription()
    today = date.today()
    trial_end = (
        datetime.strptime(sub["trial_start"], "%Y-%m-%d").date()
        + timedelta(days=TRIAL_DAYS)
    )
    if today <= trial_end:
        return True
    if sub["paid_until"]:
        paid_until = datetime.strptime(sub["paid_until"], "%Y-%m-%d").date()
        if today <= paid_until:
            return True
    return False
 
def trial_days_left():
    sub = load_subscription()
    trial_end = (
        datetime.strptime(sub["trial_start"], "%Y-%m-%d").date()
        + timedelta(days=TRIAL_DAYS)
    )
    return (trial_end - date.today()).days
 
def extend_subscription(months: int = 1):
    """Продлевает оплаченный период на N месяцев от сегодня или от paid_until, если он в будущем."""
    sub   = load_subscription()
    today = date.today()
    base  = today
    if sub["paid_until"]:
        paid_until = datetime.strptime(sub["paid_until"], "%Y-%m-%d").date()
        if paid_until > base:
            base = paid_until
    new_until = base + timedelta(days=30 * months)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE subscription SET paid_until=%s WHERE id=1",
                (new_until.strftime("%Y-%m-%d"),),
            )
        conn.commit()
    finally:
        conn.close()
    return new_until
 
def set_subscription_price(new_price: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE subscription SET price=%s WHERE id=1", (new_price,))
        conn.commit()
    finally:
        conn.close()
 
def save_event(ev):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO events
                   (id, type, name, date, start_time, end_time, phone, note, tgid)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (ev["id"], ev["t"], ev["n"], ev["d"],
                 ev["st"], ev["en"], ev.get("ph", ""),
                 ev.get("no", ""), ev.get("tgid")),
            )
        conn.commit()
    finally:
        conn.close()
 
def update_event(eid, newdate, newst, newen):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE events
                   SET date=%s, start_time=%s, end_time=%s,
                       reminded_24h=FALSE, reminded_1h=FALSE
                   WHERE id=%s""",
                (newdate, newst, newen, eid),
            )
        conn.commit()
    finally:
        conn.close()
 
def delete_event(eid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE id=%s", (eid,))
        conn.commit()
    finally:
        conn.close()
 
def mark_reminded(eid, col):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE events SET {col}=TRUE WHERE id=%s", (eid,))
        conn.commit()
    finally:
        conn.close()
 
def import_events(new_evs):
    added = skipped = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for ev in new_evs:
                if not ev.get("id"):
                    ev["id"] = uid()
                cur.execute(
                    """INSERT INTO events
                       (id, type, name, date, start_time, end_time, phone, note, tgid)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (id) DO NOTHING""",
                    (ev["id"], ev.get("t","c"), ev.get("n",""), ev.get("d",""),
                     ev.get("st",""), ev.get("en",""),
                     ev.get("ph",""), ev.get("no",""), ev.get("tgid")),
                )
                if cur.rowcount:
                    added += 1
                else:
                    skipped += 1
        conn.commit()
        return added, skipped
    finally:
        conn.close()
 
# ── Utils ─────────────────────────────────────────────────────────────────────
 
def t2m(t):
    h, m = map(int, t.split(":"))
    return h * 60 + m
 
def m2t(m):
    return f"{m // 60:02d}:{m % 60:02d}"
 
def uid():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
 
def is_vac(d, s):
    return any(v["s"] <= s <= v["e"] for v in d.get("vacs", []))
 
def ev_of(d, s):
    return [e for e in d.get("evs", []) if e["d"] == s]
 
def free_slots(d, s):
    dt = datetime.strptime(s, "%Y-%m-%d")
    js_dow = dt.weekday() + 1
    if js_dow == 7:
        js_dow = 0
    if is_vac(d, s) or js_dow not in d["wd"]:
        return []
    ws_m = t2m(d["ws"])
    we_m = t2m(d["we"])
    ls_m = t2m(d["ls"])
    le_m = t2m(d["le"])
    slot = int(d["sd"])
    ev   = ev_of(d, s)
    res  = []
    m    = ws_m
    while m + slot <= we_m:
        if not (ls_m <= m < le_m):
            busy = any(
                (t2m(e["st"]) <= m < t2m(e["en"]))
                or (m <= t2m(e["st"]) < m + slot)
                or (t2m(e["st"]) <= m and t2m(e["en"]) >= m + slot)
                for e in ev
            )
            if not busy:
                res.append(m2t(m))
        m += slot
    return res
 
def fmt_date(s):
    dt = datetime.strptime(s, "%Y-%m-%d")
    return f"{dt.day} {RU_MONTHS[dt.month - 1]} ({RU_DAYS[dt.weekday()]})"
 
def next_working_days(d, n=7):
    result  = []
    cur_day = date.today()
    while len(result) < n:
        s = cur_day.strftime("%Y-%m-%d")
        if free_slots(d, s):
            result.append(s)
        cur_day += timedelta(days=1)
    return result
 
def user_events(tgid):
    d = load()
    return [e for e in d["evs"]
            if e.get("tgid") == tgid and e.get("t") == "c"]
 
def owner_only(update: Update):
    uid_ = update.effective_user.id if update.effective_user else None
    return uid_ == OWNER
 
def developer_only(update: Update):
    uid_ = update.effective_user.id if update.effective_user else None
    return uid_ == DEVELOPER_ID
 
def main_menu_kb(user_id: int = None):
    buttons = [
        [InlineKeyboardButton("📅 Записаться",        callback_data="book")],
        [InlineKeyboardButton("🔄 Перенести запись",  callback_data="reschedule")],
        [InlineKeyboardButton("📋 Мои записи",        callback_data="my_bookings")],
        [InlineKeyboardButton("❌ Отменить запись",   callback_data="cancel_booking")],
    ]
    if user_id == OWNER:
        buttons.append([InlineKeyboardButton("🗓 Открыть календарь", web_app=WebAppInfo(url="https://calendar-interface-finamira.netlify.app"))])
    return InlineKeyboardMarkup(buttons)
 
# ── HTTP API ──────────────────────────────────────────────────────────────────
 
def check_token(request: web.Request) -> bool:
    token = request.rel_url.query.get("token", "")
    if not token:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return token == API_TOKEN
 
async def api_get_events(request: web.Request) -> web.Response:
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    month = request.rel_url.query.get("month", "")
    d     = load()
    evs   = d["evs"]
    if month:
        evs = [e for e in evs if e["d"].startswith(month)]
    result = [
        {"id": e["id"], "type": e["t"], "name": e["n"],
         "date": e["d"], "starttime": e["st"], "endtime": e["en"],
         "phone": e["ph"], "note": e["no"]}
        for e in evs
    ]
    return web.json_response({"events": result})
 
async def api_get_slots(request: web.Request) -> web.Response:
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    datestr = request.rel_url.query.get("date", "")
    if not datestr:
        return web.json_response({"error": "date param required"}, status=400)
    try:
        d     = load()
        slots = free_slots(d, datestr)
        return web.json_response({"date": datestr, "slots": slots})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
 
async def api_book(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if data.get("token") != API_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    for field in ["date", "time", "name"]:
        if not data.get(field):
            return web.json_response({"error": f"Field {field} required"}, status=400)
    try:
        d       = load()
        datestr = data["date"]
        timestr = data["time"]
        slots   = free_slots(d, datestr)
        if timestr not in slots:
            return web.json_response(
                {"error": f"Slot {timestr} not available",
                 "available_slots": slots},
                status=409,
            )
        endtime = m2t(t2m(timestr) + int(d["sd"]))
        ev_id   = uid()
        ev = {
            "id": ev_id, "t": "c", "n": data["name"],
            "d": datestr, "st": timestr, "en": endtime,
            "ph": data.get("phone", ""), "no": data.get("note", ""),
            "tgid": None,
        }
        save_event(ev)
        return web.json_response({
            "success": True,
            "event": {
                "id": ev_id, "date": datestr,
                "starttime": timestr, "endtime": endtime,
                "name": data["name"], "phone": data.get("phone", ""),
            },
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
 
async def api_delete_event(request: web.Request) -> web.Response:
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    eid = request.rel_url.query.get("id", "")
    if not eid:
        return web.json_response({"error": "id param required"}, status=400)
    delete_event(eid)
    return web.json_response({"success": True, "deleted_id": eid})
 
async def api_create_event(request: web.Request) -> web.Response:
    """Generic event create from calendar: any type ('c','b','e'), no slot check."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if data.get("token") != API_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    for field in ["date", "starttime", "name"]:
        if not data.get(field):
            return web.json_response({"error": f"Field {field} required"}, status=400)
    try:
        d        = load()
        datestr  = data["date"]
        timestr  = data["starttime"]
        etype    = data.get("type", "c")
        if etype not in ("c", "b", "e"):
            etype = "c"
        endtime  = data.get("endtime") or m2t(t2m(timestr) + int(d["sd"]))
        ev_id    = uid()
        ev = {
            "id": ev_id, "t": etype, "n": data["name"],
            "d": datestr, "st": timestr, "en": endtime,
            "ph": data.get("phone", ""), "no": data.get("note", ""),
            "tgid": None,
        }
        save_event(ev)
        return web.json_response({
            "success": True,
            "event": {
                "id": ev_id, "type": etype, "date": datestr,
                "starttime": timestr, "endtime": endtime,
                "name": data["name"], "phone": data.get("phone", ""),
                "note": data.get("note", ""),
            },
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
 
async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": "chat-bot-calendar5"})
 
async def api_get_calendar_data(request: web.Request) -> web.Response:
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    d = load()
    return web.json_response({
        "settings": {
            "wd": d["wd"], "ws": d["ws"], "we": d["we"],
            "ls": d["ls"], "le": d["le"], "sd": d["sd"],
        },
        "vacs": [
            {"id": str(v["id"]), "s": v["s"], "e": v["e"]}
            for v in d.get("vacs", [])
        ],
        "events": [
            {"id": e["id"], "type": e["t"], "name": e["n"],
             "date": e["d"], "starttime": e["st"], "endtime": e["en"],
             "phone": e["ph"], "note": e["no"]}
            for e in d.get("evs", [])
        ],
    })
 
async def api_update_settings(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if data.get("token") != API_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for key in ["wd", "ws", "we", "ls", "le", "sd"]:
                    if key in data:
                        cur.execute(
                            "UPDATE settings SET value=%s WHERE key=%s",
                            (json.dumps(data[key]), key),
                        )
                if "vacs" in data:
                    cur.execute("DELETE FROM vacations")
                    for v in data["vacs"]:
                        cur.execute(
                            "INSERT INTO vacations (date_start, date_end) VALUES (%s, %s)",
                            (v["s"], v["e"]),
                        )
            conn.commit()
        finally:
            conn.close()
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
 
async def api_add_vacation(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if data.get("token") != API_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)
    add_vacation(data["s"], data["e"])
    return web.json_response({"success": True})
 
async def api_delete_vacation(request: web.Request) -> web.Response:
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    vac_id = request.rel_url.query.get("id", "")
    if not vac_id:
        return web.json_response({"error": "id required"}, status=400)
    del_vacation(int(vac_id))
    return web.json_response({"success": True})
 
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = web.Response(status=ex.status, text=ex.text)
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response
 
def create_web_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_route("OPTIONS", "/{path_info:.*}", lambda r: web.Response())
    app.router.add_get   ("/health",            api_health)
    app.router.add_get   ("/events",            api_get_events)
    app.router.add_get   ("/slots",             api_get_slots)
    app.router.add_post  ("/book",              api_book)
    app.router.add_post  ("/event",             api_create_event)
    app.router.add_delete("/event",             api_delete_event)
    app.router.add_get   ("/calendar-data",     api_get_calendar_data)
    app.router.add_post  ("/calendar-settings", api_update_settings)
    app.router.add_post  ("/vacation",          api_add_vacation)
    app.router.add_delete("/vacation",          api_delete_vacation)
    return app
 
# ── Reminders ─────────────────────────────────────────────────────────────────
 
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    d   = load()
    now = datetime.now()
    for ev in d["evs"]:
        if ev["t"] != "c" or not ev.get("tgid"):
            continue
        try:
            ev_dt = datetime.strptime(ev["d"] + " " + ev["st"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        diff_min = (ev_dt - now).total_seconds() / 60
        if not ev.get("r24") and 1435 < diff_min <= 1440:
            try:
                d_str = fmt_date(ev["d"])
                await context.bot.send_message(
                    ev["tgid"],
                    "⏰ Напоминание! Завтра запись: "
                    + d_str + ", " + ev["st"] + "\u2013" + ev["en"]
                    + "\n*" + ev["n"] + "*\nНапишите /start чтобы управлять записью.",
                    parse_mode="Markdown",
                )
                mark_reminded(ev["id"], "reminded_24h")
            except Exception as e:
                logger.warning("Reminder 24h error for %s: %s", ev["id"], e)
        if not ev.get("r1") and 55 < diff_min <= 65:
            try:
                d_str = fmt_date(ev["d"])
                await context.bot.send_message(
                    ev["tgid"],
                    "⏰ Скоро запись! " + d_str + ", "
                    + ev["st"] + "\u2013" + ev["en"]
                    + "\n*" + ev["n"] + "* \u2014 уже через час!",
                    parse_mode="Markdown",
                )
                mark_reminded(ev["id"], "reminded_1h")
            except Exception as e:
                logger.warning("Reminder 1h error for %s: %s", ev["id"], e)
 
# ── /start  /cancel ───────────────────────────────────────────────────────────
 
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    uid_ = update.effective_user.id if update.effective_user else None
 
    if not is_subscription_active():
        if uid_ == OWNER:
            sub = load_subscription()
            await update.message.reply_text(
                "⛔ Пробный период закончился, и доступ к боту приостановлен.\n"
                "Чтобы клиенты снова могли записываться, оформите подписку.\n\n"
                "💳 Стоимость: *" + str(sub["price"]) + " ₽ / мес*\n\n"
                "Нажмите /subscribe для оплаты.",
                parse_mode="Markdown",
            )
            return
        else:
            await update.message.reply_text(
                "🙁 Бот временно недоступен. Пожалуйста, обратитесь напрямую к администратору."
            )
            return
 
    await update.message.reply_text(
        "👋 Привет! Выберите действие:",
        reply_markup=main_menu_kb(update.effective_user.id),
    )
 
async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        await update.message.reply_text("⛔ Эта команда доступна только владельцу бота.")
        return
    if not PROVIDER_TOKEN:
        await update.message.reply_text(
            "⏳ Оплата подписки пока не подключена — ждём подтверждения от ЮKassa.\n"
            "Как только всё будет готово, здесь появится возможность оплатить."
        )
        return
    sub   = load_subscription()
    price = sub["price"]
    days_left = trial_days_left()
    if days_left > 0 and not sub["paid_until"]:
        await update.message.reply_text(
            "ℹ️ Пробный период ещё активен — осталось *" + str(days_left) + "* дн.\n"
            "Можете оплатить уже сейчас, чтобы продлить доступ заранее.",
            parse_mode="Markdown",
        )
    price_year = round(price * 12 * 0.9)  # скидка 10% на годовую подписку
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "1 месяц — " + str(price) + " ₽", callback_data="sub_pay:month"
        )],
        [InlineKeyboardButton(
            "1 год — " + str(price_year) + " ₽ (скидка 10%)", callback_data="sub_pay:year"
        )],
    ])
    await update.message.reply_text(
        "Выберите период подписки:", reply_markup=kb
    )
 
async def cb_subscribe_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not owner_only(update):
        await query.answer("⛔ Доступно только владельцу бота.", show_alert=True)
        return
    await query.answer()
    period = query.data.replace("sub_pay:", "")  # "month" или "year"
    await send_subscription_invoice(update.effective_chat.id, ctx, period)
 
async def send_subscription_invoice(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, period: str):
    sub   = load_subscription()
    price = sub["price"]
    if period == "year":
        amount      = round(price * 12 * 0.9)
        title       = "Подписка на бот-календарь — 1 год"
        description = "Продление доступа к боту на 12 месяцев (скидка 10%)"
        payload     = "subscription:1year"
        label       = "Подписка на 1 год"
        item_desc   = "Подписка на бот-календарь (12 месяцев, скидка 10%)"
    else:
        amount      = price
        title       = "Подписка на бот-календарь — 1 месяц"
        description = "Продление доступа к боту на 1 месяц"
        payload     = "subscription:1month"
        label       = "Подписка на 1 месяц"
        item_desc   = "Подписка на бот-календарь (1 месяц)"
 
    receipt = {
        "receipt": {
            "items": [
                {
                    "description": item_desc,
                    "quantity": "1.00",
                    "amount": {
                        "value": "{:.2f}".format(amount),
                        "currency": "RUB",
                    },
                    "vat_code": 1,
                }
            ]
        }
    }
    await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label, amount * 100)],  # в копейках
        need_email=True,
        send_email_to_provider=True,
        provider_data=json.dumps(receipt),
    )
 
async def cb_precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload not in ("subscription:1month", "subscription:1year"):
        await query.answer(ok=False, error_message="Неверный платёж.")
        return
    await query.answer(ok=True)
 
async def cb_successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    months  = 12 if payload == "subscription:1year" else 1
    new_until = extend_subscription(months)
    period_label = "1 год" if months == 12 else "1 месяц"
    await update.message.reply_text(
        "✅ Оплата получена (" + period_label + ")! Подписка активна до *"
        + new_until.strftime("%d.%m.%Y") + "*.\nБот снова доступен клиентам.",
        parse_mode="Markdown",
    )
    await ctx.bot.send_message(
        DEVELOPER_ID,
        "💰 Получена оплата подписки (" + period_label + ") от владельца бота. Активна до "
        + new_until.strftime("%d.%m.%Y") + ".",
    )
 
async def cmd_setprice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not developer_only(update):
        return  # тихо игнорируем, чтобы не раскрывать существование команды
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /setprice 900")
        return
    new_price = int(args[0])
    set_subscription_price(new_price)
    await update.message.reply_text("✅ Новая цена подписки: " + str(new_price) + " ₽/мес")
 
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Действие отменено.",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END
 
# ── Booking flow ──────────────────────────────────────────────────────────────
 
async def cb_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d    = load()
    days = next_working_days(d, 14)
    if not days:
        await update.callback_query.edit_message_text("😔 Нет свободных дней.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(fmt_date(s), callback_data="date:" + s)] for s in days]
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conv")])
    await update.callback_query.edit_message_text(
        "Выберите дату:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return ASK_DATE
 
async def cb_ask_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    s = update.callback_query.data.replace("date:", "")
    ctx.user_data["date"] = s
    d     = load()
    slots = free_slots(d, s)
    if not slots:
        await update.callback_query.edit_message_text("😔 На этот день нет слотов.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(sl, callback_data="slot:" + sl)] for sl in slots]
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conv")])
    await update.callback_query.edit_message_text(
        "Дата: *" + fmt_date(s) + "*\nВыберите время:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return ASK_SLOT
 
async def cb_ask_slot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["slot"] = update.callback_query.data.replace("slot:", "")
    await update.callback_query.edit_message_text("Введите ваше имя:")
    return ASK_NAME
 
async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите номер телефона (или «-» если не хотите оставлять):"
    )
    return ASK_PHONE
 
async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["phone"] = update.message.text.strip()
    d_str = ctx.user_data["date"]
    sl    = ctx.user_data["slot"]
    name  = ctx.user_data["name"]
    phone = ctx.user_data["phone"]
    d     = load()
    end   = m2t(t2m(sl) + int(d["sd"]))
    kb    = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_book")],
        [InlineKeyboardButton("❌ Отмена",       callback_data="cancel_conv")],
    ])
    await update.message.reply_text(
        "*" + fmt_date(d_str) + "*, " + sl + "\u2013" + end
        + "\n👤 " + name + "\n📞 " + phone,
        reply_markup=kb,
        parse_mode="Markdown",
    )
    return CONFIRM
 
async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d_str = ctx.user_data["date"]
    sl    = ctx.user_data["slot"]
    name  = ctx.user_data["name"]
    phone = ctx.user_data["phone"]
    d     = load()
    end   = m2t(t2m(sl) + int(d["sd"]))
    ev    = {
        "id": uid(), "t": "c", "n": name,
        "d": d_str, "st": sl, "en": end,
        "ph": phone if phone != "-" else "",
        "no": "", "tgid": update.effective_user.id,
    }
    save_event(ev)
    ctx.user_data.clear()
    await update.callback_query.edit_message_text(
        "✅ Записано!\n*" + fmt_date(d_str) + "*, " + sl + "\u2013" + end
        + "\n👤 " + name,
        parse_mode="Markdown",
    )
    await ctx.bot.send_message(
        OWNER,
        "🔔 Новая запись!\n👤 " + name + "  📞 " + phone
        + "\n📅 " + fmt_date(d_str) + ", " + sl + "\u2013" + end,
    )
    return ConversationHandler.END
 
# ── My bookings / cancel ──────────────────────────────────────────────────────
 
async def cb_my_bookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    evs = user_events(update.effective_user.id)
    if not evs:
        await update.callback_query.edit_message_text("У вас нет активных записей.")
        return
    text = "\n".join(
        "📅 " + fmt_date(e["d"]) + ", " + e["st"] + "\u2013" + e["en"]
        + " — " + e["n"]
        for e in evs
    )
    await update.callback_query.edit_message_text(text)
 
async def cb_cancel_booking(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    evs = user_events(update.effective_user.id)
    if not evs:
        await update.callback_query.edit_message_text("У вас нет активных записей.")
        return
    kb = [
        [InlineKeyboardButton(
            fmt_date(e["d"]) + ", " + e["st"] + "\u2013" + e["en"],
            callback_data="del:" + e["id"],
        )]
        for e in evs
    ]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_start")])
    await update.callback_query.edit_message_text(
        "Выберите запись для отмены:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
 
async def cb_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    eid = update.callback_query.data.replace("del:", "")
    delete_event(eid)
    await update.callback_query.edit_message_text("✅ Запись отменена.")
    await ctx.bot.send_message(OWNER, "❌ Запись " + eid + " отменена пользователем.")
 
async def cb_back_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "Выберите действие:", reply_markup=main_menu_kb()
    )
 
# ── Reschedule ────────────────────────────────────────────────────────────────
 
async def cb_reschedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    evs = user_events(update.effective_user.id)
    if not evs:
        await update.callback_query.edit_message_text("У вас нет активных записей.")
        return ConversationHandler.END
    kb = [
        [InlineKeyboardButton(
            fmt_date(e["d"]) + ", " + e["st"] + "\u2013" + e["en"],
            callback_data="rpick:" + e["id"],
        )]
        for e in evs
    ]
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conv")])
    await update.callback_query.edit_message_text(
        "Выберите запись для переноса:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return RSCH_PICK
 
async def cb_rsch_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["rsch_id"] = update.callback_query.data.replace("rpick:", "")
    d    = load()
    days = next_working_days(d, 14)
    kb   = [[InlineKeyboardButton(fmt_date(s), callback_data="rdate:" + s)] for s in days]
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conv")])
    await update.callback_query.edit_message_text(
        "Выберите новую дату:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return RSCH_DATE
 
async def cb_rsch_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    s = update.callback_query.data.replace("rdate:", "")
    ctx.user_data["rsch_date"] = s
    d     = load()
    slots = free_slots(d, s)
    if not slots:
        await update.callback_query.edit_message_text("😔 На этот день нет слотов.")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(sl, callback_data="rslot:" + sl)] for sl in slots]
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conv")])
    await update.callback_query.edit_message_text(
        "Дата: *" + fmt_date(s) + "*\nВыберите время:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return RSCH_SLOT
 
async def cb_rsch_slot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    new_sl  = update.callback_query.data.replace("rslot:", "")
    new_d   = ctx.user_data["rsch_date"]
    eid     = ctx.user_data["rsch_id"]
    d       = load()
    new_end = m2t(t2m(new_sl) + int(d["sd"]))
    old_ev  = next((e for e in d["evs"] if e["id"] == eid), None)
    update_event(eid, new_d, new_sl, new_end)
    ctx.user_data.clear()
    await update.callback_query.edit_message_text(
        "✅ Перенесено!\n*" + fmt_date(new_d) + "*, " + new_sl + "\u2013" + new_end,
        parse_mode="Markdown",
    )
    if old_ev:
        await ctx.bot.send_message(
            OWNER,
            "🔄 Перенос записи " + old_ev["n"]
            + "\nБыло: " + fmt_date(old_ev["d"]) + ", " + old_ev["st"]
            + "\nСтало: " + fmt_date(new_d) + ", " + new_sl + "\u2013" + new_end,
        )
    return ConversationHandler.END
 
async def cb_cancel_conv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data.clear()
    await update.callback_query.edit_message_text(
        "❌ Отменено. Выберите действие:", reply_markup=main_menu_kb()
    )
    return ConversationHandler.END
 
# ── Settings (OWNER) ──────────────────────────────────────────────────────────
 
def settings_text(d: dict) -> str:
    wd_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    wd_str   = ", ".join(wd_names[w - 1] for w in sorted(d["wd"])) if d["wd"] else "—"
    vacs     = d.get("vacs", [])
    vacs_str = "\n".join(v["s"] + " \u2013 " + v["e"] for v in vacs) or "—"
    return (
        "⚙️ *Настройки*\n"
        "📅 Рабочие дни: " + wd_str + "\n"
        "🕐 Рабочее время: " + d["ws"] + "\u2013" + d["we"] + "\n"
        "🍽 Обед: " + d["ls"] + "\u2013" + d["le"] + "\n"
        "⏱ Длит. слота: " + str(d["sd"]) + " мин\n"
        "🏖 Отпуск/выходные:\n" + vacs_str
    )
 
def settings_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Рабочие дни",     callback_data="set_wd")],
        [InlineKeyboardButton("🕐 Рабочее время",   callback_data="set_wtime")],
        [InlineKeyboardButton("🍽 Обед",            callback_data="set_lunch")],
        [InlineKeyboardButton("⏱ Длит. слота",     callback_data="set_sd")],
        [InlineKeyboardButton("🏖 Добавить отпуск", callback_data="set_vac")],
        [InlineKeyboardButton("🗑 Удалить отпуск",  callback_data="set_delvac")],
        [InlineKeyboardButton("✅ Закрыть",         callback_data="set_close")],
    ])
 
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    d = load()
    await update.message.reply_text(
        settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
    )
    return SET_MENU
 
async def cb_set_wd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d       = load()
    current = d["wd"]
    days    = [(1,"Пн"),(2,"Вт"),(3,"Ср"),(4,"Чт"),(5,"Пт"),(6,"Сб"),(7,"Вс")]
    buttons = [
        [InlineKeyboardButton(
            ("✅ " if int(v) in current else "◻️ ") + label,
            callback_data="togwd:" + str(v),
        )]
        for v, label in days
    ]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_back")])
    await update.callback_query.edit_message_text(
        "Выберите рабочие дни:", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return SET_WD
 
async def cb_tog_wd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    dv      = int(update.callback_query.data.replace("togwd:", ""))
    d       = load()
    current = list(d["wd"])
    if dv in current:
        current.remove(dv)
    else:
        current.append(dv)
    d["wd"] = sorted(current)
    save_settings(d)
    days    = [(1,"Пн"),(2,"Вт"),(3,"Ср"),(4,"Чт"),(5,"Пт"),(6,"Сб"),(7,"Вс")]
    buttons = [
        [InlineKeyboardButton(
            ("✅ " if int(v) in d["wd"] else "◻️ ") + label,
            callback_data="togwd:" + str(v),
        )]
        for v, label in days
    ]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_back")])
    await update.callback_query.edit_message_text(
        "Выберите рабочие дни:", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return SET_WD
 
async def cb_set_wtime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d = load()
    await update.callback_query.edit_message_text(
        "Текущее: " + d["ws"] + "\u2013" + d["we"]
        + "\nВведите рабочее время в формате *ЧЧ:ММ ЧЧ:ММ* (например 10:00 19:00):",
        parse_mode="Markdown",
    )
    return SET_WS
 
async def got_wtime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return ConversationHandler.END
    try:
        parts = update.message.text.strip().split()
        ws, we = parts[0], parts[1]
        datetime.strptime(ws, "%H:%M")
        datetime.strptime(we, "%H:%M")
        d = load()
        d["ws"], d["we"] = ws, we
        save_settings(d)
        await update.message.reply_text("✅ Рабочее время: " + ws + "\u2013" + we)
    except Exception:
        await update.message.reply_text(
            "❌ Неверный формат. Пример: 10:00 19:00", parse_mode="Markdown"
        )
        return SET_WS
    d = load()
    await update.message.reply_text(
        settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
    )
    return SET_MENU
 
async def cb_set_lunch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d = load()
    await update.callback_query.edit_message_text(
        "Текущий обед: " + d["ls"] + "\u2013" + d["le"]
        + "\nВведите обед в формате *ЧЧ:ММ ЧЧ:ММ* или «нет»:",
        parse_mode="Markdown",
    )
    return SET_LS
 
async def got_lunch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return ConversationHandler.END
    text = update.message.text.strip().lower()
    d    = load()
    if text == "нет":
        d["ls"], d["le"] = "00:00", "00:00"
        save_settings(d)
        await update.message.reply_text("✅ Обед отключён.")
    else:
        try:
            parts = text.split()
            ls, le = parts[0], parts[1]
            datetime.strptime(ls, "%H:%M")
            datetime.strptime(le, "%H:%M")
            d["ls"], d["le"] = ls, le
            save_settings(d)
            await update.message.reply_text("✅ Обед: " + ls + "\u2013" + le)
        except Exception:
            await update.message.reply_text(
                "❌ Неверный формат. Пример: 13:00 14:00", parse_mode="Markdown"
            )
            return SET_LS
    d = load()
    await update.message.reply_text(
        settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
    )
    return SET_MENU
 
async def cb_set_sd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d  = load()
    sd = int(d["sd"])
    kb = [
        [InlineKeyboardButton(
            ("✅ " if sd == m else "") + str(m) + " мин",
            callback_data="setsd:" + str(m),
        )]
        for m in [15, 20, 30, 45, 60, 90, 120]
    ]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_back")])
    await update.callback_query.edit_message_text(
        "Текущий слот: " + str(sd) + " мин",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return SET_SD
 
async def cb_set_sd_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    sd_val  = int(update.callback_query.data.replace("setsd:", ""))
    d       = load()
    d["sd"] = sd_val
    save_settings(d)
    d = load()
    await update.callback_query.edit_message_text(
        settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
    )
    return SET_MENU
 
async def cb_set_vac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "Введите период отпуска в формате *ДД.ММ.ГГГГ ДД.ММ.ГГГГ*\nПример: 20.06.2026 30.06.2026",
        parse_mode="Markdown",
    )
    return SET_VAC
 
async def got_vac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return ConversationHandler.END
    try:
        parts = update.message.text.strip().split()
        ds = datetime.strptime(parts[0], "%d.%m.%Y").strftime("%Y-%m-%d")
        de = datetime.strptime(parts[1], "%d.%m.%Y").strftime("%Y-%m-%d")
        if de < ds:
            raise ValueError
        add_vacation(ds, de)
        await update.message.reply_text("✅ Отпуск: " + parts[0] + " \u2013 " + parts[1])
    except Exception:
        await update.message.reply_text(
            "❌ Неверный формат. Пример: 20.06.2026 30.06.2026", parse_mode="Markdown"
        )
        return SET_VAC
    d = load()
    await update.message.reply_text(
        settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
    )
    return SET_MENU
 
async def cb_set_delvac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d    = load()
    vacs = d.get("vacs", [])
    if not vacs:
        await update.callback_query.edit_message_text(
            settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
        )
        return SET_MENU
    buttons = [
        [InlineKeyboardButton(v["s"] + " \u2013 " + v["e"],
                              callback_data="delvac:" + str(v["id"]))]
        for v in vacs
    ]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_back")])
    await update.callback_query.edit_message_text(
        "Выберите отпуск для удаления:", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return SET_DELVAC
 
async def cb_del_vac(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    vac_id = int(update.callback_query.data.replace("delvac:", ""))
    del_vacation(vac_id)
    d = load()
    await update.callback_query.edit_message_text(
        settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
    )
    return SET_MENU
 
async def cb_set_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    d = load()
    await update.callback_query.edit_message_text(
        settings_text(d), parse_mode="Markdown", reply_markup=settings_kb()
    )
    return SET_MENU
 
async def cb_set_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("✅ Настройки сохранены.")
    return ConversationHandler.END
 
# ── JSON import ───────────────────────────────────────────────────────────────
 
async def handle_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    doc = update.message.document
    if not doc.file_name.endswith(".json"):
        return
    f   = await doc.get_file()
    bio = BytesIO()
    await f.download_to_memory(bio)
    bio.seek(0)
    try:
        data     = json.loads(bio.read().decode())
        new_evs  = data.get("evs", [])
        added, skipped = import_events(new_evs)
        await update.message.reply_text(
            "✅ Импортировано: " + str(added) + ", пропущено: " + str(skipped)
        )
    except Exception as e:
        await update.message.reply_text("❌ Ошибка: " + str(e))
 
# ── Build app ─────────────────────────────────────────────────────────────────
 
async def subscription_guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Блокирует все действия клиентов (не владельца, не разработчика),
    если пробный период истёк и подписка не оплачена.
    Срабатывает раньше остальных хендлеров (group=-1)."""
    uid_ = update.effective_user.id if update.effective_user else None
    if uid_ in (OWNER, DEVELOPER_ID):
        return  # владельцу и разработчику доступ не блокируем
    if is_subscription_active():
        return
    if update.callback_query:
        await update.callback_query.answer(
            "Бот временно недоступен.", show_alert=True
        )
        raise ApplicationHandlerStop
    if update.message:
        await update.message.reply_text(
            "🙁 Бот временно недоступен. Пожалуйста, обратитесь напрямую к администратору."
        )
        raise ApplicationHandlerStop
 
def build_telegram_app():
    app = Application.builder().token(TOKEN).build()
 
    start_fb  = CommandHandler("start",  cmd_start)
    cancel_fb = CommandHandler("cancel", cmd_cancel)
 
    book_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_book, pattern="^book$")],
        states={
            ASK_DATE: [CallbackQueryHandler(cb_ask_date, pattern="^date:")],
            ASK_SLOT: [CallbackQueryHandler(cb_ask_slot, pattern="^slot:")],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            ASK_PHONE:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            CONFIRM:  [CallbackQueryHandler(cb_confirm, pattern="^confirm_book$")],
        },
        fallbacks=[
            CallbackQueryHandler(cb_cancel_conv, pattern="^cancel_conv$"),
            start_fb,
            cancel_fb,
        ],
        per_message=False,
        conversation_timeout=600,
    )
 
    rsch_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_reschedule, pattern="^reschedule$")],
        states={
            RSCH_PICK: [CallbackQueryHandler(cb_rsch_pick, pattern="^rpick:")],
            RSCH_DATE: [CallbackQueryHandler(cb_rsch_date, pattern="^rdate:")],
            RSCH_SLOT: [CallbackQueryHandler(cb_rsch_slot, pattern="^rslot:")],
        },
        fallbacks=[
            CallbackQueryHandler(cb_cancel_conv, pattern="^cancel_conv$"),
            start_fb,
            cancel_fb,
        ],
        per_message=False,
        conversation_timeout=600,
    )
 
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", cmd_settings)],
        states={
            SET_MENU: [
                CallbackQueryHandler(cb_set_wd,     pattern="^set_wd$"),
                CallbackQueryHandler(cb_set_wtime,  pattern="^set_wtime$"),
                CallbackQueryHandler(cb_set_lunch,  pattern="^set_lunch$"),
                CallbackQueryHandler(cb_set_sd,     pattern="^set_sd$"),
                CallbackQueryHandler(cb_set_vac,    pattern="^set_vac$"),
                CallbackQueryHandler(cb_set_delvac, pattern="^set_delvac$"),
                CallbackQueryHandler(cb_set_back,   pattern="^set_back$"),
                CallbackQueryHandler(cb_set_close,  pattern="^set_close$"),
            ],
            SET_WD: [
                CallbackQueryHandler(cb_tog_wd,   pattern="^togwd:"),
                CallbackQueryHandler(cb_set_back, pattern="^set_back$"),
            ],
            SET_WS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_wtime)],
            SET_LS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_lunch)],
            SET_SD: [
                CallbackQueryHandler(cb_set_sd_val, pattern="^setsd:"),
                CallbackQueryHandler(cb_set_back,   pattern="^set_back$"),
            ],
            SET_VAC:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_vac)],
            SET_DELVAC: [CallbackQueryHandler(cb_del_vac, pattern="^delvac:")],
        },
        fallbacks=[
            CallbackQueryHandler(cb_set_close, pattern="^set_close$"),
            start_fb,
            cancel_fb,
        ],
        per_message=False,
        conversation_timeout=600,
    )
 
    app.add_handler(MessageHandler(filters.ALL, subscription_guard), group=-1)
    app.add_handler(CallbackQueryHandler(subscription_guard), group=-1)
 
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CallbackQueryHandler(cb_subscribe_period, pattern="^sub_pay:"))
    app.add_handler(CommandHandler("setprice",  cmd_setprice))
    app.add_handler(PreCheckoutQueryHandler(cb_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, cb_successful_payment))
    app.add_handler(book_conv)
    app.add_handler(rsch_conv)
    app.add_handler(settings_conv)
    app.add_handler(CallbackQueryHandler(cb_my_bookings,    pattern="^my_bookings$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_booking, pattern="^cancel_booking$"))
    app.add_handler(CallbackQueryHandler(cb_del,            pattern="^del:"))
    app.add_handler(CallbackQueryHandler(cb_back_start,     pattern="^back_start$"))
    app.add_handler(MessageHandler(filters.Document.ALL,    handle_doc))
    app.job_queue.run_repeating(check_reminders, interval=300, first=10)
    return app
 
# ── main ──────────────────────────────────────────────────────────────────────
 
from telegram.error import Conflict, NetworkError, TimedOut
 
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок.
    Сетевые и Conflict-ошибки — временные, логируем коротко без трейсбека."""
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("Conflict (временно живы два экземпляра, старый скоро остановится).")
    elif isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Сетевой сбой (временный, переподключаемся): %s", err)
    else:
        logger.error("Ошибка при обработке апдейта: %s", err, exc_info=err)
 
async def run_all():
    init_db()
 
    tg_app = build_telegram_app()
    tg_app.add_error_handler(on_error)
    await tg_app.initialize()
    await tg_app.start()
 
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
 
    web_app = create_web_app()
 
    # Добавляем эндпоинт для webhook
    async def telegram_webhook(request: web.Request) -> web.Response:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
        return web.Response(status=200)
 
    web_app.router.add_post("/telegram", telegram_webhook)
 
    # ── интеграция с MAX ─────────────────────────────────────────────────
    from max_bot import handle_max_webhook, max_set_subscription
    web_app.router.add_post("/max", handle_max_webhook)
 
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
 
    # Регистрируем webhook в Telegram
    await tg_app.bot.set_webhook(
        url=webhook_url + "/telegram",
        drop_pending_updates=True,
    )
    logger.info("✅ Webhook установлен: %s/telegram", webhook_url)
 
    try:
        await max_set_subscription(webhook_url + "/max")
        logger.info("✅ MAX webhook установлен: %s/max", webhook_url)
    except Exception as e:
        logger.error("Не удалось зарегистрировать webhook MAX: %s", e)
 
    logger.info("✅ Бот v5 запущен (HTTP API на порту %s)", PORT)
 
    # Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
 
    await stop_event.wait()
 
    logger.info("Получен сигнал остановки, завершаем работу...")
    await tg_app.bot.delete_webhook()
    await tg_app.stop()
    await tg_app.shutdown()
    await runner.cleanup()
    logger.info("Бот остановлен.")
 
if __name__ == "__main__":
    asyncio.run(run_all())
