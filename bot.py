"""
Daily Task & Goal Tracker Telegram Bot
----------------------------------------
Kunlik tasklar, maqsadlar va ish sifatini (1-10 reyting) kuzatib boradigan bot.

Ishga tushirish:
    1. .env fayl yarating va ichiga: BOT_TOKEN=xxxxx yozing
    2. pip install -r requirements.txt
    3. python bot.py
"""

import logging
import os
import sqlite3
from datetime import datetime, date, time as dtime
from contextlib import closing

import hashlib
import hmac
import json
import threading
import time as time_module
from urllib.parse import parse_qsl

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

try:
    from anthropic import Anthropic
except ImportError:  # anthropic o'rnatilmagan bo'lsa ham bot ishlashda davom etsin
    Anthropic = None

try:
    import uvicorn
    from fastapi import FastAPI, Query
    from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:  # fastapi/uvicorn o'rnatilmagan bo'lsa, mini-app o'chiriladi
    uvicorn = None
    FastAPI = None

# ---------------------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "tracker.db")
DEFAULT_REMINDER_TIME = "21:00"  # HH:MM, server vaqti bo'yicha (pastda tushuntirilgan)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
ai_client = Anthropic(api_key=ANTHROPIC_API_KEY) if (Anthropic and ANTHROPIC_API_KEY) else None

# Railway'da Settings -> Networking -> Generate Domain orqali olingan havola,
# masalan: https://telegram-tracker-bot-production.up.railway.app
WEBAPP_BASE_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
if WEBAPP_BASE_URL and not WEBAPP_BASE_URL.startswith(("http://", "https://")):
    WEBAPP_BASE_URL = "https://" + WEBAPP_BASE_URL  # https:// unutilgan bo'lsa ham ishlasin
WEBAPP_URL = f"{WEBAPP_BASE_URL}/webapp" if WEBAPP_BASE_URL else None
PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                name TEXT,
                reminder_time TEXT DEFAULT '21:00',
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT,
                task_date TEXT,
                done INTEGER DEFAULT 0,
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT,
                log_date TEXT,
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                rating_date TEXT,
                rating INTEGER,
                note TEXT,
                UNIQUE(chat_id, rating_date)
            )"""
        )


def today_str():
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def ensure_user(chat_id: int, name: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, name, reminder_time, created_at) VALUES (?,?,?,?)",
            (chat_id, name, DEFAULT_REMINDER_TIME, datetime.now().isoformat()),
        )


def add_goal(chat_id: int, text: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO goals (chat_id, text, task_date, created_at) VALUES (?,?,?,?)",
            (chat_id, text, today_str(), datetime.now().isoformat()),
        )


def get_today_goals(chat_id: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM goals WHERE chat_id=? AND task_date=? ORDER BY id",
            (chat_id, today_str()),
        ).fetchall()


def mark_done(chat_id: int, goal_id: int) -> bool:
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE goals SET done=1 WHERE id=? AND chat_id=?", (goal_id, chat_id)
        )
        return cur.rowcount > 0


def add_log(chat_id: int, text: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO logs (chat_id, text, log_date, created_at) VALUES (?,?,?,?)",
            (chat_id, text, today_str(), datetime.now().isoformat()),
        )


def save_rating(chat_id: int, rating: int, note: str = ""):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """INSERT INTO ratings (chat_id, rating_date, rating, note) VALUES (?,?,?,?)
               ON CONFLICT(chat_id, rating_date) DO UPDATE SET rating=excluded.rating, note=excluded.note""",
            (chat_id, today_str(), rating, note),
        )


def get_stats(chat_id: int, days: int = 7):
    with closing(get_conn()) as conn:
        goals = conn.execute(
            "SELECT done, COUNT(*) as c FROM goals WHERE chat_id=? AND task_date >= date('now', ?) GROUP BY done",
            (chat_id, f"-{days} days"),
        ).fetchall()
        ratings = conn.execute(
            "SELECT AVG(rating) as avg_r, COUNT(*) as n FROM ratings WHERE chat_id=? AND rating_date >= date('now', ?)",
            (chat_id, f"-{days} days"),
        ).fetchone()
        return goals, ratings


def get_today_logs(chat_id: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT text, created_at FROM logs WHERE chat_id=? AND log_date=? ORDER BY id",
            (chat_id, today_str()),
        ).fetchall()


async def get_ai_feedback(chat_id: int) -> str:
    """Bugungi tasklar va loglarni Claude API'ga yuborib, ish sifati bo'yicha
    qisqa tahlil va 1-10 taklif reytingini oladi."""
    if not ai_client:
        return (
            "⚠️ AI tahlil hozircha yoqilmagan. Buni yoqish uchun Railway'da "
            "`ANTHROPIC_API_KEY` muhit o'zgaruvchisini qo'shing."
        )

    goals = get_today_goals(chat_id)
    logs = get_today_logs(chat_id)

    if not goals and not logs:
        return "Bugun hali hech qanday task yoki yozuv qo'shmadingiz — tahlil qilishga narsa yo'q."

    goals_text = "\n".join(
        f"- [{'bajarildi' if g['done'] else 'bajarilmadi'}] {g['text']}" for g in goals
    ) or "(task qo'shilmagan)"
    logs_text = "\n".join(f"- {l['text']}" for l in logs) or "(yozuv yo'q)"

    prompt = (
        "Siz mehribon, lekin halol shaxsiy mahsuldorlik murabbiysiz. "
        "Quyida foydalanuvchining bugungi tasklari va u yozgan ish loglari berilgan. "
        "O'zbek tilida, 4-6 jumlada qisqa tahlil bering: nima yaxshi ketdi, "
        "nimani yaxshilash mumkin, va oxirida \"Taklif reyting: X/10\" formatida "
        "ish sifati bo'yicha o'z bahoingizni yozing.\n\n"
        f"Bugungi tasklar:\n{goals_text}\n\nBugungi yozuvlar (loglar):\n{logs_text}"
    )

    try:
        response = ai_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        return "\n".join(parts).strip() or "AI javob berolmadi, birozdan keyin qayta urinib ko'ring."
    except Exception as e:
        logger.error(f"AI feedback xatosi: {e}")
        return "⚠️ AI tahlil olishda xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring."


def get_chart_data(chat_id: int, days: int = 14):
    with closing(get_conn()) as conn:
        goal_rows = conn.execute(
            """SELECT task_date as d, SUM(done) as done, COUNT(*) as total
               FROM goals WHERE chat_id=? AND task_date >= date('now', ?)
               GROUP BY task_date ORDER BY task_date""",
            (chat_id, f"-{days} days"),
        ).fetchall()
        rating_rows = conn.execute(
            """SELECT rating_date as d, rating FROM ratings
               WHERE chat_id=? AND rating_date >= date('now', ?)
               ORDER BY rating_date""",
            (chat_id, f"-{days} days"),
        ).fetchall()
    return {
        "goals": [{"date": r["d"], "done": r["done"], "total": r["total"]} for r in goal_rows],
        "ratings": [{"date": r["d"], "rating": r["rating"]} for r in rating_rows],
    }


# ---------------------------------------------------------------------------
# ASOSIY MENYU (reply keyboard)
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("➕ Task qo'shish"), KeyboardButton("📋 Bugungi tasklar")],
        [KeyboardButton("🌙 Kunni yakunlash"), KeyboardButton("📊 Statistika")],
        [KeyboardButton("🧠 AI tahlil"), KeyboardButton("⚙️ Vaqtni sozlash")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def day_progress_text(chat_id: int) -> str:
    goals = get_today_goals(chat_id)
    if not goals:
        return "Bugun hali maqsad qo'shmadingiz. /goal buyrug'i bilan qo'shing."
    done = sum(1 for g in goals if g["done"])
    lines = [f"📋 Bugungi progress: {done}/{len(goals)} bajarildi\n"]
    for g in goals:
        mark = "✅" if g["done"] else "◻️"
        lines.append(f"{mark} #{g['id']} {g['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(update.effective_chat.id, user.first_name or "")
    text = (
        f"Salom, {user.first_name}! 👋\n\n"
        "Men sizning kunlik tasklaringiz, maqsadlaringiz va ish sifatingizni kuzatib boraman.\n\n"
        "*Buyruqlar:*\n"
        "/goal <matn> — bugungi maqsad/task qo'shish\n"
        "/goals — bugungi tasklar ro'yxati (bajarish uchun tugmalar bilan)\n"
        "/done <raqam> — taskni bajarildi deb belgilash\n"
        "/report — kunni yakunlab, 1-10 baholash\n"
        "/feedback — bugungi ish sifati bo'yicha AI tahlil olish\n"
        "/stats — oxirgi 7 kunlik statistika\n"
        "/settime HH:MM — kechki eslatma vaqtini o'rnatish\n\n"
        "Shuningdek, menga oddiy xabar yozsangiz (masalan, nima qilganingiz haqida), "
        "men uni kundaligingizga yozib qo'yaman va joriy progressni ko'rsataman.\n\n"
        "Pastdagi tugmali menyudan ham foydalanishingiz mumkin 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())
    await schedule_reminder(context, update.effective_chat.id, DEFAULT_REMINDER_TIME)


async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user(chat_id, update.effective_user.first_name or "")
    if not context.args:
        await update.message.reply_text("Foydalanish: /goal Kod yozib bo'lish")
        return
    text = " ".join(context.args)
    add_goal(chat_id, text)
    await update.message.reply_text(f"✅ Maqsad qo'shildi: {text}")


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    goals = get_today_goals(chat_id)
    if not goals:
        await update.message.reply_text("Bugun hali maqsad qo'shmadingiz. /goal buyrug'i bilan qo'shing.")
        return
    buttons = [
        [InlineKeyboardButton(f"{'✅' if g['done'] else '◻️'} {g['text']}", callback_data=f"done:{g['id']}")]
        for g in goals if not g["done"]
    ]
    text = day_progress_text(chat_id)
    if buttons:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text + "\n\n🎉 Barcha tasklar bajarildi!")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Foydalanish: /done 3  (task raqami)")
        return
    goal_id = int(context.args[0])
    if mark_done(chat_id, goal_id):
        await update.message.reply_text("✅ Bajarildi deb belgilandi!\n\n" + day_progress_text(chat_id))
    else:
        await update.message.reply_text("Bunday raqamli task topilmadi.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if query.data.startswith("done:"):
        goal_id = int(query.data.split(":")[1])
        mark_done(chat_id, goal_id)
        await query.edit_message_text(day_progress_text(chat_id))
    elif query.data.startswith("rate:"):
        rating = int(query.data.split(":")[1])
        save_rating(chat_id, rating)
        await query.edit_message_text(
            f"Kun {rating}/10 deb baholandi. Rahmat! Ertaga ko'rishguncha 👋\n\n"
            "Xohlasangiz, izoh yozib qo'yishingiz mumkin — u ham saqlanadi.\n"
            "(AI tahlil olish uchun istalgan vaqt /feedback yozing)"
        )
        context.user_data["awaiting_rating_note"] = True


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_evening_prompt(update.effective_chat.id, context)


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thinking_msg = await update.message.reply_text("🤔 Tahlil qilyapman...")
    feedback = await get_ai_feedback(chat_id)
    await thinking_msg.edit_text(f"🧠 *AI tahlili*\n\n{feedback}", parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    goals, ratings = get_stats(chat_id, days=7)
    done = sum(row["c"] for row in goals if row["done"] == 1)
    total = sum(row["c"] for row in goals)
    pct = round(100 * done / total) if total else 0
    avg_r = round(ratings["avg_r"], 1) if ratings and ratings["avg_r"] else None

    text = (
        "📊 *Oxirgi 7 kunlik statistika*\n\n"
        f"Tasklar: {done}/{total} bajarildi ({pct}%)\n"
        f"O'rtacha kun reytingi: {avg_r if avg_r is not None else 'hali baholanmagan'}/10\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Foydalanish: /settime 21:00")
        return
    try:
        hh, mm = context.args[0].split(":")
        t = dtime(int(hh), int(mm))
    except Exception:
        await update.message.reply_text("Noto'g'ri format. Masalan: /settime 21:00")
        return
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET reminder_time=? WHERE chat_id=?", (context.args[0], chat_id)
        )
    await schedule_reminder(context, chat_id, context.args[0])
    await update.message.reply_text(f"⏰ Kechki eslatma vaqti {context.args[0]} ga o'rnatildi.")


# ---------------------------------------------------------------------------
# FREE-TEXT HANDLER — darhol javob berish
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Agar oxirgi baholashdan keyin izoh kutilayotgan bo'lsa
    if context.user_data.get("awaiting_rating_note"):
        with closing(get_conn()) as conn, conn:
            conn.execute(
                "UPDATE ratings SET note=? WHERE chat_id=? AND rating_date=?",
                (text, chat_id, today_str()),
            )
        context.user_data["awaiting_rating_note"] = False
        await update.message.reply_text("Izoh saqlandi. Rahmat! 🙌")
        return

    # Menyu orqali "Task qo'shish" bosilgan bo'lsa, keyingi xabarni task sifatida kutamiz
    if context.user_data.get("awaiting_goal_text"):
        context.user_data["awaiting_goal_text"] = False
        add_goal(chat_id, text)
        await update.message.reply_text(f"✅ Maqsad qo'shildi: {text}")
        return

    # Menyu orqali "Vaqtni sozlash" bosilgan bo'lsa, keyingi xabarni vaqt sifatida kutamiz
    if context.user_data.get("awaiting_time_text"):
        context.user_data["awaiting_time_text"] = False
        try:
            hh, mm = text.split(":")
            dtime(int(hh), int(mm))
        except Exception:
            await update.message.reply_text("Noto'g'ri format. Masalan: 21:00 deb yozing.")
            return
        with closing(get_conn()) as conn, conn:
            conn.execute("UPDATE users SET reminder_time=? WHERE chat_id=?", (text, chat_id))
        await schedule_reminder(context, chat_id, text)
        await update.message.reply_text(f"⏰ Kechki eslatma vaqti {text} ga o'rnatildi.")
        return

    # --- Asosiy menyu tugmalari ---
    if text == "➕ Task qo'shish":
        context.user_data["awaiting_goal_text"] = True
        await update.message.reply_text("Nimani qo'shmoqchisiz? Task matnini yozing:")
        return

    if text == "📋 Bugungi tasklar":
        await cmd_goals(update, context)
        return

    if text == "🌙 Kunni yakunlash":
        await send_evening_prompt(chat_id, context)
        return

    if text == "🧠 AI tahlil":
        await cmd_feedback(update, context)
        return

    if text == "⚙️ Vaqtni sozlash":
        context.user_data["awaiting_time_text"] = True
        await update.message.reply_text("Kechki eslatma vaqtini yozing, masalan: 21:00")
        return

    if text == "📊 Statistika":
        if WEBAPP_URL:
            inline_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📊 Statistikani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]
            )
            await update.message.reply_text(
                "Interaktiv statistikani ochish uchun bosing 👇", reply_markup=inline_kb
            )
        else:
            await cmd_stats(update, context)
        return

    # Boshqa har qanday matn — kundalik log sifatida saqlanadi
    ensure_user(chat_id, update.effective_user.first_name or "")
    add_log(chat_id, text)
    await update.message.reply_text(
        "📝 Yozib qo'ydim.\n\n" + day_progress_text(chat_id)
    )


# ---------------------------------------------------------------------------
# KECHKI ESLATMA (scheduled job)
# ---------------------------------------------------------------------------

async def send_evening_prompt(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    goals = get_today_goals(chat_id)
    done = sum(1 for g in goals if g["done"])
    total = len(goals)
    summary = f"Bugun {done}/{total} task bajardingiz." if total else "Bugun hech qanday task belgilamagansiz."

    buttons = [
        [InlineKeyboardButton(str(i), callback_data=f"rate:{i}") for i in range(1, 6)],
        [InlineKeyboardButton(str(i), callback_data=f"rate:{i}") for i in range(6, 11)],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌙 Kun yakunlandi.\n{summary}\n\nBugungi kuningizni 1 dan 10 gacha qanday baholaysiz?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await send_evening_prompt(chat_id, context)


async def schedule_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, hhmm: str):
    # avvalgi job bo'lsa, olib tashlaymiz
    for job in context.job_queue.get_jobs_by_name(f"reminder_{chat_id}"):
        job.schedule_removal()
    hh, mm = hhmm.split(":")
    context.job_queue.run_daily(
        reminder_job,
        time=dtime(int(hh), int(mm)),
        chat_id=chat_id,
        name=f"reminder_{chat_id}",
    )


async def schedule_all_reminders(app: Application):
    with closing(get_conn()) as conn:
        users = conn.execute("SELECT chat_id, reminder_time FROM users").fetchall()
    for u in users:
        hh, mm = u["reminder_time"].split(":")
        app.job_queue.run_daily(
            reminder_job,
            time=dtime(int(hh), int(mm)),
            chat_id=u["chat_id"],
            name=f"reminder_{u['chat_id']}",
        )


# ---------------------------------------------------------------------------
# MINI APP (Telegram WebApp) — FastAPI server
# ---------------------------------------------------------------------------

def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400):
    """Telegram WebApp initData'ni rasmiy algoritm bo'yicha tekshiradi.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        logger.warning("initData tekshiruvi: 'hash' maydoni topilmadi")
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        logger.warning(
            f"initData tekshiruvi: hash mos kelmadi. "
            f"kutilgan_boshi={computed_hash[:8]} olingan_boshi={received_hash[:8]} "
            f"token_uzunligi={len(bot_token)}"
        )
        return None

    auth_date = int(parsed.get("auth_date", 0))
    if max_age and (time_module.time() - auth_date) > max_age:
        return None

    user_json = parsed.get("user")
    if not user_json:
        return None
    return json.loads(user_json)


web_app = FastAPI() if FastAPI else None

if web_app:

    @web_app.get("/webapp", response_class=HTMLResponse)
    async def webapp_page():
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()

    @web_app.get("/api/stats")
    async def api_stats(initData: str = Query(default="")):
        user = verify_init_data(initData, BOT_TOKEN)
        if not user:
            return JSONResponse({"error": "invalid_init_data"}, status_code=401)
        chat_id = user["id"]
        data = get_chart_data(chat_id, days=14)
        return JSONResponse(data)

    @web_app.get("/health")
    async def health():
        return {"status": "ok"}


def run_webserver():
    if not web_app:
        logger.warning("fastapi/uvicorn o'rnatilmagan — mini-app o'chirilgan.")
        return
    uvicorn.run(web_app, host="0.0.0.0", port=PORT, log_level="warning")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable o'rnatilmagan! .env fayliga qo'shing.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_once(lambda ctx: None, when=0)  # job_queue ishga tushishini ta'minlash
    app.post_init = schedule_all_reminders

    if web_app:
        threading.Thread(target=run_webserver, daemon=True).start()
        logger.info(f"Mini-app serveri {PORT} portda ishga tushdi...")

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
