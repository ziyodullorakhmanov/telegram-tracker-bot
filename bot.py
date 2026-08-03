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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "tracker.db")
DEFAULT_REMINDER_TIME = "21:00"  # HH:MM, server vaqti bo'yicha (pastda tushuntirilgan)

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
        "/stats — oxirgi 7 kunlik statistika\n"
        "/settime HH:MM — kechki eslatma vaqtini o'rnatish\n\n"
        "Shuningdek, menga oddiy xabar yozsangiz (masalan, nima qilganingiz haqida), "
        "men uni kundaligingizga yozib qo'yaman va joriy progressni ko'rsataman."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
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
            "Xohlasangiz, izoh yozib qo'yishingiz mumkin — u ham saqlanadi."
        )
        context.user_data["awaiting_rating_note"] = True


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_evening_prompt(update.effective_chat.id, context)


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
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_once(lambda ctx: None, when=0)  # job_queue ishga tushishini ta'minlash
    app.post_init = schedule_all_reminders

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
